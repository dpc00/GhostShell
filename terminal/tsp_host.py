"""TSP host: answers a program's Tern Surface Protocol messages for one terminal.

This is the CONTROL BLOCK of GhostShell's TSP support, between the pty output
(source) and the pty input (return path).  One TspHost serves one terminal
session.  Feed it every chunk of text read from the pty with ``filter``; it:

  * takes the TSP messages out of the text (the screen never sees them),
  * answers the program's ``hello`` query so the program switches to its native
    interface, and the ``blobs`` query,
  * keeps a TspDocument per open surface and applies every frame to it, with the
    spec's validation, limits and error events,
  * acknowledges frames (without acks the program stalls ~5 seconds): one ack per
    ``filter`` call and surface, carrying the highest frame, because an ack covers
    every frame up to it,
  * keeps palettes (``t``), stylesheets (``s``) and blobs (``b``),
  * applies the retention budget (closed surfaces first, then settled children of
    ``main``, reporting them with ``gone``),
  * and returns the remaining text, which is drawn as usual.

It never touches Sublime and never writes to the pty itself: replies go out
through the ``send`` function the caller gives it.

Order matters: the program sends ``hello`` and then a DA1 query in the same
write, and the terminal engine answers DA1 on its own.  The hello reply must
reach the program first, so call ``filter`` BEFORE feeding text to the engine.

Behaviour follows the protocol pages at docs.stencil.so/tern/ (transport, handshake,
surfaces, documents, input, theme, operations) and omp (github.com/can1357/oh-my-pi,
packages/tui/src/native/backend.ts and terminal.ts, main branch, 2026-10-01).

Things the caller has to tell the host (the host cannot see them):
  * ``shell_prompt()``     OSC 133;A: the program is gone; returns text to draw (a flow surface left open).
  * ``process_exited()``   the pane's process ended; returns text to draw.
  * ``reset()``            RIS (ESC c).
  * ``alt_screen_left()``  the alternate screen ended: screen surfaces opened on it go.
  * ``anchor_lost(id)``    a surface's anchor row left the scrollback.
  * ``send_event(...)``    clicks, resize, theme, motion, visible, edit, undo, send (see its docstring).
"""

import base64
import binascii
import hashlib
import json
import os
import re
import threading
import time
import traceback
from collections import OrderedDict

from .tsp_codec import (DEFAULT_APC_LIMIT, ERROR_VERB, TspReader, ack_event, encode_message,
                        encode_program_message, hello_reply)
from .tsp_document import TSP_EXTRA_KINDS, TSP_KINDS, TspDocument, check_css
from .tsp_text import render_lines

CLEAR_SCREEN = "\x1b[H\x1b[2J"        # cursor home, then erase the visible screen

MODES = ("inline", "screen", "flow")
FEATURES = ("blobs", "settle", "adopt", "dock", "program-palette", "reduce-motion", "scroll", "styles", "flow")
ASIDE_FEATURE = "aside"         # announced only when the view may open the sheet in a column of its own
CREDITS = 2
RETENTION_BUDGET = 64 * 1024 * 1024         # bytes of document state per pane
MAX_BLOB_BYTES = 16 * 1024 * 1024           # one blob, decoded
BLOBS_IN_MEMORY = 256 * 1024 * 1024         # unreferenced blobs go past this, least recently used first
BLOBS_ON_DISK = 1024 * 1024 * 1024          # the least recently shown go at the first blob use
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
# Program features whose events only go to a program that announced them in its hello.
_EVENT_FEATURES = {"edit": "edit", "undo": "undo", "send": "send"}
_hour12_cache = []


def system_hour12():
    """Whether the user's system clock reads 12 hours (``3:05 PM``): Windows' short time format, else LC_TIME."""
    if _hour12_cache:
        return _hour12_cache[0]
    result = False
    try:
        if os.name == "nt":
            import ctypes
            buffer = ctypes.create_unicode_buffer(80)
            if ctypes.windll.kernel32.GetLocaleInfoEx(None, 0x79, buffer, 80):     # LOCALE_SSHORTTIME
                result = "h" in buffer.value and "H" not in buffer.value
        else:
            sample = time.strftime("%X", (2000, 1, 1, 15, 5, 0, 5, 1, -1))
            result = "15" not in sample
    except Exception:
        result = False
    _hour12_cache.append(result)
    return result


def default_blob_dir():
    """Where blobs are kept on disk so a restored screen shows its images without a resend."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "GhostShell", "tsp-blobs")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "ghostshell", "tsp-blobs")


class _Surface:
    """One surface the program opened: its document and what belongs to it."""

    def __init__(self, surface_id, mode, document, alt):
        self.id = surface_id
        self.mode = mode
        self.doc = document
        self.alt = alt              # opened on the alternate screen (a screen surface only)
        self.listen = True
        self.closed = False
        self.title = None
        self.role = None
        self.palette = None         # the last `t` message
        self.palette_bytes = 0
        self.last_s = None          # the program's newest frame number
        self.pending_ack = None     # frame number to acknowledge when the current filter call ends

    def size(self):
        return self.doc.bytes + self.doc.sheet_bytes() + self.palette_bytes


class _BlobStore(OrderedDict):
    """Blob id -> base64 text.  A blob the program did not resend (a restart) is looked for on disk when a
    drawer asks for it, so a restored screen shows its images."""

    def __init__(self, host):
        OrderedDict.__init__(self)
        self._host = host

    def get(self, key, default=None):
        if OrderedDict.__contains__(self, key):
            return OrderedDict.__getitem__(self, key)
        if isinstance(key, str) and _SHA256.match(key) and self._host._blob_known(key.lower()):
            return OrderedDict.get(self, key.lower(), default)
        return default


class TspHost:
    """The terminal side of one TSP conversation.

    ``send``           function taking one str to write to the pty input.
    ``kinds``          component kinds this terminal says it can draw (to omp: see _handle_query).
    ``windows_conpty`` True on Windows: replies use OSC 877 because ConPTY
                       drops APC on its input side.
    ``get_cols``       function returning the current width in cells, or None.
    ``on_change``      optional function called with a surface id after a
                       frame changed that surface (a renderer hooks in here).
    ``get_reduce_motion`` / ``get_dark`` / ``get_cell`` / ``get_hour12``  functions giving the state the
                       hello reply reports (cell is {"w","h"} in pixels); defaults: False, True, unknown,
                       the system clock format.
    ``get_alt_screen`` function returning True while the alternate screen is active.
    ``retention_budget`` bytes of document state before eviction (default 64 MiB).
    ``blob_dir``       folder for blobs kept on disk; None = the default folder, "" = memory only.
    """

    def __init__(self, send, kinds=TSP_KINDS, windows_conpty=True,
                 get_cols=None, on_change=None, get_reduce_motion=None,
                 get_dark=None, get_cell=None, get_hour12=None, get_alt_screen=None,
                 retention_budget=RETENTION_BUDGET, blob_dir=None, clock=None, get_cwd=None, aside=False):
        self._send = send
        self._features = FEATURES + (ASIDE_FEATURE,) if aside else FEATURES
        self._kinds = tuple(kinds)
        self._conpty = windows_conpty
        self._get_cols = get_cols
        self._on_change = on_change
        self._get_reduce_motion = get_reduce_motion
        self._get_dark = get_dark
        self._get_cell = get_cell
        self._get_hour12 = get_hour12
        self._get_alt_screen = get_alt_screen
        self._get_cwd = get_cwd
        self._budget = retention_budget
        self._blob_dir = default_blob_dir() if blob_dir is None else blob_dir
        self._lock = threading.RLock()
        self._timer = None
        self._reader = TspReader(clock)
        self._strip_reader = TspReader()    # for strip(): keeps a message that straddles two chunks whole
        self.documents = {}         # surface id -> TspDocument (the newest surface with that id, an open one first)
        self.palettes = {}          # surface id -> latest palette message
        self.hello_seen = False     # the program asked, so it speaks TSP
        self.hello_app = None       # the "app" of its hello (lower case)
        self.hello_features = ()    # the "features" of its hello
        self.hello_ver = None
        self.kinds_offered = ()     # the kinds the hello reply listed
        self.dark = None            # the appearance a theme event last reported (None: none yet, ask get_dark)
        self.reduce_motion = None   # Reduce Motion as a motion event last reported
        self.visible = True
        self.cols = None            # the pane width last reported by a resize event
        self.cell = None
        self.errors = []            # rejected ops, newest last (kept short)
        self.internal_errors = []   # exceptions caught while handling a message (a bug, not the program's fault)
        self._surfaces = []         # _Surface objects, oldest first
        self._changed = []          # surface ids whose frames changed since the last paint
        self._stashed_palettes = {}     # `t` for a surface that did not exist yet
        self._kept_text = []        # rendered text of closed flow surfaces, added to the next filter result
        self._ever = set()          # every surface id the program has opened
        self._quiet = False         # a not-listening surface opened: nothing is sent until a q or a listening o
        self._program_gone = False  # shell prompt, process exit or RIS: answers stop until a q or an o
        self._replaying = False
        self._told_gone = set()     # unknown surface ids we already answered with `gone`
        self.blobs = _BlobStore(self)   # blob id (lowercase sha256) -> base64 text, least recently used first
        self._blob_sizes = {}
        self.blob_mimes = {}
        self._blob_bytes = 0
        self._disk_trimmed = False
        self.events_sent = []       # the last events sent to the program (clicks), newest last
        self._replay_surfaces = {}  # surface ids named in replayed history (an ordered set)

    # ----- replay of old output --------------------------------------------

    def strip(self, text):
        """Take TSP messages out of ``text`` WITHOUT handling them; return the text left.

        Used on the replay of old output when a tab reattaches to a program that kept running: the
        messages in it are history (frames of a surface this host never opened), so applying or
        answering them would be wrong, and leaving them in would draw their JSON as text.
        """
        messages, rest = self._strip_reader.feed_text(text)
        for verb, body, _params in messages:
            if verb in ("o", "f"):
                try:
                    value = json.loads(body)
                    surface_id = value.get("id") if verb == "o" else value.get("sf")
                except (ValueError, AttributeError, RecursionError):
                    continue
                if isinstance(surface_id, str):
                    self._replay_surfaces[surface_id] = None
        return rest

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
        # A program that is idle sends no frame after a resize, so nothing would ever reveal its old surface.
        # The replay named it: say it is gone and the program opens a new one with the whole document.
        stale = [i for i in self._replay_surfaces if self._find(i) is None and i not in self._told_gone]
        if stale:
            self._told_gone.update(stale)
            self._reply("e", {"ev": "gone", "ids": stale})
        self._reply("e", {"ev": "resize", "cols": cols - 1})
        timer = threading.Timer(delay, lambda: self._reply("e", {"ev": "resize", "cols": cols}))
        timer.daemon = True
        timer.start()

    # ----- the stream ---------------------------------------------------------

    def filter(self, text):
        """Handle TSP messages in ``text``; return the text left to draw."""
        with self._lock:
            messages, passthrough = self._reader.feed_text(text)
            for verb, body, params in messages:
                self._handle_safely(verb, body, params)
            self._flush_acks()
            self._arm_timer()
            if self._kept_text:
                # A flow surface closed with keep: its document becomes ordinary output in the scrollback.
                passthrough += "".join(self._kept_text)
                del self._kept_text[:]
        return passthrough

    def _arm_timer(self):
        """A chunk sequence that is left waiting must be dropped after a second even when nothing else arrives."""
        if not self._reader.waiting() or (self._timer is not None and self._timer.is_alive()):
            return
        self._timer = threading.Timer(1.05, self._on_timer)
        self._timer.daemon = True
        self._timer.start()

    def _on_timer(self):
        with self._lock:
            for verb, body, params in self._reader.poll():
                self._handle_safely(verb, body, params)
            self._arm_timer()

    def _handle_safely(self, verb, body, params):
        try:
            self._handle(verb, body, params)
        except Exception as error:          # one bad message must not turn TSP off for the whole tab
            self.internal_errors.append(traceback.format_exc())
            del self.internal_errors[:-10]
            self._error("internal error while handling a %s message: %s" % (verb, error))

    def _reply(self, verb, value):
        if not self._replaying:
            self._send(encode_message(verb, value, windows_conpty=self._conpty))

    def _answer(self, verb, value):
        """A reply, an error or a `gone` about something the program sent: not while it is known to be
        gone (after a shell prompt, a process exit or RIS) and not after it opened a surface with listen:false."""
        if self._quiet or self._program_gone:
            return
        self._reply(verb, value)

    def _error(self, message, surface=None, **fields):
        event = {"ev": "error"}
        if surface is not None:
            if not surface.listen:
                return
            event["sf"] = surface.id
        event.update(fields)
        event["msg"] = message
        self._answer("e", event)

    def _handle(self, verb, body, params):
        if verb == ERROR_VERB:          # the reader dropped a chunk sequence
            self._error(body)
            return
        if verb == "b":                 # an image blob: base64 text, kept under its content hash
            self._handle_blob(body, params)
            return
        if verb not in ("q", "o", "f", "t", "s", "x"):
            return                      # an unknown verb is ignored, without an error
        try:
            value = json.loads(body)
        except (ValueError, RecursionError) as error:
            self._error("malformed %s body: %s" % (verb, error))
            return
        if not isinstance(value, dict):
            self._error("malformed %s body: not an object" % verb)
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
            self._handle_palette(value)
        elif verb == "s":
            self._handle_sheet(value)

    # ----- hello and queries ------------------------------------------------------

    def _hello_kinds(self, app):
        """The kinds the hello reply lists: every kind the host really draws.

        omp keeps the list it has always been given (an extra kind could change what it sends); any other
        program (a Tern SDK program) is also offered media, block and el, when they are drawn."""
        kinds = list(self._kinds)
        if app and "omp" not in app and "oh-my-pi" not in app:
            drawn = None
            try:
                from . import tsp_html
                drawn = set(tsp_html._DRAWERS)
            except Exception:
                drawn = None
            for kind in TSP_EXTRA_KINDS:
                if kind not in kinds and (drawn is None or kind in drawn):
                    kinds.append(kind)
        return tuple(kinds)

    def _handle_query(self, value):
        self._quiet = False
        self._program_gone = False
        kind = value.get("q")
        if kind == "hello":
            versions = value.get("v")
            if not isinstance(versions, list) or not any(v == 1 and not isinstance(v, bool) for v in versions):
                return                  # Tern speaks only v1: a query without it gets no reply
            self.hello_seen = True
            app = value.get("app")
            features = value.get("features")
            app = app if isinstance(app, str) else ""
            self.hello_app = app.lower()    # for diagnosis: who asked, and which kinds were offered
            self.hello_ver = value.get("ver") if isinstance(value.get("ver"), str) else None
            self.hello_features = tuple(f for f in features if isinstance(f, str)) if isinstance(features, list) else ()
            self.kinds_offered = self._hello_kinds(self.hello_app)
            for surface in self._surfaces:
                self._decorate(surface)
            self._reply("r", self._hello_reply())
        elif kind == "blobs":
            wanted = value.get("ids")
            have = []
            for blob_id in wanted if isinstance(wanted, list) else []:
                if isinstance(blob_id, str) and _SHA256.match(blob_id):
                    key = blob_id.lower()
                    if self._blob_known(key):
                        have.append(blob_id)
            self._reply("r", {"r": "blobs", "have": have})

    def _hello_reply(self):
        cols = self._get_cols() if self._get_cols else self.cols
        cell = self.cell if self.cell is not None else (self._get_cell() if self._get_cell else None)
        if self.dark is not None:       # what a theme / motion / resize event last said is the newest word
            dark = self.dark
        else:
            dark = self._get_dark() if self._get_dark else True
        if self.reduce_motion is not None:
            reduce_motion = self.reduce_motion
        else:
            reduce_motion = bool(self._get_reduce_motion and self._get_reduce_motion())
        hour12 = self._get_hour12() if self._get_hour12 else system_hour12()
        return hello_reply(
            self.kinds_offered, features=self._features, cols=cols, cell=cell, dark=dark,
            reduce_motion=reduce_motion, apc=DEFAULT_APC_LIMIT, credits=CREDITS, hour12=hour12)

    # ----- surfaces ---------------------------------------------------------------

    def _alt(self):
        try:
            return bool(self._get_alt_screen()) if self._get_alt_screen else False
        except Exception:
            return False

    def _find(self, surface_id, open_only=False):
        """The surface ``f`` and ``x`` reach for ``surface_id``: the first open one (the active buffer's first),
        else the newest closed one (unless ``open_only``)."""
        if not isinstance(surface_id, str):
            return None
        alt = self._alt()
        best = None
        for surface in reversed(self._surfaces):
            if surface.id != surface_id or surface.closed:
                continue
            if surface.alt == alt or surface.mode != "screen":
                return surface
            best = best or surface
        if best is not None or open_only:
            return best
        for surface in reversed(self._surfaces):
            if surface.id == surface_id:
                return surface
        return None

    def surface_mode(self, surface_id):
        """The ``mode`` of the newest surface with this id (inline, screen, flow), or None."""
        surface = self._find(surface_id)
        return surface.mode if surface is not None else None

    def surface_info(self, surface_id):
        """{"mode","title","role","listen","closed","suspended"} of the newest surface with this id, or None.
        ``role`` is the `o` role (data-surface of the regions) and ``hello_app`` the program's app name (data-app)."""
        surface = self._find(surface_id)
        if surface is None:
            return None
        return {"mode": surface.mode, "title": surface.title, "role": surface.role, "listen": surface.listen,
                "closed": surface.closed, "suspended": surface.doc.suspended}

    def active_surface(self):
        """Id of the surface the pane shows natively, or None.

        A screen surface covers the pane, then an inline one owns it, then a flow one is drawn among the output.
        A surface the program suspended has handed the pane back to the grid, and a closed one is not shown.
        While there is one, the program is drawing natively and its text rows are not shown; otherwise
        the screen text is.
        """
        surface = self._active()
        return surface.id if surface is not None else None

    def _active(self):
        for mode in ("screen", "inline", "flow"):
            for surface in reversed(self._surfaces):
                if surface.mode == mode and not surface.closed and not surface.doc.suspended:
                    return surface
        return None

    def _sync(self):
        """Rebuild the id -> document and id -> palette maps the view reads."""
        self.documents.clear()
        self.palettes.clear()
        for closed in (True, False):
            for surface in self._surfaces:
                if surface.closed == closed:
                    self.documents[surface.id] = surface.doc
                    if surface.palette is not None:
                        self.palettes[surface.id] = surface.palette
                    else:
                        self.palettes.pop(surface.id, None)

    def _live(self):
        """The newest open surface (the one a `t` or `s` without `sf` is for)."""
        for surface in reversed(self._surfaces):
            if not surface.closed:
                return surface
        return None

    def _any_listening(self):
        return any(surface.listen and not surface.closed for surface in self._surfaces)

    def _discard(self, surface):
        if surface in self._surfaces:
            self._surfaces.remove(surface)
        if surface.id in self._changed and not any(s.id == surface.id for s in self._surfaces):
            self._changed.remove(surface.id)

    def _close_surface(self, surface, keep=True):
        """Close ``surface``: keep it (read-only, in place) or remove it; a screen surface is always discarded."""
        if surface.closed:
            if not keep:
                self._discard(surface)
            return
        if surface.mode == "screen" or not keep:
            self._discard(surface)
            return
        if surface.mode == "flow" and not self._replaying:
            cols = self._get_cols() if self._get_cols else None
            lines = render_lines(surface.doc, cols if isinstance(cols, int) and cols > 0 else 100)
            while lines and not lines[-1].strip():
                lines.pop()
            if lines:
                self._kept_text.append("\r\n".join(lines) + "\r\n")
        surface.closed = True
        surface.doc.close()

    def _handle_open(self, value):
        surface_id = value.get("id")
        listen = value.get("listen") is not False
        # A surface that does not listen turns every answer off, until the next q or a listening o.
        if listen:
            self._quiet = False
            self._program_gone = False
        elif not self._any_listening():
            self._quiet = True
        if not isinstance(surface_id, str) or not surface_id:
            self._error("o needs a surface id")
            return
        mode = value.get("mode", "inline")
        adopt = value.get("adopt") is True
        if mode is None:
            mode = "inline"
        if not adopt and mode not in MODES:
            if listen:
                self._error("unknown surface mode %s" % (json.dumps(mode),), sf=surface_id)
            return
        if adopt:
            self._adopt(surface_id, value, listen)
            return
        if mode in ("inline", "flow") and self._alt():
            if listen:
                self._error("%s surface cannot open on the alternate screen" % mode, sf=surface_id)
            return
        self._replace_live(mode)
        surface = _Surface(surface_id, mode, TspDocument(surface_id), self._alt() and mode == "screen")
        self._apply_open_fields(surface, value, listen)
        surface.palette = self._stashed_palettes.pop(surface_id, None)
        if surface.palette is not None:
            surface.palette_bytes = len(json.dumps(surface.palette))
        self._surfaces.append(surface)
        self._ever.add(surface_id)
        self._told_gone.discard(surface_id)
        self._sync()

    def _apply_open_fields(self, surface, value, listen):
        surface.listen = listen
        if isinstance(value.get("title"), str):
            surface.title = value["title"]
        if isinstance(value.get("role"), str):
            surface.role = value["role"]
        self._decorate(surface)

    def _decorate(self, surface):
        """What the drawers need besides the nodes: the `o` role, the hello app and the pane's directory."""
        surface.doc.role = surface.role
        surface.doc.app = self.hello_app or None
        if self._get_cwd is not None:
            try:
                surface.doc.cwd = self._get_cwd() or None
            except Exception:
                surface.doc.cwd = None

    def _replace_live(self, mode):
        """A new ``o`` replaces the buffer's live surface of the same mode (inline and flow: closed as if keep:true,
        screen: discarded); a new inline ``o`` also discards a screen surface still covering the buffer."""
        alt = self._alt()
        for surface in list(self._surfaces):
            if surface.closed:
                continue
            if surface.mode == mode and (mode != "screen" or surface.alt == alt):
                self._close_surface(surface, True)
            elif mode == "inline" and surface.mode == "screen" and surface.alt == alt:
                self._discard(surface)

    def _adopt(self, surface_id, value, listen):
        live = self._find(surface_id, open_only=True)
        if live is not None and live.mode == "inline":
            return                      # adopting the id that is already live does nothing
        target = None
        for surface in reversed(self._surfaces):
            if surface.id == surface_id and surface.closed and surface.mode == "inline":
                target = surface
                break
        if target is None:
            if listen:
                self._answer("e", {"ev": "gone", "ids": [surface_id]})
            return
        self._replace_live("inline")
        target.closed = False
        target.doc.suspended = False
        self._apply_open_fields(target, value, listen)
        self._surfaces.remove(target)
        self._surfaces.append(target)
        self._told_gone.discard(surface_id)
        self._sync()
        if surface_id not in self._changed:
            self._changed.append(surface_id)
        if self._on_change:
            self._on_change(surface_id)

    def _handle_close(self, value):
        surface_id = value.get("id")
        if not isinstance(surface_id, str) or not surface_id:
            self._error("x needs a surface id")
            return
        surface = self._find(surface_id, open_only=True)
        if surface is None:
            return                      # already closed, or discarded by a prompt: nothing to say
        keep = value.get("keep", True) is not False
        self._close_surface(surface, keep)
        self._sync()
        self._enforce_budget()
        if self._on_change:
            self._on_change(surface_id)

    def shell_prompt(self):
        """OSC 133;A: the program is gone.  Inline and screen surfaces (closed ones too) are discarded, an open
        flow surface is closed in place; the hello's app and features are forgotten.  Returns text to draw."""
        with self._lock:
            for surface in list(self._surfaces):
                if surface.mode in ("inline", "screen"):
                    self._discard(surface)
                elif not surface.closed:
                    self._close_surface(surface, True)
            self._forget_program()
            self._sync()
            return self._take_kept()

    def process_exited(self):
        """The pane's process ended: every live surface closes (inline and flow as if keep:true, screen
        discarded).  Returns text to draw."""
        with self._lock:
            for surface in list(self._surfaces):
                if not surface.closed:
                    self._close_surface(surface, True)
            self._forget_program()
            self._sync()
            return self._take_kept()

    def reset(self):
        """RIS (ESC c): the screen and its surfaces go; half-received chunks are dropped silently."""
        with self._lock:
            del self._surfaces[:]
            del self._changed[:]
            self._reader.reset()
            self._forget_program()
            self._sync()
            del self._kept_text[:]

    def _forget_program(self):
        self._program_gone = True
        self._quiet = False
        self.hello_seen = False
        self.hello_app = None
        self.hello_features = ()
        self.hello_ver = None

    def _take_kept(self):
        text = "".join(self._kept_text)
        del self._kept_text[:]
        return text

    def alt_screen_left(self):
        """The alternate screen ended: a screen surface opened on it is discarded and a listening program hears
        {"ev":"gone","sf":id,"ids":[id]}."""
        with self._lock:
            for surface in list(self._surfaces):
                if surface.mode == "screen" and surface.alt and not surface.closed:
                    self._discard(surface)
                    if surface.listen:
                        self._answer("e", {"ev": "gone", "sf": surface.id, "ids": [surface.id]})
            self._sync()

    def anchor_lost(self, surface_id):
        """The anchor row of the newest inline or flow surface ``surface_id`` left the scrollback (scrolled out,
        erased or cleared): the surface goes, and an open one whose program listens hears ``gone``."""
        with self._lock:
            for surface in reversed(self._surfaces):
                if surface.id == surface_id and surface.mode != "screen":
                    was_open = not surface.closed
                    self._discard(surface)
                    if was_open and surface.listen:
                        self._answer("e", {"ev": "gone", "sf": surface_id, "ids": [surface_id]})
                    break
            self._sync()

    # ----- palette and stylesheets ------------------------------------------------

    def _target(self, value):
        """(surface, error text): the surface a `t` or `s` is for (the live one when `sf` is absent)."""
        surface_id = value.get("sf")
        if surface_id is None:
            surface = self._live()
            return surface, None if surface is not None else "no live surface"
        surface = self._find(surface_id)
        return surface, None if surface is not None else "unknown surface %s" % (surface_id,)

    def _handle_palette(self, value):
        """A program palette (`t`) replaces the surface's previous one whole and is drawn at once."""
        surface, problem = self._target(value)
        if surface is None:
            if isinstance(value.get("sf"), str):
                self._stashed_palettes[value["sf"]] = value     # applied if a surface of that id opens later
            self._error("palette for an unknown surface: %s" % problem,
                        **({"sf": value["sf"]} if isinstance(value.get("sf"), str) else {}))
            return
        surface.palette = {key: item for key, item in value.items() if key != "sf"}
        surface.palette_bytes = len(json.dumps(surface.palette))
        self._sync()
        self._enforce_budget()
        self._changed_surface(surface.id)

    def _handle_sheet(self, value):
        """A stylesheet (`s`): kept with the surface (the live one when `sf` is absent)."""
        name = value.get("name")
        sheet = name if isinstance(name, str) else str(name)
        surface, problem = self._target(value)
        if surface is None:
            self._error(problem, sheet=sheet, **({"sf": value["sf"]} if isinstance(value.get("sf"), str) else {}))
            return
        css = value.get("css")
        previous = surface.doc.sheets.get(name) if isinstance(name, str) else None
        error = surface.doc.set_sheet(name, css)
        if error:
            self._error(error, surface, sheet=sheet)
            return
        if css and css != previous:
            problems = check_css(css)
            if problems:
                self._error("; ".join(problems), surface, sheet=sheet)
        self._enforce_budget()
        self._changed_surface(surface.id)

    def _changed_surface(self, surface_id):
        if surface_id not in self._changed:
            self._changed.append(surface_id)
        if self._on_change:
            self._on_change(surface_id)

    # ----- frames -------------------------------------------------------------------

    def _handle_frame(self, value):
        surface_id = value.get("sf")
        sequence = value.get("s")
        extra = {"s": sequence} if sequence is not None else {}
        if not isinstance(surface_id, str) or not surface_id:
            self._error("frame without a surface (sf)", **extra)
            return
        surface = self._find(surface_id, open_only=True)
        if surface is None:
            self._frame_for_unknown(surface_id, extra)
            return
        if not isinstance(value.get("ops"), list):
            self._error("frame ops must be an array", surface, **extra)
            return
        document = surface.doc
        self._decorate(surface)
        for error in document.apply_frame(value):
            self.errors.append(error)
            del self.errors[:-20]       # keep only the newest 20
            self._error(error["msg"], surface, s=error["s"], op=error["op"])
        for problem in document.take_node_errors():
            self._error(problem["msg"], surface, id=problem["id"])
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            surface.last_s = sequence
            surface.pending_ack = sequence
        self._enforce_budget()
        self._changed_surface(surface_id)

    def _frame_for_unknown(self, surface_id, extra):
        closed = self._find(surface_id)
        if closed is not None:
            self._error("surface %s is closed" % surface_id, closed, sf=surface_id, **extra)
        elif surface_id in self._ever or surface_id in self._told_gone:
            self._error("unknown surface %s" % surface_id, sf=surface_id, **extra)
        else:
            # A frame for a surface this host never opened: the program has been running since before
            # this host existed (a tab reattached after Sublime restarted).  Say once that the surface
            # is gone; the program then opens a new one and sends the whole document again.
            self._told_gone.add(surface_id)
            self._answer("e", {"ev": "gone", "ids": [surface_id]})

    def _flush_acks(self):
        """One ack per surface for the highest frame applied: it covers every frame up to it."""
        for surface in self._surfaces:
            if surface.pending_ack is None:
                continue
            number, surface.pending_ack = surface.pending_ack, None
            if surface.listen and not surface.closed:
                self._reply("e", ack_event(surface.id, number))

    # ----- retention -----------------------------------------------------------------

    def _enforce_budget(self):
        """Over the pane's budget: evict closed surfaces whole (oldest first), then the oldest settled
        children of ``main`` of open surfaces, reporting each with a `gone` event."""
        total = sum(surface.size() for surface in self._surfaces)
        if total <= self._budget:
            return
        for surface in list(self._surfaces):
            if total <= self._budget:
                break
            if surface.closed:
                total -= surface.size()
                self._discard(surface)
        gone = []
        for surface in self._surfaces:
            if total <= self._budget:
                break
            if surface.closed:
                continue
            dropped = []
            for child_id in surface.doc.settled_children():
                if total <= self._budget:
                    break
                total -= surface.doc.drop(child_id)
                dropped.append(child_id)
            if dropped:
                gone.append((surface, dropped))
        self._sync()
        for surface, ids in gone:
            if surface.listen:
                self._answer("e", {"ev": "gone", "sf": surface.id, "ids": ids})

    # ----- blobs ----------------------------------------------------------------------

    def _blob_path(self, blob_id):
        return os.path.join(self._blob_dir, blob_id) if self._blob_dir else None

    def _trim_disk(self):
        if self._disk_trimmed or not self._blob_dir:
            return
        self._disk_trimmed = True
        try:
            entries = []
            for name in os.listdir(self._blob_dir):
                if _SHA256.match(name):
                    path = os.path.join(self._blob_dir, name)
                    stat = os.stat(path)
                    entries.append((stat.st_mtime, stat.st_size, path))
            total = sum(entry[1] for entry in entries)
            for _mtime, size, path in sorted(entries):
                if total <= BLOBS_ON_DISK:
                    break
                os.remove(path)
                if os.path.exists(path + ".mime"):
                    os.remove(path + ".mime")
                total -= size
        except OSError:
            pass

    def _blob_known(self, blob_id):
        """True when the blob is in memory or on disk (a disk one is loaded); asking counts as a use."""
        self._trim_disk()
        if blob_id in self.blobs:
            self.blobs.move_to_end(blob_id)
            return True
        path = self._blob_path(blob_id)
        if not path:
            return False
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            if hashlib.sha256(data).hexdigest() != blob_id:
                return False
            mime = None
            if os.path.exists(path + ".mime"):
                with open(path + ".mime", "r", encoding="utf-8") as handle:
                    mime = handle.read(200).strip() or None
            os.utime(path, None)
        except OSError:
            return False
        self._store_blob(blob_id, base64.b64encode(data).decode("ascii"), len(data), mime)
        return True

    def _store_blob(self, blob_id, text, size, mime):
        if blob_id in self.blobs:
            self._blob_bytes -= self._blob_sizes.get(blob_id, 0)
        self.blobs[blob_id] = text
        self.blobs.move_to_end(blob_id)
        self._blob_sizes[blob_id] = size
        self._blob_bytes += size
        if mime:
            self.blob_mimes[blob_id] = mime
        self._evict_blobs()

    def _evict_blobs(self):
        if self._blob_bytes <= BLOBS_IN_MEMORY:
            return
        referenced = self._referenced_blobs()
        for blob_id in list(self.blobs):
            if self._blob_bytes <= BLOBS_IN_MEMORY:
                break
            if blob_id not in referenced:
                self._blob_bytes -= self._blob_sizes.pop(blob_id, 0)
                del self.blobs[blob_id]
                self.blob_mimes.pop(blob_id, None)

    def _referenced_blobs(self):
        found = set()
        for surface in self._surfaces:
            stack = [surface.doc.snapshot(drawn=False)]
            while stack:
                node = stack.pop()
                blob = (node.get("p") or {}).get("blob")
                if isinstance(blob, str):
                    found.add(blob.lower())
                stack.extend(node.get("c") or [])
        return found

    def _handle_blob(self, body, params):
        blob_id = params.get("id")
        if not isinstance(blob_id, str) or not _SHA256.match(blob_id):
            self._error("blob without a valid id (the SHA-256 of its bytes, 64 hex digits)")
            return
        blob_id = blob_id.lower()
        text = body.strip()
        if len(text) * 3 // 4 > MAX_BLOB_BYTES + 3:
            self._error("blob %s over %d bytes" % (blob_id, MAX_BLOB_BYTES))
            return
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            self._error("blob %s is not valid base64" % blob_id)
            return
        if len(data) > MAX_BLOB_BYTES:
            self._error("blob %s over %d bytes" % (blob_id, MAX_BLOB_BYTES))
            return
        if hashlib.sha256(data).hexdigest() != blob_id:
            self._error("blob bytes do not match id %s" % blob_id)
            return
        mime = params.get("mime")
        self._store_blob(blob_id, text, len(data), mime if mime else None)
        path = self._blob_path(blob_id)
        if path:
            try:
                os.makedirs(self._blob_dir, exist_ok=True)
                if not os.path.exists(path):
                    with open(path, "wb") as handle:
                        handle.write(data)
                if mime:
                    with open(path + ".mime", "w", encoding="utf-8") as handle:
                        handle.write(mime)
            except OSError:
                pass                    # the disk copy is a convenience; memory has it
        self._changed_blob_arrived(blob_id)

    def _changed_blob_arrived(self, blob_id):
        """A blob that arrives after an image drew shows on the node's next change: ask for a redraw."""
        surface = self._active()
        if surface is not None and self._on_change and blob_id in self._referenced_blobs():
            self._changed_surface(surface.id)

    # ----- events to the program ----------------------------------------------------------

    def send_event(self, event):
        """Send a terminal -> program event (a click, a view or an editing event) to the program.

        Events about a surface go only while that surface is open, listening and not suspended; events the
        terminal originates go only while some surface is open and listening.  ``resize`` and ``visible``
        get the active surface's id when they have none, ``theme`` and ``motion`` carry none; their values are
        also what later hello replies report.  ``edit``, ``undo`` and ``send`` go only to a program that
        announced the feature in its hello.

        A ``change`` click on a form control of an ``el`` node is completed here: the document flips the control
        (a radio unchecks its group) and the event gets the control's value, name and the form's ``values``;
        ``action``, ``select``, ``activate`` and ``change`` from inside a form carry the form's ``values``."""
        with self._lock:
            event = dict(event)
            kind = event.get("ev")
            if kind == "theme":
                self.dark = bool(event.get("dark"))
                event.pop("sf", None)
                if not self._any_listening():
                    return
                self._emit(event)
                return
            if kind == "motion":
                self.reduce_motion = bool(event.get("reduce"))
                event.pop("sf", None)
                if not self._any_listening():
                    return
                self._emit(event)
                return
            if kind in _EVENT_FEATURES and _EVENT_FEATURES[kind] not in self.hello_features:
                return
            if kind == "resize":
                if isinstance(event.get("cols"), int):
                    self.cols = event["cols"]
                if isinstance(event.get("cell"), dict):
                    self.cell = event["cell"]
            elif kind == "visible":
                self.visible = bool(event.get("visible"))
            if kind in ("resize", "visible") and event.get("sf") is None:
                active = self._active()
                if active is None:
                    return
                event["sf"] = active.id
            surface = self._find(event.get("sf"), open_only=True)
            if surface is None or not surface.listen or surface.doc.suspended:
                return
            document = surface.doc
            if kind == "change" and "item" not in event:
                # A click on an `el` checkbox or radio; a `prefs` row's change (it names its `item`) is the
                # program's own and goes through as it is.
                fields = document.click_control(event.get("id"))
                if fields is None:
                    return
                event.update(fields)
                self._changed_surface(surface.id)
            if kind in ("action", "select", "activate", "change") and "values" not in event:
                values = document.form_values(event.get("id"))
                if values is not None:
                    event["values"] = values
            self._emit(event)

    def _emit(self, event):
        self.events_sent.append(event)
        del self.events_sent[:-50]      # keep the last 50 for diagnosis
        self._reply("e", event)

    # ----- the screen text ------------------------------------------------------------------

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

    # ----- snapshot and replay ---------------------------------------------------------------

    def snapshot_messages(self, apc=DEFAULT_APC_LIMIT):
        """The messages that rebuild this host's state in a new host, as program -> terminal APC strings:
        the hello, each surface (closed ones first) as an `o`, its palette `t`, its sheets `s` and one frame
        adding the whole document, then `settle`s, the focus and `suspend`, and the `x` of closed surfaces.
        Feed them to ``replay`` of another host.  Blobs are not included (they live on disk by id)."""
        with self._lock:
            out = []

            def put(verb, value):
                body = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
                out.extend(encode_program_message(verb, None, body, apc, "snap%d" % len(out)))

            if self.hello_seen:
                hello = {"q": "hello", "v": [1]}
                if self.hello_app:
                    hello["app"] = self.hello_app
                if self.hello_ver:
                    hello["ver"] = self.hello_ver
                if self.hello_features:
                    hello["features"] = list(self.hello_features)
                put("q", hello)
            ordered = [s for s in self._surfaces if s.closed] + [s for s in self._surfaces if not s.closed]
            for surface in ordered:
                opened = {"id": surface.id, "mode": surface.mode}
                if surface.title:
                    opened["title"] = surface.title
                if surface.role:
                    opened["role"] = surface.role
                if not surface.listen:
                    opened["listen"] = False
                put("o", opened)
                if surface.palette is not None:
                    put("t", dict(surface.palette, sf=surface.id))
                for name, css in surface.doc.sheets.items():
                    put("s", {"sf": surface.id, "name": name, "css": css})
                document = surface.doc
                regions = document.snapshot(drawn=False).get("c") or []
                ops = [["add", region["id"], surface.id, None, region] for region in regions]
                ops.extend(["settle", node_id] for node_id in sorted(document.settled))
                if document.focus is not None:
                    ops.append(["focus", document.focus])
                if document.suspended:
                    ops.append(["suspend"])
                put("f", {"sf": surface.id, "s": surface.last_s if surface.last_s is not None else 0, "ops": ops})
                if surface.closed:
                    put("x", {"id": surface.id, "keep": True})
            return out

    def replay(self, text):
        """Feed a snapshot (see ``snapshot_messages``) into this host: the normal parser takes it, and none of
        the host's answers (acks, errors, replies) reach the program."""
        with self._lock:
            saved = (self._quiet, self._program_gone)
            self._replaying = True
            try:
                self.filter(text)
            finally:
                self._replaying = False
                self._quiet, self._program_gone = saved
                del self._kept_text[:]
