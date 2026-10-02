"""TSP host: answers a program's Tern Surface Protocol messages for one terminal.

This is the CONTROL BLOCK of GhostShell's TSP support, between the pty output
(source) and the pty input (return path).  One TspHost serves one terminal
session.  Feed it every chunk of text read from the pty with ``filter``; it:

  * takes the TSP messages out of the text (the screen never sees them),
  * answers the program's ``hello`` query so the program switches to its native
    interface,
  * keeps a TspDocument per open surface and applies every frame to it,
  * acknowledges every frame (without acks the program stalls ~5 seconds),
  * and returns the remaining text, which is drawn as usual.

It never touches Sublime and never writes to the pty itself: replies go out
through the ``send`` function the caller gives it.

Order matters: the program sends ``hello`` and then a DA1 query in the same
write, and the terminal engine answers DA1 on its own.  The hello reply must
reach the program first, so call ``filter`` BEFORE feeding text to the engine.

Behaviour follows omp (github.com/can1357/oh-my-pi, packages/tui/src/native/
backend.ts and terminal.ts, main branch, 2026-10-01).
"""

import json
import threading

from .tsp_codec import TspReader, ack_event, encode_message, hello_reply
from .tsp_document import TSP_KINDS, TspDocument
from .tsp_text import render_lines

CLEAR_SCREEN = "\x1b[H\x1b[2J"        # cursor home, then erase the visible screen


class TspHost:
    """The terminal side of one TSP conversation.

    ``send``           function taking one str to write to the pty input.
    ``kinds``          component kinds this terminal says it can draw.
    ``windows_conpty`` True on Windows: replies use OSC 877 because ConPTY
                       drops APC on its input side.
    ``get_cols``       function returning the current width in cells, or None.
    ``on_change``      optional function called with a surface id after a
                       frame changed that surface (a renderer hooks in here).
    """

    def __init__(self, send, kinds=TSP_KINDS, windows_conpty=True,
                 get_cols=None, on_change=None):
        self._send = send
        self._kinds = tuple(kinds)
        self._conpty = windows_conpty
        self._get_cols = get_cols
        self._on_change = on_change
        self._reader = TspReader()
        self._strip_reader = TspReader()    # for strip(): keeps a message that straddles two chunks whole
        self.documents = {}         # surface id -> TspDocument
        self.hello_seen = False     # the program asked, so it speaks TSP
        self.errors = []            # rejected ops, newest last (kept short)
        self.palettes = {}          # surface id -> latest palette message
        self._changed = []          # surface ids whose frames changed since the last paint
        self._surface_order = []    # surface ids in the order the program opened them
        self._closed = set()        # surface ids the program has closed
        self._told_gone = set()     # unknown surface ids we already answered with `gone`
        self.blobs = {}             # content hash -> base64 text of an image the program sent
        self.events_sent = []       # the last events sent to the program (clicks), newest last

    def strip(self, text):
        """Take TSP messages out of ``text`` WITHOUT handling them; return the text left.

        Used on the replay of old output when a tab reattaches to a program that kept running: the
        messages in it are history (frames of a surface this host never opened), so applying or
        answering them would be wrong, and leaving them in would draw their JSON as text.
        """
        return self._strip_reader.feed_text(text)[1]

    def request_resync(self, delay=0.3):
        """Ask a program that has been running since before this host existed to send everything again.

        Sends a width one cell smaller and, ``delay`` seconds later, the real width.  The program
        re-lays itself out and sends frames for its old surface; this host does not know that surface,
        answers `gone` (see _handle_frame), and the program opens a new surface and sends the whole
        document.  A one-shot timer is used because there is no event for "the program has drawn".
        """
        cols = self._get_cols() if self._get_cols else None
        if not isinstance(cols, int) or cols < 2:
            return
        self._reply("e", {"ev": "resize", "cols": cols - 1})
        timer = threading.Timer(delay, lambda: self._reply("e", {"ev": "resize", "cols": cols}))
        timer.daemon = True
        timer.start()

    def filter(self, text):
        """Handle TSP messages in ``text``; return the text left to draw."""
        messages, passthrough = self._reader.feed_text(text)
        for verb, body, params in messages:
            self._handle(verb, body, params)
        return passthrough

    def _reply(self, verb, value):
        self._send(encode_message(verb, value, windows_conpty=self._conpty))

    def _handle(self, verb, body, params):
        if verb == "b":                 # an image blob: base64 text, kept under its content hash
            if params.get("id"):
                self.blobs[params["id"]] = body
            return
        try:
            value = json.loads(body)
        except ValueError:
            return                      # malformed: the protocol says ignore
        if not isinstance(value, dict):
            return
        if verb == "q":
            self._handle_query(value)
        elif verb == "o":
            self._handle_open(value)
        elif verb == "x":
            self._handle_close(value)
        elif verb == "f":
            self._handle_frame(value)
        elif verb == "t":
            if "sf" in value:
                self.palettes[value["sf"]] = value
        # Any other verb is unknown: ignored, as the protocol requires.

    def _handle_query(self, value):
        kind = value.get("q")
        if kind == "hello":
            self.hello_seen = True
            cols = self._get_cols() if self._get_cols else None
            self._reply("r", hello_reply(
                self._kinds,
                features=("blobs", "settle", "adopt", "dock"),
                cols=cols,
            ))
        elif kind == "blobs":
            wanted = value.get("ids") or []
            self._reply("r", {"r": "blobs", "have": [i for i in wanted if i in self.blobs]})

    def _handle_open(self, value):
        surface_id = value.get("id")
        if not isinstance(surface_id, str):
            return
        if value.get("adopt") and surface_id not in self.documents:
            # The program wants to reuse a surface we no longer have.
            self._reply("e", {"ev": "gone", "ids": [surface_id]})
            return
        if not value.get("adopt"):
            self.documents[surface_id] = TspDocument(surface_id)
            if surface_id in self._surface_order:
                self._surface_order.remove(surface_id)
            self._surface_order.append(surface_id)
            self._closed.discard(surface_id)

    def _handle_close(self, value):
        surface_id = value.get("id")
        document = self.documents.get(surface_id)
        if document is None:
            return
        self._closed.add(surface_id)
        if value.get("keep"):
            document.close()
        else:
            del self.documents[surface_id]

    def _handle_frame(self, value):
        surface_id = value.get("sf")
        document = self.documents.get(surface_id)
        if document is None:
            # A frame for a surface this host never opened: the program has been running since before
            # this host existed (a tab reattached after Sublime restarted).  Say once that the surface
            # is gone; the program then opens a new one and sends the whole document again.
            if isinstance(surface_id, str) and surface_id not in self._told_gone:
                self._told_gone.add(surface_id)
                self._reply("e", {"ev": "gone", "ids": [surface_id]})
            return
        for error in document.apply_frame(value):
            self.errors.append(error)
            del self.errors[:-20]       # keep only the newest 20
            self._reply("e", {"ev": "error", "sf": surface_id,
                              "s": error["s"], "op": error["op"],
                              "msg": error["msg"]})
        self._reply("e", ack_event(surface_id, value.get("s")))
        if surface_id not in self._changed:
            self._changed.append(surface_id)
        if self._on_change:
            self._on_change(surface_id)

    def send_event(self, event):
        """Send a terminal -> program event (a click or key result) to the program."""
        self.events_sent.append(event)
        del self.events_sent[:-50]      # keep the last 50 for diagnosis
        self._reply("e", event)

    def active_surface(self):
        """Id of the newest surface the program has not closed, or None.

        While there is one, the program is drawing natively and its text rows
        are not shown; after it closes (or before it opens) the screen text is.
        """
        for surface_id in reversed(self._surface_order):
            if surface_id in self.documents and surface_id not in self._closed:
                return surface_id
        return None

    def take_paint(self, cols, rows):
        """Text that repaints the screen from the newest changed surface, or "".

        A full repaint (clear, then the last ``rows`` lines of the surface drawn
        as text) is sent each time frames changed something, because the
        document, not the screen, holds the transcript.  Lines above the visible
        rows are left out: the screen is a window on the tail, like a terminal
        that follows new output.
        """
        if not self._changed:
            return ""
        surface_id = self._changed[-1]
        del self._changed[:]
        document = self.documents.get(surface_id)
        if document is None:
            return ""
        lines = render_lines(document, cols)
        return CLEAR_SCREEN + "\r\n".join(lines[-rows:])
