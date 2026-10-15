"""Tern Surface Protocol (TSP) message codec: bytes on the pty <-> Python values.

This is the INPUT and RETURN-PATH block of GhostShell's TSP support.
A program (omp) describes its UI to the terminal with messages shaped like

    ESC _ tsp ; <verb> [; <key>=<value>]* ; <body> ESC \\

(an "APC string").  The body is JSON text, or base64 for image blobs.
Program -> terminal verbs:  q query, o open, f frame, b blob, t palette, s stylesheet, x close.
Terminal -> program verbs:  r reply, e event.

Framing follows docs.stencil.so/tern/transport: ST (ESC \\) or BEL ends a string, a segment after the verb is a
parameter only when it looks like key=value AND another ";" follows, messages larger than the hello's ``apc`` are
chunked (``c`` chunk id, ``m=1`` on all but the last), one chunk sequence is joined at a time and is dropped with an
error when interrupted, idle for one second or larger than 24 MiB, and an APC string longer than 262 144 bytes is
abandoned silently.

Pure functions and one small class.  No Sublime, no I/O, so it can be read and run on its own.
"""

import json
import re
import time

TSP_VERSION = 1                     # protocol version this host speaks
TSP_PREFIX = "\x1b_tsp;"            # every TSP message starts with this (APC "tsp")
APC_END = "\x1b\\"                  # ST, the string terminator that ends an APC
BEL = "\x07"                        # the parser also ends any string here, as kitty does
# Windows ConPTY drops APC strings on the way to the program, so terminal ->
# program messages are wrapped as OSC 877 there instead; omp converts them back.
TSP_OSC_PREFIX = "\x1b]877;tsp;"
DEFAULT_APC_LIMIT = 65536           # largest body before chunking (hello may change it)
MAX_APC_STRING = 262144             # a longer APC string is abandoned without an error
MAX_CHUNKED_BYTES = 24 * 1024 * 1024    # a chunk sequence whose joined body passes this is dropped
CHUNK_IDLE_SECONDS = 1.0            # a chunk sequence idle this long is dropped
ERROR_VERB = "!"                    # pseudo verb: the reader reports a dropped chunk sequence in the message stream

# A "k=v" parameter: ASCII, never contains ";", no spaces.
_PARAM_PATTERN = re.compile(r"^[A-Za-z0-9_-]+=[\x21-\x3a\x3c-\x7e]*$")
# A chunk body that starts like this might begin with a parameter-shaped segment (see encode_program_message).
_PARAM_START = re.compile(r"[A-Za-z0-9_-]+=")


def split_message(sequence):
    """Split one complete ``ESC _ tsp;... ST`` (or BEL terminated) string.

    Returns (verb, params, body) or None when it is not a TSP message.
    ``params`` is a dict of str -> str (for example chunk id ``c`` and the
    "more chunks follow" flag ``m``).
    """
    if not sequence.startswith(TSP_PREFIX):
        return None
    if sequence.endswith(APC_END):
        inner = sequence[len(TSP_PREFIX):-len(APC_END)]
    elif sequence.endswith(BEL):
        inner = sequence[len(TSP_PREFIX):-len(BEL)]
    else:
        return None
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
        params.setdefault(key, value)
        pos = semi + 1
    return (verb, params, inner[pos:])


class TspReader:
    """Turns a stream of pty output into decoded program -> terminal messages.

    ``feed_text`` takes raw text from the pty (any slicing: a message may be
    split across calls) and returns (messages, passthrough_text).
    ``passthrough_text`` is everything that was not TSP, to be drawn as usual.
    Each message is a (verb, body, params) tuple where ``body`` has had chunks joined
    and ``params`` holds the k=v parameters of the first chunk (a blob's ``id``).
    A dropped chunk sequence shows up in the stream as (ERROR_VERB, message, {}) in the order it happened.
    Unknown verbs are passed on and ignored by the caller (the protocol says unknown verbs are ignored).
    """

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._pending = ""      # text held back because it may be an unfinished message
        self._seq = None        # the chunk sequence being joined: [verb, chunk id, body parts, params, bytes, last time]
        self._skip_id = None    # chunk id of a sequence just dropped: its remaining chunks are swallowed

    # ----- feeding ---------------------------------------------------------

    def feed_text(self, text):
        messages = []
        self._poll(messages)
        data = self._pending + text
        self._pending = ""
        out = []
        at = 0
        while True:
            start = data.find(TSP_PREFIX, at)
            if start == -1:
                break
            end, end_len = _find_end(data, start + len(TSP_PREFIX))
            if end == -1:
                # Unfinished message: keep it for the next feed, unless it is over the size a parser accepts.
                rest = data[start:]
                if _byte_length(rest) > MAX_APC_STRING:
                    # Abandoned without an error; what follows shows on the screen as text.
                    out.append(data[at:start])
                    data = ""
                    at = 0
                    break
                out.append(data[at:start])
                self._pending = rest
                return messages, "".join(out)
            out.append(data[at:start])
            body_text = data[start:end + end_len]
            if _byte_length(body_text) - len(TSP_PREFIX) - end_len > MAX_APC_STRING:
                # Longer than the parser takes: abandoned; the part past the limit shows as text.
                out.append(_text_after_bytes(body_text, MAX_APC_STRING + 2))
            else:
                self._take(body_text, messages)
            at = end + end_len
        # The tail may end inside the 6-character prefix: hold that back too.
        tail = data[at:]
        hold = _partial_prefix_length(tail)
        if hold:
            self._pending = tail[-hold:]
            tail = tail[:-hold]
        out.append(tail)
        return messages, "".join(out)

    def poll(self):
        """Messages (errors only) for a chunk sequence that has been idle too long; call on a timer."""
        messages = []
        self._poll(messages)
        return messages

    def waiting(self):
        """True while a chunk sequence is being joined (a timer should check it within a second)."""
        return self._seq is not None

    def reset(self):
        """RIS: forget everything half-received, silently."""
        self._pending = ""
        self._seq = None
        self._skip_id = None

    # ----- internals -------------------------------------------------------

    def _poll(self, messages):
        seq = self._seq
        if seq is not None and self._clock() - seq[5] > CHUNK_IDLE_SECONDS:
            self._drop(messages, "timed out")

    def _drop(self, messages, reason):
        seq = self._seq
        self._seq = None
        self._skip_id = seq[1]
        messages.append((ERROR_VERB, "chunked %s message c=%s dropped: %s" % (seq[0], seq[1], reason), {}))

    def _take(self, sequence, messages):
        parts = split_message(sequence)
        if parts is None:
            return
        verb, params, body = parts
        chunk_id = params.get("c")
        seq = self._seq
        if seq is not None and not (chunk_id == seq[1] and verb == seq[0]):
            # Any TSP message but the next chunk interrupts the sequence; it is still handled on its own.
            self._drop(messages, "interrupted")
            if chunk_id == seq[1]:
                self._skip_id = None        # same id under another verb: a new sequence, not the old one's rest
            seq = None
        if chunk_id is None:
            messages.append((verb, body, params))
            return
        if seq is None and chunk_id == self._skip_id:
            # The rest of a sequence that was just dropped.
            if params.get("m") != "1":
                self._skip_id = None
            return
        self._skip_id = None
        if seq is None:
            seq = self._seq = [verb, chunk_id, [], params, 0, self._clock()]
        seq[2].append(body)
        seq[4] += _byte_length(body)
        seq[5] = self._clock()
        if seq[4] > MAX_CHUNKED_BYTES:
            self._drop(messages, "over %d bytes" % MAX_CHUNKED_BYTES)
            if params.get("m") != "1":
                self._skip_id = None
            return
        if params.get("m") == "1":
            return
        self._seq = None
        messages.append((verb, "".join(seq[2]), seq[3]))


def _find_end(data, from_index):
    """(index, length) of the first string terminator (ST or BEL) at or after ``from_index``, or (-1, 0)."""
    st = data.find(APC_END, from_index)
    bel = data.find(BEL, from_index)
    if st == -1 and bel == -1:
        return -1, 0
    if bel == -1 or (st != -1 and st < bel):
        return st, len(APC_END)
    return bel, len(BEL)


def _byte_length(text):
    return len(text) if text.isascii() else len(text.encode("utf-8", "replace"))


def _text_after_bytes(text, count):
    """The part of ``text`` after its first ``count`` UTF-8 bytes (character aligned)."""
    if text.isascii():
        return text[count:]
    total = 0
    for index, char in enumerate(text):
        total += len(char.encode("utf-8", "replace"))
        if total > count:
            return text[index:]
    return ""


def _partial_prefix_length(text):
    """Length of the longest suffix of ``text`` that starts the TSP prefix."""
    for size in range(min(len(TSP_PREFIX) - 1, len(text)), 0, -1):
        if text.endswith(TSP_PREFIX[:size]):
            return size
    return 0


def encode_message(verb, value, windows_conpty=False):
    """Encode one terminal -> program message (a reply ``r`` or an event ``e``).

    ``value`` is a dict and is sent as JSON.  With ``windows_conpty`` the OSC 877
    form is used because ConPTY discards APC on the input side.  Terminal -> program
    messages are never chunked.
    """
    body = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if windows_conpty:
        # ConPTY's input parser takes C1 controls inside an OSC string, and U+009C would end it:
        # those code points travel as their JSON escapes.
        body = "".join("\\u%04x" % ord(ch) if "\x80" <= ch <= "\x9f" else ch for ch in body)
        return "%s%s;%s%s" % (TSP_OSC_PREFIX, verb, body, APC_END)
    return "%s%s;%s%s" % (TSP_PREFIX, verb, body, APC_END)


def _starts_with_parameter(body, index):
    """True when a chunk starting at ``index`` would begin with a parameter-shaped segment (key=value;)."""
    if _PARAM_START.match(body, index, index + 256) is None:   # a key longer than that is not looked for
        return False
    semi = body.find(";", index)
    return semi != -1 and _PARAM_PATTERN.match(body[index:semi]) is not None


def encode_program_message(verb, params, body, apc=DEFAULT_APC_LIMIT, chunk_id="c1"):
    """Encode one program -> terminal message (always APC), chunked at ``apc`` bytes of body.

    ``params`` is a dict of k=v parameters (a blob's id and mime).  Returns a list of strings, one per
    chunk.  Split points follow the transport rule: a chunk never starts with a parameter-shaped segment,
    so a split falls right before ``;`` or before any character outside A-Z a-z 0-9 _ -.
    """
    head = "".join("%s=%s;" % (key, value) for key, value in (params or {}).items())
    if _byte_length(body) <= apc:
        return ["%s%s;%s%s%s" % (TSP_PREFIX, verb, head, body, APC_END)]
    pieces = []
    at = 0
    ascii_body = body.isascii()
    while at < len(body):
        if ascii_body:
            end = min(len(body), at + apc)
        else:
            # Longest run that fits in ``apc`` bytes.
            end = at
            used = 0
            while end < len(body) and used + len(body[end].encode("utf-8", "replace")) <= apc:
                used += len(body[end].encode("utf-8", "replace"))
                end += 1
        if end < len(body):
            # Move the split back until the next chunk would not start with a parameter-shaped segment.
            while end > at + 1 and _starts_with_parameter(body, end):
                end -= 1
        pieces.append(body[at:end])
        at = end
    out = []
    for index, piece in enumerate(pieces):
        last = index == len(pieces) - 1
        chunk_params = "c=%s;" % chunk_id + ("" if last else "m=1;")
        first = head if index == 0 else ""
        out.append("%s%s;%s%s%s%s" % (TSP_PREFIX, verb, chunk_params, first, piece, APC_END))
    return out


def hello_reply(kinds, features=(), cols=None, cell=None, dark=True,
                reduce_motion=False, apc=None, credits=None, term="ghostshell",
                hour12=None, ver=None):
    """Build the reply to a program's ``hello`` query.

    ``kinds`` lists the component kinds this host draws; any kind left out
    makes the program send pre-rendered ``rows`` for it instead.  Optional fields are
    left out when None.
    """
    reply = {"r": "hello", "v": TSP_VERSION, "term": term}
    if ver is not None:
        reply["ver"] = ver
    reply["kinds"] = list(kinds)
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
    if hour12 is not None:
        reply["hour12"] = bool(hour12)
    return reply


def ack_event(surface_id, sequence):
    """The event that acknowledges frame ``sequence`` of surface ``surface_id``.

    A program holds back new frames when too many are unacknowledged (default 2), so
    frames must be acked or the UI stalls for about 5 seconds.  An ack covers every frame up to it.
    """
    return {"ev": "ack", "sf": surface_id, "s": sequence}
