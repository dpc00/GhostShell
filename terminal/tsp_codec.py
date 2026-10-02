"""Tern Surface Protocol (TSP) message codec: bytes on the pty <-> Python values.

This is the INPUT and RETURN-PATH block of GhostShell's TSP support.
A program (omp) describes its UI to the terminal with messages shaped like

    ESC _ tsp ; <verb> [; <key>=<value>]* ; <body> ESC \\

(an "APC string").  The body is JSON text, or base64 for image blobs.
Program -> terminal verbs:  q query, o open, f frame, b blob, t palette, x close.
Terminal -> program verbs:  r reply, e event.

VERIFIED against omp source (github.com/can1357/oh-my-pi, packages/tui/src/
native/encode.ts and packages/wire/src/tsp.ts, main branch, 2026-10-01).

Pure functions and one small class.  No Sublime, no I/O, so it can be read and
checked on its own.
"""

import json
import re

TSP_VERSION = 1                     # protocol version this host speaks
TSP_PREFIX = "\x1b_tsp;"            # every TSP message starts with this (APC "tsp")
APC_END = "\x1b\\"                  # ST, the string terminator that ends an APC
# Windows ConPTY drops APC strings on the way to the program, so terminal ->
# program messages are wrapped as OSC 877 there instead; omp converts them back.
TSP_OSC_PREFIX = "\x1b]877;tsp;"
DEFAULT_APC_LIMIT = 65536           # largest body before chunking (hello may change it)

# A "k=v" parameter: ASCII, never contains ";".
_PARAM_PATTERN = re.compile(r"^[A-Za-z0-9_-]+=[\x21-\x3a\x3c-\x7e]*$")


def split_message(sequence):
    """Split one complete ``ESC _ tsp;... ESC \\`` string.

    Returns (verb, params, body) or None when it is not a TSP message.
    ``params`` is a dict of str -> str (for example chunk id ``c`` and the
    "more chunks follow" flag ``m``).
    """
    if not sequence.startswith(TSP_PREFIX) or not sequence.endswith(APC_END):
        return None
    inner = sequence[len(TSP_PREFIX):-len(APC_END)]
    semi = inner.find(";")
    if semi <= 0:
        # No second ";": a bare verb with an empty body, or garbage.
        return (inner, {}, "") if semi == -1 and inner else None
    verb = inner[:semi]
    params = {}
    pos = semi + 1
    # A segment is a parameter only when another ";" follows it and it looks
    # like k=v; the body is everything after the last parameter.
    while True:
        semi = inner.find(";", pos)
        if semi == -1:
            break
        segment = inner[pos:semi]
        if not _PARAM_PATTERN.match(segment):
            break
        key, _, value = segment.partition("=")
        params[key] = value
        pos = semi + 1
    return (verb, params, inner[pos:])


class TspReader:
    """Turns a stream of pty output into decoded program -> terminal messages.

    ``feed_text`` takes raw text from the pty (any slicing: a message may be
    split across calls) and returns (messages, passthrough_text).
    ``passthrough_text`` is everything that was not TSP, to be drawn as usual.
    Each message is a (verb, body, params) tuple where ``body`` has had chunks joined
    and ``params`` holds the k=v parameters of the last chunk (a blob's ``id``).
    Malformed messages are dropped; unknown verbs are passed on and ignored by
    the caller (the protocol says unknown verbs and fields are ignored).
    """

    def __init__(self):
        self._pending = ""      # text held back because it may be an unfinished message
        self._chunks = {}       # chunk id -> body so far (large bodies are split)

    def feed_text(self, text):
        data = self._pending + text
        self._pending = ""
        messages = []
        out = []
        at = 0
        while True:
            start = data.find(TSP_PREFIX, at)
            if start == -1:
                break
            end = data.find(APC_END, start)
            if end == -1:
                # Unfinished message: keep it for the next feed.
                out.append(data[at:start])
                self._pending = data[start:]
                return messages, "".join(out)
            out.append(data[at:start])
            message = self._decode(data[start:end + len(APC_END)])
            if message is not None:
                messages.append(message)
            at = end + len(APC_END)
        # The tail may end inside the 6-character prefix: hold that back too.
        tail = data[at:]
        hold = _partial_prefix_length(tail)
        if hold:
            self._pending = tail[-hold:]
            tail = tail[:-hold]
        out.append(tail)
        return messages, "".join(out)

    def _decode(self, sequence):
        parts = split_message(sequence)
        if parts is None:
            return None
        verb, params, body = parts
        chunk_id = params.get("c")
        if chunk_id is not None:
            joined = self._chunks.get(chunk_id, "") + body
            if params.get("m") == "1":
                self._chunks[chunk_id] = joined
                return None
            self._chunks.pop(chunk_id, None)
            body = joined
        return (verb, body, params)


def _partial_prefix_length(text):
    """Length of the longest suffix of ``text`` that starts the TSP prefix."""
    for size in range(min(len(TSP_PREFIX) - 1, len(text)), 0, -1):
        if text.endswith(TSP_PREFIX[:size]):
            return size
    return 0


def encode_message(verb, value, windows_conpty=False):
    """Encode one terminal -> program message (a reply ``r`` or an event ``e``).

    ``value`` is a dict and is sent as JSON.  With ``windows_conpty`` the OSC 877
    form is used because ConPTY discards APC on the input side.
    """
    body = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if windows_conpty:
        return "%s%s;%s%s" % (TSP_OSC_PREFIX, verb, body, APC_END)
    return "%s%s;%s%s" % (TSP_PREFIX, verb, body, APC_END)


def hello_reply(kinds, features=(), cols=None, cell=None, dark=True,
                reduce_motion=False, apc=None, credits=None, term="ghostshell"):
    """Build the reply to omp's ``hello`` query.

    ``kinds`` lists the component kinds this host draws; any kind left out
    makes omp send pre-rendered ``rows`` for it instead.  Optional fields are
    left out when None so omp uses its defaults (apc 65536, credits 2).
    """
    reply = {"r": "hello", "v": TSP_VERSION, "term": term, "kinds": list(kinds)}
    if features:
        reply["features"] = list(features)
    if apc is not None:
        reply["apc"] = apc
    if credits is not None:
        reply["credits"] = credits
    if cols is not None:
        reply["cols"] = cols
    if cell is not None:
        reply["cell"] = cell
    reply["dark"] = bool(dark)
    reply["reduceMotion"] = bool(reduce_motion)
    return reply


def ack_event(surface_id, sequence):
    """The event that acknowledges frame ``sequence`` of surface ``surface_id``.

    omp holds back new frames when too many are unacknowledged (default 2), so
    every frame must be acked or the UI stalls for about 5 seconds.
    """
    return {"ev": "ack", "sf": surface_id, "s": sequence}
