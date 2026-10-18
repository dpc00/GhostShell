"""TSP text renderer: turns a TspDocument into plain lines of text.

This is the OUTPUT (SINK) block of GhostShell's TSP support.  GhostShell shows a
terminal as text in a Sublime view, so each component kind is drawn as its
plain-text equivalent: a card is a head line with its content indented under
it, a progress node is a bar made of characters, a table is a grid of aligned
columns, and so on.  Every kind of the vocabulary (the 44 official kinds plus
``media``) has a drawer; a kind this file does not know is drawn as its
children, so a newer omp never makes the screen go blank.

Each ``_draw_<kind>`` function takes a wire-shaped node dict (see
tsp_document.py) and the width in cells, and returns a list of strings.
Nothing here touches Sublime.

Rules every drawer follows:

* ``hidden`` nodes draw nothing;
* a drawer that raises never takes the page down: the node is drawn as its
  plain text instead (``_fallback``);
* lines never exceed the width (a cut ends in an ellipsis) and carry no control
  characters; widths are counted in terminal cells (East Asian wide characters
  count 2, combining marks 0);
* style tokens (colours, bold) are not drawn: this is plain text.

The one exception to "no escape sequences" is the caret of an ``editor`` /
``input``: it is the inverse of the character it sits on (and the selection and
the ghost completion are inverse / dim), written as the four SGR codes in
``_SGR_KEEP`` so that it takes no cell of its own.  Set ``ALLOW_SGR`` to False
for strictly escape-free output: the caret is then a ``▏`` bar.
"""

import re
import threading
import unicodedata

INDENT = "  "
ALLOW_SGR = True            # editor/input caret as SGR inverse (see the module docstring)

_ELL = "…"
_SGR_KEEP = ("\x1b[7m", "\x1b[27m", "\x1b[2m", "\x1b[22m")

_ESC_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"                  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"      # OSC, ended by BEL or ST
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"           # DCS, SOS, PM, APC
    r"|\x1b[@-Z\\-_]"                           # two-character escapes
    r"|\x1b")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")   # C0/C1 but not \t \n (handled before)
_BIDI_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_SGR_RE = re.compile(r"\x1b\[(?:7|27|2|22)m")
_SGR_SPLIT = re.compile(r"(\x1b\[(?:7|27|2|22)m)")

_MARKS = {"running": "●", "done": "✓", "error": "✗", "cancelled": "○", "pending": "◌"}


# ---------------------------------------------------------------- values

def _props(node):
    props = node.get("p") if isinstance(node, dict) else None
    return props if isinstance(props, dict) else {}


def _kids(node):
    kids = node.get("c") if isinstance(node, dict) else None
    return [kid for kid in kids if isinstance(kid, dict)] if isinstance(kids, (list, tuple)) else []


def _list(value):
    return list(value) if isinstance(value, (list, tuple)) else []


def _flag(props, key):
    return props.get(key) is True


def _get(table, key, default=None):
    """table.get(key), but a key that cannot be hashed (garbage in a prop) is just absent."""
    try:
        return table.get(key, default)
    except TypeError:
        return default


def _num(value, default=None):
    if isinstance(value, bool):
        return default
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return default
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            return default
        return value
    return default


def _int(value, default=None):
    number = _num(value)
    return default if number is None else int(number)


def _clamp01(value):
    return max(0.0, min(1.0, float(value)))


def _fmt_count(value):
    """A number as the pickers show it: 3, 272k, 1.6M."""
    number = _num(value)
    if number is None:
        return ""
    sign = "-" if number < 0 else ""
    number = abs(number)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if number >= limit:
            scaled = number / limit
            text = ("%d" % round(scaled)) if scaled >= 10 else ("%.1f" % scaled).rstrip("0").rstrip(".")
            return sign + text + suffix
    return sign + (("%d" % number) if number == int(number) else ("%.1f" % number).rstrip("0").rstrip("."))


def _format_age(milliseconds, clock=False):
    """12.3s / 4m 05s / 1h 02m, or with clock=True 0:12 / 4:05 / 1:02:03 (negative shows as zero)."""
    ms = int(max(0, _num(milliseconds, 0) or 0))
    if clock:
        hours, rest = divmod(ms // 1000, 3600)
        minutes, seconds = divmod(rest, 60)
        return "%d:%02d:%02d" % (hours, minutes, seconds) if hours else "%d:%02d" % (minutes, seconds)
    if ms < 60000:
        return "%d.%ds" % divmod(ms // 100, 10)
    hours, rest = divmod(ms // 1000, 3600)
    minutes, seconds = divmod(rest, 60)
    return "%dh %02dm" % (hours, minutes) if hours else "%dm %02ds" % (minutes, seconds)


# ---------------------------------------------------------------- text and width

_KEYCAPS = {
    "ctrl": "⌃", "control": "⌃", "alt": "⌥", "option": "⌥", "opt": "⌥", "shift": "⇧",
    "cmd": "⌘", "command": "⌘", "meta": "⌘", "super": "⌘", "win": "⌘",
    "enter": "⏎", "return": "⏎", "esc": "esc", "escape": "esc", "tab": "⇥", "backspace": "⌫",
    "delete": "⌦", "del": "⌦", "space": "␣", "up": "↑", "down": "↓", "left": "←", "right": "→",
    "pageup": "⇞", "pagedown": "⇟", "home": "↖", "end": "↘",
}
_KEY_ID = re.compile(r"^[A-Za-z0-9]+(?:\+[A-Za-z0-9]+)*$")


def _keycap(key):
    text = str(key)
    low = text.lower()
    if low in _KEYCAPS:
        return _KEYCAPS[low]
    if len(text) == 1:
        return text.upper()
    if re.match(r"^f\d+$", low):
        return text.upper()
    return text


def _span_text(span):
    if isinstance(span, str):
        return span
    if isinstance(span, dict):
        text = span.get("t")
        text = "" if text is None else (text if isinstance(text, str) else str(text))
        tokens = span.get("s")
        if isinstance(tokens, str) and tokens:
            tokens = tokens.split()
            if "hide" in tokens:
                return ""
            if "key" in tokens and _KEY_ID.match(text):
                return "".join(_keycap(key) for key in text.split("+"))
        return text
    return "" if span is None else str(span)


def plain(value):
    """Text given as a str or as a list of {"t": text, ...} spans, as one str."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _span_text(value)
    if isinstance(value, (list, tuple)):
        return "".join(_span_text(span) for span in value)
    return str(value)


def _clean(text):
    """Text with escape sequences, control characters and invisible bidi marks removed; tabs become spaces."""
    if not isinstance(text, str):
        text = str(text)
    if "\x1b" in text:
        text = _ESC_RE.sub("", text)
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\t" in text:
        text = text.replace("\t", "    ")
    return _BIDI_RE.sub("", _CTRL_RE.sub("", text))


def _t(value):
    """The clean text of a str or span list."""
    return _clean(plain(value))


def _one(text):
    """Text on one line."""
    return text.replace("\n", " ")


_CW = {}


def _cw(ch):
    width = _CW.get(ch)
    if width is None:
        code = ord(ch)
        if code < 0x20 or 0x7f <= code < 0xa0:
            width = 0
        elif code < 0x300:
            width = 1
        elif unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            width = 0
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            width = 2
        else:
            width = 1
        _CW[ch] = width
    return width


def _w(text):
    """Width in terminal cells (the editor's SGR codes take none)."""
    if "\x1b" in text:
        text = _SGR_RE.sub("", text)
    if text.isascii():
        return len(text)
    return sum(_cw(ch) for ch in text)


def _close_sgr(text):
    """Text with the inverse/dim switched off at its end when a cut left them on."""
    if "\x1b" not in text:
        return text
    if text.rfind("\x1b[7m") > text.rfind("\x1b[27m"):
        text += "\x1b[27m"
    if text.rfind("\x1b[2m") > text.rfind("\x1b[22m"):
        text += "\x1b[22m"
    return text


def _cut(text, cells):
    """(head, tail): head is the longest prefix that fits in `cells` cells."""
    cells = max(0, cells)
    if "\x1b" not in text:
        if text.isascii():
            return text[:cells], text[cells:]
        used = 0
        for index, ch in enumerate(text):
            used += _cw(ch)
            if used > cells:
                return text[:index], text[index:]
        return text, ""
    head, used = [], 0
    parts = _SGR_SPLIT.split(text)
    for number, part in enumerate(parts):
        if number % 2:
            head.append(part)
            continue
        for index, ch in enumerate(part):
            used += _cw(ch)
            if used > cells:
                head.append(part[:index])
                return _close_sgr("".join(head)), part[index:] + "".join(parts[number + 1:])
        head.append(part)
    return "".join(head), ""


def _trunc(text, width, where="end"):
    """Text cut to `width` cells with an ellipsis: end keeps the start, start the end, middle both."""
    if width < 1:
        return ""
    if _w(text) <= width:
        return text
    if width == 1:
        return _ELL
    keep = width - 1
    if where == "start" or where == "middle":
        plain_text = _SGR_RE.sub("", text)
        if where == "start":
            tail = ""
            for ch in reversed(plain_text):
                if _w(tail) + _cw(ch) > keep:
                    break
                tail = ch + tail
            return _ELL + tail
        left = (keep + 1) // 2
        head = _cut(plain_text, left)[0]
        tail = ""
        for ch in reversed(plain_text):
            if _w(tail) + _cw(ch) > keep - _w(head):
                break
            tail = ch + tail
        return head + _ELL + tail
    return _cut(text, keep)[0] + _ELL


def _pad(text, cells, align="start"):
    gap = cells - _w(text)
    if gap <= 0:
        return text
    if align == "end":
        return " " * gap + text
    if align == "center":
        return " " * (gap // 2) + text + " " * (gap - gap // 2)
    return text + " " * gap


_WIDE = 1000        # the width a node is drawn at when its lines are joined into one


def _hard_wrap(text, width):
    """Text cut into pieces of at most `width` cells, wherever that falls."""
    width = max(1, width)
    if _w(text) <= width:
        return [text]
    if "\x1b" not in text and text.isascii():
        return [text[start:start + width] for start in range(0, len(text), width)]
    pieces, current, used = [], [], 0
    for ch in text:
        size = _cw(ch)
        if current and used + size > width:
            pieces.append("".join(current))
            current, used = [], 0
        current.append(ch)
        used += size
    pieces.append("".join(current))
    return pieces


def _wrap_words(raw, width):
    if _w(raw) <= width:
        return [raw]
    body = raw.lstrip(" ")
    indent = raw[:len(raw) - len(body)]
    if _w(indent) > width // 2:
        indent = ""
    indent_w = _w(indent)
    lines, current, current_w, has, space = [], indent, indent_w, False, ""
    for token in re.findall(r"\s+|\S+", body):
        if token.isspace():
            space = token if has else ""
            continue
        token_w, space_w = _w(token), _w(space)
        if has and current_w + space_w + token_w <= width:
            current += space + token
            current_w += space_w + token_w
            space = ""
            continue
        if has:
            lines.append(current)
        space, has = "", False
        room = width - indent_w
        while token_w > room:
            head, token = _cut(token, room)
            if not head:
                head, token = token[0], token[1:]
            lines.append(indent + head)
            token_w = _w(token)
        current, current_w, has = indent + token, indent_w + token_w, bool(token)
    if has:
        lines.append(current)
    return lines or [""]


def _wrap(text, width, mode="word"):
    """Text wrapped to `width` cells: mode word (indentation kept), char (anywhere) or none (lines kept)."""
    width = max(1, width)
    lines = []
    for raw in text.split("\n"):
        raw = raw.rstrip()
        if mode == "none":
            lines.append(raw)
        elif mode == "char":
            lines.extend(piece.rstrip() for piece in _hard_wrap(raw, width))
        else:
            lines.extend(_wrap_words(raw, width))
    return lines


def _hang_wrap(text, width, hang="  "):
    """Text wrapped to `width`, continuation lines indented by `hang`."""
    width = max(1, width)
    first = _wrap_words(text, width)
    if len(first) == 1:
        return first
    rest = text[len(first[0]):].lstrip()
    room = max(1, width - _w(hang))
    return [first[0]] + [hang + line for line in _wrap(rest, room)]


def _clamp_lines(lines, count, width):
    """The first `count` lines, the last ending in an ellipsis when something was dropped."""
    if count <= 0 or len(lines) <= count:
        return lines
    kept = list(lines[:count])
    last = kept[-1].rstrip()
    if _w(last) >= width:
        last = _cut(last, width - 1)[0].rstrip()
    kept[-1] = last + _ELL
    return kept


def _indent(lines, prefix=INDENT):
    return [prefix + line if line else line for line in lines]


def _block_width(lines):
    return max((_w(line) for line in lines), default=0)


# ---------------------------------------------------------------- tree walking, errors, folding

_ctx = threading.local()


def _open_folds():
    return getattr(_ctx, "folds", 0)


def _sub(node, width):
    """The lines of one node; hidden draws nothing, a drawer that raises degrades to the node's plain text."""
    if not isinstance(node, dict) or _props(node).get("hidden") is True:
        return []
    handler = _DRAWERS.get(node.get("k"), _draw_children)
    try:
        return handler(node, width)
    except Exception:
        return _fallback(node, width)


def _plain_text(node, depth=0):
    props = _props(node)
    for key in ("text", "spans", "label", "head", "title", "name", "alt", "summary"):
        value = props.get(key)
        if value:
            return _t(value)
    if depth > 40:
        return ""
    return "\n".join(part for part in (_plain_text(kid, depth + 1) for kid in _kids(node)
                                        if _props(kid).get("hidden") is not True) if part)


def _fallback(node, width):
    try:
        return _wrap(_plain_text(node), width)
    except Exception:
        return []


def _children(node, width):
    lines = []
    for child in _kids(node):
        lines.extend(_sub(child, width))
    return lines


def _body_lines(node, width, skip=None, fold=False):
    """The children of a collapsible node; its open body lets nested previews give way."""
    lines = []
    if fold:
        _ctx.folds = _open_folds() + 1
    try:
        for child in _kids(node):
            if child is not skip:
                lines.extend(_sub(child, width))
    finally:
        if fold:
            _ctx.folds -= 1
    return lines


def _unfolded_for_log(props):
    """True when render_lines was asked to open this node whatever its collapsed state (the session log)."""
    unfold = getattr(_ctx, "unfold", None)
    if not unfold:
        return False
    if unfold is True:
        return True
    role = props.get("role")
    return isinstance(role, str) and any(role.startswith(prefix) for prefix in unfold)


def _is_folded(props):
    """A collapsible node that is collapsed (collapsed alone does nothing)."""
    return _flag(props, "collapsible") and _flag(props, "collapsed") and not _unfolded_for_log(props)


def _preview_lines(value):
    """The `preview` prop as a (count, tail) pair; count 0 means no preview."""
    if value == "auto":
        return 10, False
    if isinstance(value, dict):
        tail = _int(value.get("tail"), 0)
        if tail and tail >= 1:
            return tail, True
        count = _int(value.get("lines"), 0)
        if count and count >= 1:
            return count, False
    return 0, False


def _more_lines(count, early=False):
    return "… %d %s %s" % (count, "earlier" if early else "more", "line" if count == 1 else "lines")


def _preview_clamp(lines, preview, follow=False):
    """The lines of a kind with its own `preview`, clamped; previews give way inside an open fold."""
    count, tail = _preview_lines(preview)
    if not count or _open_folds() or len(lines) <= count:
        return lines
    if tail or follow:
        return [_more_lines(len(lines) - count, True)] + lines[-count:]
    return lines[:count] + [_more_lines(len(lines) - count)]


# ---------------------------------------------------------------- cell grids (tables, md tables)

def _cell_cut(text, width, where):
    return _trunc(text, width, where if where in ("start", "middle") else "end")


def _grid(columns, header, rows, width, gap=2):
    """Columns aligned to their widest cell.  columns: [{align, truncate, priority, grow}]; header: list of str or
    None; rows: lists of str.  Columns hide lowest priority first (ties: the rightmost) while they do not fit,
    then the widest give up width.  A header gets an underline."""
    count = len(columns)
    cells = [list(row) + [""] * (count - len(row)) for row in rows]
    heads = (list(header) + [""] * (count - len(header))) if header else None
    natural = []
    for index in range(count):
        values = [row[index] for row in cells] + ([heads[index]] if heads else [])
        natural.append(max([_w(value) for value in values] + [1]))
    shown = list(range(count))
    def fitting(i):         # a grow column counts as at most 24 cells when deciding what hides
        return min(natural[i], 24) if (_num(columns[i].get("grow"), 0) or 0) > 0 else natural[i]

    while len(shown) > 1 and sum(fitting(i) for i in shown) + gap * (len(shown) - 1) > width:
        victim = min(shown, key=lambda i: (_num(columns[i].get("priority"), 0) or 0, -i))
        shown.remove(victim)
    widths = {i: natural[i] for i in shown}
    while sum(widths.values()) + gap * (len(shown) - 1) > width and max(widths.values()) > 3:
        wide = [i for i in shown if widths[i] > 24 and (_num(columns[i].get("grow"), 0) or 0) > 0]
        pool = wide or shown
        widest = max(pool, key=lambda i: widths[i])
        widths[widest] -= 1
    spacing = " " * gap

    def draw(row, heading=False):
        parts = []
        for i in shown:
            align = columns[i].get("align")
            align = align if align in ("center", "end") else "start"
            cell = row[i] if heading else _cell_cut(row[i], widths[i], columns[i].get("truncate"))
            if heading:
                cell = _trunc(cell, widths[i])
            parts.append(_pad(cell, widths[i], align))
        return spacing.join(parts).rstrip()

    lines = []
    if heads and any(heads):
        lines.append(draw(heads, True))
        lines.append(spacing.join("─" * widths[i] for i in shown))
    lines.extend(draw(row) for row in cells)
    return lines


# ---------------------------------------------------------------- bars, rings, meters

_PART_GLYPHS = "█▓▒"
_RING = "○◔◑◕●"


def _bar(value, size=20):
    if value is None:
        return "[" + "·" * size + "]"
    filled = int(round(_clamp01(value) * size))
    return "[" + "█" * filled + "░" * (size - filled) + "]"


def _meter_cells(meter, size):
    """A meter (value or stacked parts) as `size` cells: █ fill, ▓ ▒ further parts, ░ empty track, · unknown."""
    parts = meter.get("parts")
    if isinstance(parts, list) and parts:
        left, text, index = 1.0, "", 0
        for part in parts:
            if not isinstance(part, dict):
                continue
            share = min(_clamp01(_num(part.get("value"), 0) or 0), left)
            left -= share
            glyph = "░" if part.get("token") == "track" else _PART_GLYPHS[index % len(_PART_GLYPHS)]
            text += glyph * int(round(share * size))
            index += 1
        return text[:size].ljust(size, "░")
    value = _num(meter.get("value"))
    if value is None:
        return "·" * size
    filled = int(round(_clamp01(value) * size))
    return "█" * filled + "░" * (size - filled)


def _meter_value(meter):
    parts = meter.get("parts")
    value = _num(meter.get("value"))
    if value is None and isinstance(parts, list):
        left, value = 1.0, 0.0
        for part in parts:
            if isinstance(part, dict):
                share = min(_clamp01(_num(part.get("value"), 0) or 0), left)
                left -= share
                value += share
    return value


def _meter_level(meter):
    value = _num(meter.get("value"))
    marks = meter.get("thresholds")
    if value is None or not isinstance(marks, dict):
        return ""
    bad, warn = _num(marks.get("bad")), _num(marks.get("warn"))
    if bad is not None and value >= bad:
        return "bad"
    if warn is not None and value >= warn:
        return "warn"
    return ""


# ---------------------------------------------------------------- icons

_ICON_NAMES = frozenset("""
app-btop app-bun app-claude app-codex app-deno app-docker app-go app-hammer app-helix app-hermes app-htop app-java
app-node app-omp app-python app-ruby app-rust app-vim logo tern pi-mark
arrow-up arrow-down arrow-left arrow-right back forward chev chev-r chev-up corner-down-right expand shrink minimize
fit zoom-in zoom-out more grip compass
check x x-circle warn info help bell shield shield-alert slash slash-circle lock key key-round plus minus clock timer
doc file file-code file-image file-pdf file-plus files folder folder-go folder-minus folder-open folder-plus markdown
note clipboard save inbox image newspaper trash
branch commit merge rebase pull push fetch stash cherry diff unified split split-down revert history
play pause stop rewind fast-forward frame-next frame-prev repeat redo undo run reel mic wave vibrate power send share
open download copy scissors
bold italic underline type align-left align-center align-right align-justify pen eraser wand cursor selection
path-insert prompt vector blur ink palette
columns grid sidebar tabs-h tabs-v dock-up dock-down dock-left dock-right pip pip-tl pip-tr pip-bl pip-exit layers
stack kanban canvas diagram flow peek tree box eye eye-off
terminal code braces binary bug cpu database server laptop monitor gauge activity chart plug puzzle wrench gear
sliders stethoscope hash tag link globe broadcast search funnel funnel-x sort-x swap list list-checks pin keyboard
user users message brain sparkle lightbulb bolt flame rocket moon sun cart footprints scale log-in log-out
""".split())

_ICON_GLYPH = {
    "arrow-up": "↑", "arrow-down": "↓", "arrow-left": "←", "arrow-right": "→", "back": "◂", "forward": "▸",
    "chev": "›", "chev-r": "›", "chev-up": "˄", "corner-down-right": "↳", "expand": "⤢", "shrink": "⤡",
    "minimize": "–", "fit": "▣", "zoom-in": "+", "zoom-out": "−", "more": "…", "grip": "⋮", "compass": "✦",
    "check": "✓", "x": "✗", "x-circle": "⊗", "warn": "⚠", "info": "ℹ", "help": "?", "bell": "♪", "shield": "◈",
    "shield-alert": "⚠", "slash": "⊘", "slash-circle": "⊘", "lock": "⚿", "key": "⚷", "key-round": "⚷", "plus": "+",
    "minus": "−", "clock": "◷", "timer": "◴",
    "doc": "▤", "file": "▫", "file-code": "‹›", "file-image": "▨", "file-pdf": "▤", "file-plus": "▫", "files": "▤",
    "folder": "▣", "folder-go": "▣", "folder-minus": "▣", "folder-open": "▢", "folder-plus": "▣", "markdown": "▤",
    "note": "▫", "clipboard": "▤", "save": "▾", "inbox": "▭", "image": "▨", "newspaper": "▤", "trash": "✗",
    "branch": "⑂", "commit": "◉", "merge": "⇆", "rebase": "⇅", "pull": "⇣", "push": "⇡", "fetch": "⇣", "stash": "⊟",
    "cherry": "●", "diff": "±", "unified": "≡", "split": "‖", "split-down": "═", "revert": "↶", "history": "⟲",
    "play": "▶", "pause": "‖", "stop": "■", "rewind": "«", "fast-forward": "»", "frame-next": "▷", "frame-prev": "◁",
    "repeat": "⟳", "redo": "↷", "undo": "↶", "run": "▶", "reel": "▦", "mic": "◍", "wave": "≈", "vibrate": "≋",
    "power": "◉", "send": "➤", "share": "⤴", "open": "↗", "download": "⇩", "copy": "⧉", "scissors": "✂",
    "bold": "B", "italic": "I", "underline": "U", "type": "T", "align-left": "≡", "align-center": "≡",
    "align-right": "≡", "align-justify": "≡", "pen": "✎", "eraser": "⌫", "wand": "✦", "cursor": "↖",
    "selection": "▢", "path-insert": "⤶", "prompt": "›", "vector": "◇", "blur": "░", "ink": "✒", "palette": "◐",
    "columns": "▥", "grid": "▦", "sidebar": "◧", "tabs-h": "▭", "tabs-v": "▯", "dock-up": "▀", "dock-down": "▄",
    "dock-left": "▌", "dock-right": "▐", "pip": "◳", "pip-tl": "◰", "pip-tr": "◳", "pip-bl": "◱", "pip-exit": "◲",
    "layers": "≣", "stack": "≣", "kanban": "▥", "canvas": "▭", "diagram": "◇", "flow": "⇢", "peek": "◉",
    "tree": "┬", "box": "▢", "eye": "◉", "eye-off": "◌",
    "terminal": ">_", "code": "‹›", "braces": "{}", "binary": "01", "bug": "✱", "cpu": "▣", "database": "◫",
    "server": "▤", "laptop": "▭", "monitor": "▭", "gauge": "◔", "activity": "∿", "chart": "▃", "plug": "⏚",
    "puzzle": "◧", "wrench": "⚒", "gear": "⚙", "sliders": "≡", "stethoscope": "✚", "hash": "#", "tag": "◆",
    "link": "⌁", "globe": "⊕", "broadcast": "◉", "search": "⌕", "funnel": "▽", "funnel-x": "▽", "sort-x": "↕",
    "swap": "⇄", "list": "☰", "list-checks": "☑", "pin": "◆", "keyboard": "⌨",
    "user": "☺", "users": "☺", "message": "✉", "brain": "✱", "sparkle": "✦", "lightbulb": "✦", "bolt": "ϟ",
    "flame": "✹", "rocket": "➤", "moon": "☾", "sun": "☀", "cart": "▭", "footprints": "∴", "scale": "⚖",
    "log-in": "⇥", "log-out": "⇤", "logo": "◈", "tern": "◈", "pi-mark": "◈", "app-omp": "◈",
}

_ICON_ALIAS = {
    "success": "check", "done": "check", "error": "x", "failed": "x", "warning": "warn", "pending": "clock",
    "running": "activity", "aborted": "stop", "cancelled": "stop", "disabled": "minus", "model": "sparkle",
    "plan": "list", "goal": "pin", "loop": "redo", "git": "branch", "pr": "merge", "tokens": "hash",
    "speculation": "selection", "compaction": "wand", "context": "layers", "cost": "hash", "time": "clock",
    "omp": "app-omp", "hermes": "app-hermes", "agents": "user", "agent": "user", "job": "run", "cache": "database",
    "input": "download", "output": "send", "host": "laptop", "session": "history", "worktree": "tree",
    "bash": "terminal", "shell": "terminal", "python": "app-python", "edit": "pen", "write": "pen", "read": "doc",
    "web": "globe", "url": "link", "todo": "list", "task": "layers", "thinking": "sparkle", "settings": "sliders",
    "appearance": "ink", "interaction": "keyboard", "memory": "brain", "tools": "wand", "tasks": "list",
    "providers": "server", "plugins": "box", "advisor": "sparkle", "git-branch": "branch", "sparkles": "sparkle",
    "action": "terminal", "extension": "puzzle", "computer": "monitor", "stats": "chart", "news": "newspaper",
    "export": "open", "restart": "revert", "compress": "shrink", "handoff": "forward", "question": "help",
    "pencil": "pen", "folderMove": "folder-go", "folderPlus": "folder-plus", "folderMinus": "folder-minus",
    "hammer": "app-hammer", "prewalk": "footprints", "jobs": "run", "signIn": "log-in", "signOut": "log-out",
    "package": "box", "fast": "bolt", "voice": "mic", "rule": "scale", "skill": "lightbulb", "mcp": "plug",
    "path": "folder", "token_total": "hash", "time_spent": "clock",
}


def _icon_glyph(name):
    """The glyph of a named icon (aliases and the `icon.` / `status.` prefix resolved); None for an unknown name."""
    if not isinstance(name, str) or not name:
        return None
    name = name.rsplit(".", 1)[-1]
    if name not in _ICON_NAMES:
        name = _ICON_ALIAS.get(name, name)
    if name not in _ICON_NAMES:
        return None
    if name.startswith("app-") and name not in _ICON_GLYPH:
        return "▣"
    return _ICON_GLYPH.get(name, "•")


# ---------------------------------------------------------------- layout kinds

def _draw_children(node, width):
    return _children(node, width)


def _draw_col(node, width):
    align = _props(node).get("align")
    if align not in ("center", "end"):
        return _children(node, width)
    lines = []
    for child in _kids(node):
        block = _sub(child, width)
        size = _block_width(block)
        shift = (width - size) // 2 if align == "center" else width - size
        lines.extend((" " * shift + line) if (shift > 0 and line) else line for line in block)
    return lines


_GAPS = {"none": 1, "xs": 1, "sm": 2, "md": 3, "lg": 4}


def _basis(props):
    """A flex basis given as a fraction of the row (0 < b <= 1), else None."""
    value = _num(props.get("basis"))
    return value if value is not None and 0 < value <= 1 else None


def _spread(texts, width, gap):
    """Pieces of one line with the free room shared between them (`justify: between`, growing spacers)."""
    free = width - sum(_w(text) for text in texts)
    count = len(texts) - 1
    if count < 1 or free <= 0:
        return gap.join(text for text in texts if text)
    each, extra = divmod(free, count)
    line = texts[0]
    for number, text in enumerate(texts[1:], 1):
        line += " " * (each + (extra if number == count else 0)) + text
    return line.rstrip()


def _row_inline(items, width, gap, wrap, justify):
    groups = [[]]
    for _node, _props_, lines, spacer in items:
        if spacer:
            groups.append([])
        else:
            groups[-1].append(lines[0])
    if len(groups) > 1:
        return [_spread([gap.join(group) for group in groups], width, gap)]
    pieces = groups[0]
    line = gap.join(pieces)
    if justify == "between" and len(pieces) > 1:
        return [_spread(pieces, width, gap)]
    if justify == "end" and _w(line) < width:
        return [" " * (width - _w(line)) + line]
    if wrap and _w(line) > width:
        lines, current = [], []
        for piece in pieces:
            if current and _w(gap.join(current + [piece])) > width:
                lines.append(gap.join(current))
                current = []
            current.append(piece)
        if current:
            lines.append(gap.join(current))
        return lines
    return [line]


def _hstack(blocks, widths, gap, align):
    """Blocks of lines side by side, each padded to its width; shorter ones sit at the top, middle or bottom."""
    height = max((len(lines) for lines in blocks), default=0)
    rows = []
    for row in range(height):
        parts = []
        for lines, cell in zip(blocks, widths):
            offset = _get({"end": height - len(lines), "start": 0, "baseline": 0}, align, (height - len(lines)) // 2)
            index = row - offset
            text = lines[index] if 0 <= index < len(lines) else ""
            parts.append(_pad(_trunc(text, cell), cell))
        rows.append(gap.join(parts).rstrip())
    return rows


def _row_blocks(items, width, gap, wrap, align):
    gap_w = len(gap)
    entries = []             # [node, lines, target width, grow weight]
    for node, props, lines, spacer in items:
        grow = _num(props.get("grow"), 0) or 0
        size = 0 if spacer else _block_width(lines)
        fraction = None if spacer else _basis(props)
        if fraction is not None:
            size = max(1, int(fraction * width))
            lines = _sub(node, size)
        entries.append([node, lines, size, max(0, grow)])
    # partition into rows
    parts, current = [], []
    for entry in entries:
        total = sum(e[2] for e in current) + gap_w * len(current) + entry[2]
        if wrap and current and total > width:
            parts.append(current)
            current = []
        current.append(entry)
    parts.append(current)
    out = []
    for part in parts:
        used = sum(e[2] for e in part) + gap_w * (len(part) - 1)
        if used > width:
            return [line for entry in part for line in entry[1]] if len(parts) == 1 else \
                [line for e in entries for line in e[1]]
        free = width - used
        weights = sum(e[3] for e in part)
        sizes = [e[2] for e in part]
        if free > 0 and weights > 0:
            sizes = [size + int(free * e[3] / weights) for size, e in zip(sizes, part)]
        out.extend(_hstack([e[1] for e in part], sizes, gap, align))
    return out


def _draw_row(node, width):
    props = _props(node)
    gap = " " * _get(_GAPS, props.get("gap"), 2)
    items = []
    for child in _kids(node):
        child_props = _props(child)
        if child_props.get("hidden") is True:
            continue
        lines = _sub(child, width)
        if lines:
            items.append((child, child_props, lines, False))
        elif (_num(child_props.get("grow"), 0) or 0) > 0:
            items.append((child, child_props, [], True))     # an empty growing child pushes the rest away
    if not any(not item[3] for item in items):
        return []
    if all(item[3] or len(item[2]) == 1 for item in items):
        return _row_inline(items, width, gap, _flag(props, "wrap"), props.get("justify"))
    return _row_blocks(items, width, gap, _flag(props, "wrap"), props.get("align"))


def _head_of(node, props):
    """(text, child): a card/section head is spans or the id of one of its own children."""
    head = props.get("head")
    if isinstance(head, str):
        for child in _kids(node):
            if child.get("id") == head:
                return " ".join(line.strip() for line in _sub(child, _WIDE) if line.strip()), child
    return _one(_t(head)).strip(), None


def _draw_card(node, width):
    props = _props(node)
    folded = _is_folded(props)
    head, head_child = _head_of(node, props)
    glyph = _get(_MARKS, props.get("status"), "")
    if not glyph and head:
        glyph = _get({"error": "✗", "warning": "⚠", "success": "✓", "info": "ℹ", "pending": "◌"}, props.get("tone"), "")
    chevron = ("▸" if folded else "▾") if _flag(props, "collapsible") else ""
    title = " ".join(part for part in (chevron, glyph, head) if part)
    lines = _hang_wrap(title, width, "  ") if title else []
    count, _tail = _preview_lines(props.get("preview"))
    if folded:
        if not count:
            return lines
        body = _body_lines(node, width - len(INDENT), head_child)
        shown = body[:count]
        if len(body) > count:
            shown.append(_more_lines(len(body) - count))
        return lines + _indent(shown)
    return lines + _indent(_body_lines(node, width - len(INDENT), head_child, _flag(props, "collapsible")))


def _draw_section(node, width):
    props = _props(node)
    folded = _is_folded(props)
    head, head_child = _head_of(node, props)
    title = ("▸ " if folded else "▾ ") + head
    lines = _hang_wrap(title.rstrip(), width, "  ")
    if folded:
        return lines
    return lines + _indent(_body_lines(node, width - len(INDENT), head_child, _flag(props, "collapsible")))


def _draw_rule(node, width):
    label = _one(_t(_props(node).get("label"))).strip()
    span = min(width, 80)
    if not label:
        return ["─" * span]
    label = _trunc(label, max(1, span - 8))
    room = span - _w(label) - 2
    left = room // 2
    return ["─" * left + " " + label + " " + "─" * (room - left)]


def _draw_spacer(node, width):
    size = _props(node).get("size")
    return [] if size in ("none", "xs") else (["", ""] if size == "lg" else [""])


# ---------------------------------------------------------------- text kinds

def _draw_text(node, width):
    props = _props(node)
    spans = props.get("spans")
    text = _t(spans) if isinstance(spans, list) else (_clean(props["text"]) if isinstance(props.get("text"), str) else "")
    if props.get("measure") == "prose":
        width = min(width, 80)
    mode = props.get("wrap") if props.get("wrap") in ("word", "char", "none") else "word"
    where = props.get("truncate") if props.get("truncate") in ("end", "start", "middle") else "end"
    clamp = _int(props.get("lines"), 0) or 0
    one_line = mode == "none" or clamp == 1 or (props.get("truncate") is not None and props.get("wrap") is None
                                               and not clamp)
    if one_line and clamp != 1:
        return [_trunc(line, width, where) for line in text.split("\n")]
    lines = _wrap(text, width, mode)
    if clamp > 0:
        lines = _clamp_lines(lines, clamp, width)
    return _preview_clamp(lines, props.get("preview"))


# ---- markdown

_MD_REFDEF = re.compile(r"^ {0,3}\[([^\]^][^\]]*)\]:[ \t]*<?(\S+?)>?(?:[ \t]+[\"'(].*)?[ \t]*$")
_MD_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})[ \t]*([^`]*?)[ \t]*$")
_MD_HEAD = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_MD_HR = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_MD_ITEM = re.compile(r"^( {0,3})([-+*]|\d{1,9}[.)])(?:([ \t]+)(.*))?$")
_MD_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_MD_DELIM = re.compile(r"^:?-+:?$")
_MD_HTML = re.compile(r"^ {0,3}(?:</?[A-Za-z][A-Za-z0-9-]*(?:[ \t/>]|$)|<!--)")
_MD_CALLOUT = re.compile(r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*$", re.I)
_MD_ICONS = {"NOTE": "ℹ", "TIP": "✦", "IMPORTANT": "★", "WARNING": "⚠", "CAUTION": "✖"}
_MD_BULLETS = ("•", "◦", "▪")


def _md_keep(store, text):
    store.append(text)
    return "\x01%d\x01" % (len(store) - 1)


def _md_emphasis(text):
    text = re.sub(r"(?<![\w*])\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<!\w)___(?=\S)(.+?)(?<=\S)___(?!\w)", r"\1", text)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"\1", text)
    text = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])", r"\1", text)
    return re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"\1", text)


def _md_link(text, url):
    text, url = text.strip(), url.strip()
    return url if (not text or text == url) else "%s (%s)" % (text, url)


def _md_inline(text, refs):
    """Inline Markdown as plain text: emphasis markers removed, links as `text (url)`, images as `[image: alt]`."""
    store = []
    text = re.sub(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)", lambda m: _md_keep(store, "`" + m.group(2).strip() + "`"), text)
    text = re.sub(r"\\\((.+?)\\\)", lambda m: _md_keep(store, _tex(m.group(1)).strip()), text)
    text = re.sub(r"\\([!-/:-@\[-`{-~])", lambda m: _md_keep(store, m.group(1)), text)
    text = re.sub(r"(?<![\w$])\$(?=[^\s$])([^$\n]*?[^\s$]|[^\s$])\$(?!\d)",
                  lambda m: _md_keep(store, _tex(m.group(1)).strip()), text)
    text = re.sub(r"!\[([^\]]*)\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)",
                  lambda m: _md_keep(store, "[image: %s]" % (m.group(1).strip() or m.group(2))), text)
    text = re.sub(r"\[([^\]]*)\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)",
                  lambda m: _md_keep(store, _md_link(_md_emphasis(m.group(1)), m.group(2))), text)

    def reference(match):
        label = (match.group(2) or match.group(1)).strip().lower()
        if label in refs:
            return _md_keep(store, _md_link(_md_emphasis(match.group(1)), refs[label]))
        return match.group(0)

    text = re.sub(r"\[([^\]]+)\]\[([^\]]*)\]", reference, text)
    if refs:
        text = re.sub(r"\[([^\]]+)\](?![(\[:])", lambda m: reference(m) if m.group(1).strip().lower() in refs
                      else m.group(0), text)
    text = re.sub(r"<((?:https?|ftp|mailto):[^>\s]+|[^>\s@]+@[^>\s]+\.[^>\s]+)>",
                  lambda m: _md_keep(store, m.group(1)), text)
    text = _md_emphasis(text)
    for _ in range(8):
        if "\x01" not in text:
            break
        text = re.sub("\x01(\\d+)\x01", lambda m: store[int(m.group(1))], text)
    return text


def _md_indent(line):
    return len(line) - len(line.lstrip(" "))


def _md_split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line)]


def _md_is_delim(line, count):
    if "-" not in line or ("|" not in line and count != 1):
        return False
    cells = _md_split_row(line)
    return len(cells) == count and all(_MD_DELIM.match(cell) for cell in cells)


def _md_starts_block(line):
    return bool(_MD_FENCE.match(line) or _MD_HEAD.match(line) or _MD_HR.match(line) or line.lstrip(" ").startswith(">")
                or _MD_ITEM.match(line) and (_MD_ITEM.match(line).group(4) or "").strip() != ""
                or line.lstrip().startswith("$$") or line.lstrip().startswith("\\["))


def _md_code(lines, label, width):
    out = [("    " + piece) for line in lines for piece in _hard_wrap(line, max(1, width - 4))]
    return (["  [%s]" % label] if label else []) + out


def _md_math(source, width):
    lines = [line.strip() for line in _tex(source).split("\n") if line.strip()]
    return [_pad("", max(0, (width - _w(line)) // 2)) + line for line in lines]


def _md_table(rows, aligns, width, refs):
    columns = [{"align": {"l": "start", "r": "end", "c": "center"}.get(a, "start")} for a in aligns]
    head = [_md_inline(cell, refs) for cell in rows[0]]
    body = [[_md_inline(cell, refs) for cell in row] for row in rows[1:]]
    return _grid(columns, head, body, width)


def _md_list(lines, start, width, refs, depth):
    """One list starting at lines[start]: (output lines, next index)."""
    count = len(lines)
    first = _MD_ITEM.match(lines[start])
    base = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    number = int(first.group(2)[:-1]) if ordered else 0
    items, i = [], start
    while i < count:
        match = _MD_ITEM.match(lines[i])
        if not match or len(match.group(1)) >= base + 4 or match.group(2)[0].isdigit() != ordered:
            break
        marker, spaces, rest = match.group(2), match.group(3) or "", match.group(4) or ""
        content = len(match.group(1)) + len(marker) + (len(spaces) if 0 < len(spaces) <= 4 else 1)
        body, loose = [rest], False
        i += 1
        while i < count:
            line = lines[i]
            if not line.strip():
                j = i + 1
                while j < count and not lines[j].strip():
                    j += 1
                if j < count and _md_indent(lines[j]) >= content:
                    body.append("")
                    loose = True
                    i += 1
                    continue
                break
            if _md_indent(line) >= content:
                body.append(line[content:])
            elif body[-1].strip() and not _md_starts_block(line) and not _MD_ITEM.match(line):
                body.append(line.strip())
            else:
                break
            i += 1
        items.append((body, loose))
        j = i
        while j < count and not lines[j].strip():
            j += 1
        if j < count and j > i:
            nxt = _MD_ITEM.match(lines[j])
            if nxt and len(nxt.group(1)) < base + 4 and nxt.group(2)[0].isdigit() == ordered:
                i = j
    out = []
    marks = []
    for index, (body, loose_item) in enumerate(items):
        task = re.match(r"^\[([ xX])\][ \t]+", body[0])
        if task:
            marks.append("[x]" if task.group(1) in "xX" else "[ ]")
            items[index] = ([body[0][task.end():]] + body[1:], loose_item)
        elif ordered:
            marks.append("%d." % (number + index))
        else:
            marks.append(_MD_BULLETS[depth % len(_MD_BULLETS)])
    mark_w = max((_w(mark) for mark in marks), default=1) + 1
    for (body, loose), mark in zip(items, marks):
        inner = _md_blocks(body, max(1, width - mark_w), refs, depth + 1, loose)
        if not inner:
            out.append(mark)
            continue
        out.append(_pad(mark, mark_w - 1, "end" if ordered else "start") + " " + inner[0])
        out.extend((" " * mark_w + line) if line else line for line in inner[1:])
    return out, i


def _md_blocks(lines, width, refs, depth=0, loose=True):
    blocks = []
    i, count = 0, len(lines)
    while i < count:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _md_indent(line) >= 4:
            code = []
            while i < count and (_md_indent(lines[i]) >= 4 or not lines[i].strip()):
                code.append(lines[i][4:] if lines[i].strip() else "")
                i += 1
            while code and not code[-1].strip():
                code.pop()
            blocks.append(_md_code(code, "", width))
            continue
        fence = _MD_FENCE.match(line)
        if fence:
            mark, info = fence.group(2), fence.group(3).split(" ")[0] if fence.group(3) else ""
            code = []
            i += 1
            while i < count and not re.match(r"^ {0,3}" + mark[0] + "{%d,}[ \t]*$" % len(mark), lines[i]):
                code.append(lines[i][min(len(fence.group(1)), _md_indent(lines[i])):])
                i += 1
            i += 1
            blocks.append(_md_code(code, "mermaid" if info.lower() == "mermaid" else "", width))
            continue
        stripped = line.strip()
        if stripped.startswith("$$") or stripped.startswith("\\["):
            close = "$$" if stripped.startswith("$$") else "\\]"
            body = stripped[2:]
            source = []
            if close in body:
                source.append(body[:body.index(close)])
                i += 1
            else:
                source.append(body)
                i += 1
                while i < count and close not in lines[i]:
                    source.append(lines[i])
                    i += 1
                if i < count:
                    source.append(lines[i][:lines[i].index(close)])
                    i += 1
            blocks.append(_md_math("\n".join(source), width))
            continue
        heading = _MD_HEAD.match(line)
        if heading:
            text = _md_inline((heading.group(2) or "").strip(), refs)
            blocks.append(_hang_wrap(heading.group(1) + (" " + text if text else ""), width, " " * (len(heading.group(1)) + 1)))
            i += 1
            continue
        if _MD_HR.match(line):
            blocks.append(["─" * min(width, 80)])
            i += 1
            continue
        if stripped.startswith(">"):
            quote = []
            while i < count and lines[i].strip():
                quote.append(re.sub(r"^ {0,3}> ?", "", lines[i]) if lines[i].lstrip().startswith(">") else lines[i])
                i += 1
            callout = _MD_CALLOUT.match(quote[0].strip())
            title = []
            if callout:
                kind = callout.group(1).upper()
                title = ["%s %s" % (_MD_ICONS[kind], kind)]
                quote = quote[1:]
            inner = title + _md_blocks(quote, max(1, width - 2), refs, depth, True)
            blocks.append([("│ " + text).rstrip() for text in inner] or ["│"])
            continue
        item = _MD_ITEM.match(line)
        if item and (item.group(4) or "").strip():
            out, i = _md_list(lines, i, width, refs, depth)
            blocks.append(out)
            continue
        if _MD_HTML.match(line):
            literal = []
            while i < count and lines[i].strip():
                literal.append(lines[i].rstrip())
                i += 1
            blocks.append([piece for text in literal for piece in _hard_wrap(text, width)])
            continue
        # a table, or a paragraph
        if "|" in line and i + 1 < count and _md_is_delim(lines[i + 1], len(_md_split_row(line))):
            head = _md_split_row(line)
            aligns = ["c" if cell.startswith(":") and cell.endswith(":") else "r" if cell.endswith(":")
                      else "l" for cell in _md_split_row(lines[i + 1])]
            rows = [head]
            i += 2
            while i < count and lines[i].strip() and not _md_starts_block(lines[i]):
                row = _md_split_row(lines[i])
                rows.append((row + [""] * len(head))[:len(head)])
                i += 1
            blocks.append(_md_table(rows, aligns, width, refs))
            continue
        para = [line]
        i += 1
        level = 0
        while i < count and lines[i].strip():
            setext = _MD_SETEXT.match(lines[i])
            if setext:
                level = 1 if setext.group(1)[0] == "=" else 2
                i += 1
                break
            if _md_starts_block(lines[i]) or _MD_HTML.match(lines[i]) or (
                    "|" in lines[i] and i + 1 < count and _md_is_delim(lines[i + 1], len(_md_split_row(lines[i])))):
                break
            para.append(lines[i])
            i += 1
        text = ""
        for number, raw in enumerate(para):
            hard = raw.endswith("  ") or raw.endswith("\\")
            piece = raw.strip()
            if raw.endswith("\\"):
                piece = piece[:-1].rstrip()
            text += piece + ("\n" if hard and number < len(para) - 1 else " " if number < len(para) - 1 else "")
        text = _md_inline(text, refs)
        if level:
            blocks.append(_hang_wrap("#" * level + " " + _one(text), width, " " * (level + 1)))
        else:
            blocks.append(_wrap(text, width))
    out = []
    for number, block in enumerate(blocks):
        if number and loose:
            out.append("")
        out.extend(block)
    return out


def _draw_md(node, width):
    source = _clean(plain(_props(node).get("text")))
    lines = source.split("\n")
    refs, kept, fenced = {}, [], None
    for line in lines:
        fence = _MD_FENCE.match(line)
        if fence:
            if fenced is None:
                fenced = fence.group(2)
            elif line.strip().startswith(fenced[0] * len(fenced)) and not fence.group(3):
                fenced = None
        elif fenced is None:
            ref = _MD_REFDEF.match(line)
            if ref:
                refs[ref.group(1).strip().lower()] = ref.group(2)
                continue
        kept.append(line)
    return _md_blocks(kept, width, refs)


# ---- TeX

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ",
    "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
_TEX_SYM = {
    "sum": "∑", "prod": "∏", "int": "∫", "oint": "∮", "infty": "∞", "partial": "∂", "nabla": "∇", "pm": "±",
    "mp": "∓", "times": "×", "div": "÷", "cdot": "·", "ast": "∗", "star": "⋆", "circ": "∘", "bullet": "•",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡", "sim": "∼",
    "simeq": "≃", "propto": "∝", "ll": "≪", "gg": "≫", "to": "→", "rightarrow": "→", "leftarrow": "←",
    "leftrightarrow": "↔", "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔", "implies": "⇒",
    "iff": "⇔", "mapsto": "↦", "uparrow": "↑", "downarrow": "↓", "in": "∈", "notin": "∉", "ni": "∋",
    "subset": "⊂", "supset": "⊃", "subseteq": "⊆", "supseteq": "⊇", "cup": "∪", "cap": "∩", "emptyset": "∅",
    "varnothing": "∅", "forall": "∀", "exists": "∃", "neg": "¬", "lnot": "¬", "land": "∧", "wedge": "∧", "lor": "∨",
    "vee": "∨", "oplus": "⊕", "otimes": "⊗", "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱",
    "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉", "degree": "°",
    "angle": "∠", "perp": "⊥", "parallel": "∥", "hbar": "ħ", "ell": "ℓ", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ",
    "prime": "′", "quad": "  ", "qquad": "    ", ",": " ", ";": " ", ":": " ", "!": "", " ": " ", "\\": "\n",
    "|": "‖", "{": "{", "}": "}", "&": "&", "%": "%", "$": "$", "#": "#", "_": "_",
}
_TEX_FUNCS = frozenset("sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh log ln lg exp lim limsup liminf "
                       "max min sup inf det dim gcd ker deg arg mod Pr".split())
_TEX_WRAP = frozenset("text mathrm mathbf mathit mathsf mathtt operatorname textbf textit boldsymbol mathcal "
                      "mathfrak mathscr bm textrm emph displaystyle textstyle scriptstyle".split())
_TEX_ACCENT = {"hat": "\u0302", "widehat": "\u0302", "bar": "\u0304", "overline": "\u0304", "vec": "\u20d7",
               "tilde": "\u0303", "widetilde": "\u0303", "dot": "\u0307", "ddot": "\u0308"}
_BB = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "H": "ℍ", "P": "ℙ"}
_SUP = {"0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
        "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼", "(": "⁽", ")": "⁾", "n": "ⁿ", "i": "ⁱ", "x": "ˣ"}
_SUB = {"0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
        "+": "₊", "-": "₋", "−": "₋", "=": "₌", "(": "₍", ")": "₎", "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ",
        "j": "ⱼ", "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ",
        "u": "ᵤ", "v": "ᵥ", "x": "ₓ"}
_TEX_CMD = re.compile(r"\\([A-Za-z]+|.)", re.S)


def _tex_group(text, i):
    """The argument at text[i]: a {balanced group} or one token.  Returns (content, next index)."""
    size = len(text)
    while i < size and text[i] == " ":
        i += 1
    if i >= size:
        return "", i
    if text[i] == "{":
        depth, j = 0, i
        while j < size:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    return text[i + 1:j], j + 1
            j += 1
        return text[i + 1:], size
    if text[i] == "\\":
        match = _TEX_CMD.match(text, i)
        return match.group(0), match.end()
    return text[i], i + 1


def _tex_needs_parens(text):
    depth = 0
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch in "+-−=<>≤≥±/ ":
            return True
    return False


def _tex_script(text, table, mark):
    text = text.strip()
    if text and all(ch in table for ch in text):
        return "".join(table[ch] for ch in text)
    return mark + ("(%s)" % text if len(text) > 1 else text)


def _tex(text):
    """TeX math as readable text: symbols and Greek as Unicode, fractions as a/b, scripts as ² ₁ or ^(…)."""
    out, i, size = [], 0, len(text)
    while i < size:
        ch = text[i]
        if ch == "\\":
            match = _TEX_CMD.match(text, i)
            name, i = match.group(1), match.end()
            if name in ("frac", "dfrac", "tfrac", "cfrac"):
                top, i = _tex_group(text, i)
                bottom, i = _tex_group(text, i)
                top, bottom = _tex(top).strip(), _tex(bottom).strip()
                out.append("%s/%s" % ("(%s)" % top if _tex_needs_parens(top) else top,
                                      "(%s)" % bottom if _tex_needs_parens(bottom) else bottom))
            elif name == "sqrt":
                root = ""
                if i < size and text[i] == "[" and "]" in text[i:]:
                    close = text.index("]", i)
                    root, i = _tex(text[i + 1:close]), close + 1
                arg, i = _tex_group(text, i)
                arg = _tex(arg).strip()
                prefix = (_tex_script(root, _SUP, "^") if root else "") + "√"
                out.append(prefix + ("(%s)" % arg if _tex_needs_parens(arg) or len(arg) > 1 else arg))
            elif name in _TEX_WRAP:
                arg, i = _tex_group(text, i)
                out.append(_tex(arg))
            elif name == "mathbb":
                arg, i = _tex_group(text, i)
                out.append("".join(_BB.get(c, c) for c in _tex(arg)))
            elif name in _TEX_ACCENT:
                arg, i = _tex_group(text, i)
                arg = _tex(arg)
                out.append(arg[:1] + _TEX_ACCENT[name] + arg[1:] if len(arg) == 1 else arg)
            elif name in ("begin", "end"):
                _arg, i = _tex_group(text, i)
            elif name in ("left", "right", "big", "Big", "bigg", "Bigg", "limits", "nolimits"):
                if i < size and text[i] == ".":
                    i += 1
            elif name in _GREEK:
                out.append(_GREEK[name])
            elif name in _TEX_SYM:
                out.append(_TEX_SYM[name])
            elif name in _TEX_FUNCS:
                out.append(name)
            else:
                out.append(name)
        elif ch in "^_":
            arg, i = _tex_group(text, i + 1)
            out.append(_tex_script(_tex(arg), _SUP if ch == "^" else _SUB, ch))
        elif ch in "{}":
            i += 1
        elif ch == "&":
            out.append("  ")
            i += 1
        elif ch == "~":
            out.append(" ")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _draw_math(node, width):
    props = _props(node)
    source = _clean(plain(props.get("text")))
    if _flag(props, "display"):
        lines = [line.strip() for line in _tex(source).split("\n") if line.strip()]
        return [_pad("", max(0, (width - _w(line)) // 2)) + line for line in lines]
    return _wrap(" ".join(_tex(source).split()), width)


# ---- code, diff, ansi, rows

def _draw_code(node, width):
    props = _props(node)
    lines = _clean(plain(props.get("text"))).split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    start = _int(props.get("start"), 1)
    marks = {}
    for mark in _list(props.get("marks")):
        if isinstance(mark, dict) and _int(mark.get("line")) is not None:
            marks[_int(mark.get("line"))] = _get({"error": "✗", "warning": "!", "success": "✓", "info": "i"},
                                                 mark.get("tone"), "▸")
    numbers = _flag(props, "numbers")
    gutter = max(4, len(str(start + len(lines) - 1))) if numbers else 0
    prefix_w = (gutter + 2 if numbers else 0) + (1 if marks else 0)
    room = max(1, width - len(INDENT) - prefix_w)
    out = []
    path = _one(_t(props.get("path"))).strip()
    if path:
        out.append(INDENT + "┌─ " + path)
    for index, line in enumerate(lines):
        number = start + index
        pieces = _hard_wrap(line, room)
        for piece_no, piece in enumerate(pieces):
            head = ""
            if marks:
                head += marks.get(number, " ") if piece_no == 0 else " "
            if numbers:
                head += ("%*d  " % (gutter, number)) if piece_no == 0 else " " * (gutter + 2)
            out.append(INDENT + head + piece)
    return _preview_clamp(out, props.get("preview"))


_HUNK_RE = re.compile(r"^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@+ ?(.*)$")


def _diff_hunks(props):
    """[(oldStart, newStart, [lines], header text)] from `hunks` or from the unified `text`."""
    hunks = props.get("hunks")
    if isinstance(hunks, list):
        result = []
        for hunk in hunks:
            if isinstance(hunk, dict):
                lines = [_clean(str(line)) for line in _list(hunk.get("lines")) if isinstance(line, str)
                         and not line.startswith("\\")]
                result.append((_int(hunk.get("oldStart"), 1), _int(hunk.get("newStart"), 1), lines, ""))
        return result
    lines = _clean(plain(props.get("text"))).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    result, i, count = [], 0, len(lines)
    while i < count and not _HUNK_RE.match(lines[i]):
        i += 1
    if i >= count:
        body = [line for line in lines if not line.startswith("\\")]
        header_skip = 0
        while header_skip < len(body) and re.match(r"^(diff --git |index |--- |\+\+\+ |new file|deleted file)",
                                                   body[header_skip]):
            header_skip += 1
        body = body[header_skip:] if header_skip and header_skip < len(body) else body
        return [(1, 1, body, "")] if body else []
    while i < count:
        match = _HUNK_RE.match(lines[i])
        if not match:
            i += 1
            continue
        body = []
        i += 1
        while i < count and not _HUNK_RE.match(lines[i]) and not lines[i].startswith("diff --git "):
            if lines[i].startswith("--- ") and i + 2 < count and lines[i + 1].startswith("+++ ") \
                    and _HUNK_RE.match(lines[i + 2]):
                break
            if not lines[i].startswith("\\"):
                body.append(lines[i])
            i += 1
        result.append((int(match.group(1)), int(match.group(3)), body, match.group(5).strip()))
    return result


def _diff_rows(hunks):
    """Rows (kind, old number, new number, text) with fold rows ('fold', count, section) between hunks."""
    rows, end_old = [], None
    for index, (old, new, lines, header) in enumerate(hunks):
        if index:
            gap = old - end_old if end_old is not None else None
            rows.append(("fold", gap if gap and gap > 0 else None, header))
        for line in lines:
            mark, text = (line[:1], line[1:]) if line[:1] in ("+", "-", " ") else (" ", line)
            if mark == "+":
                rows.append(("add", None, new, text))
                new += 1
            elif mark == "-":
                rows.append(("del", old, None, text))
                old += 1
            else:
                rows.append(("ctx", old, new, text))
                old += 1
                new += 1
        end_old = old
    return rows


def _draw_diff(node, width):
    props = _props(node)
    rows = _diff_rows(_diff_hunks(props))
    if not any(row[0] in ("add", "del", "ctx") for row in rows):
        return ["No changes"]
    digits = max([len(str(n)) for row in rows if row[0] != "fold" for n in row[1:3] if n is not None] + [1])
    split = props.get("mode") == "split" and width >= 60
    out = []
    marker = {"add": "+", "del": "-", "ctx": " "}
    if not split:
        room = max(1, width - 2 * digits - 4)
        for row in rows:
            if row[0] == "fold":
                out.append(("⋯ %d unchanged lines" % row[1] if row[1] else "⋯") + ("  " + row[2] if row[2] else ""))
                continue
            kind, old, new, text = row
            for number, piece in enumerate(_hard_wrap(text, room)):
                if number == 0:
                    out.append("%*s %*s %s %s" % (digits, "" if old is None else old, digits,
                                                  "" if new is None else new, marker[kind], piece))
                else:
                    out.append(" " * (2 * digits + 2) + "  " + piece)
        return out
    half = (width - 3) // 2
    room = max(1, half - digits - 3)
    index = 0
    while index < len(rows):
        row = rows[index]
        if row[0] == "fold":
            out.append(("⋯ %d unchanged lines" % row[1] if row[1] else "⋯") + ("  " + row[2] if row[2] else ""))
            index += 1
            continue
        if row[0] == "ctx":
            left = right = row
            index += 1
            pairs = [(left, right)]
        else:
            dels, adds = [], []
            while index < len(rows) and rows[index][0] == "del":
                dels.append(rows[index])
                index += 1
            while index < len(rows) and rows[index][0] == "add":
                adds.append(rows[index])
                index += 1
            pairs = [(dels[k] if k < len(dels) else None, adds[k] if k < len(adds) else None)
                     for k in range(max(len(dels), len(adds)))]
        for left, right in pairs:
            def cell(side, which):
                if side is None:
                    return [""]
                number = side[1] if which == 0 else side[2]
                pieces = _hard_wrap(side[3], room)
                return [("%*s %s %s" % (digits, "" if number is None else number, marker[side[0]], piece)
                         if k == 0 else " " * (digits + 3) + piece) for k, piece in enumerate(pieces)]
            left_lines, right_lines = cell(left, 0), cell(right, 1)
            for k in range(max(len(left_lines), len(right_lines))):
                a = left_lines[k] if k < len(left_lines) else ""
                b = right_lines[k] if k < len(right_lines) else ""
                out.append((_pad(a, half) + " │ " + b).rstrip())
    return out


def _ansi_lines(text):
    """The visible lines of terminal output: escapes stripped, \\r overwrites and \\b erases applied."""
    out = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        raw = _ESC_RE.sub("", raw)
        if "\r" in raw:
            buffer = []
            for segment in raw.split("\r"):
                buffer[:len(segment)] = list(segment)
            raw = "".join(buffer)
        while "\b" in raw:
            raw = re.sub(r"[^\x08]\x08|^\x08", "", raw, count=1)
        out.append(_clean(raw).rstrip())
    return out


def _draw_ansi(node, width):
    props = _props(node)
    lines = _ansi_lines(plain(props.get("text")))
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    cols = _int(props.get("cols"))
    room = min(width, cols) if cols and cols > 0 else width
    wrapped = [piece for line in lines for piece in _hard_wrap(line, room)]
    return _preview_clamp(wrapped, props.get("preview"), _flag(props, "follow"))


def _draw_rows(node, width):
    lines = []
    for line in _list(_props(node).get("lines")):
        if isinstance(line, str):
            lines.extend(_hard_wrap(piece, width) for piece in _ansi_lines(line))
    return [piece for group in lines for piece in group]


# ---------------------------------------------------------------- data kinds

def _draw_kv(node, width):
    props = _props(node)
    pairs = []
    for item in _list(props.get("items")):
        if isinstance(item, dict):
            key, value = item.get("k"), item.get("v")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            key, value = item[0], item[1]
        else:
            continue
        pairs.append((_one(_t(key)).strip(), _t(value)))
    if not pairs:
        return []
    if props.get("layout") == "inline":
        return _wrap(" · ".join("%s %s" % (key, _one(value)) for key, value in pairs), width)
    key_w = min(max(_w(key) for key, _value in pairs), max(4, width // 2))
    room = max(1, width - key_w - 2)
    lines = []
    for key, value in pairs:
        label = _pad(_trunc(key, key_w) + ":", key_w + 1) + " "
        pieces = _wrap(value, room)
        lines.append((label + pieces[0]).rstrip())
        lines.extend(" " * (key_w + 2) + piece for piece in pieces[1:])
    return lines


def _draw_table(node, width):
    props = _props(node)
    columns = [col for col in _list(props.get("cols")) if isinstance(col, dict)]
    if not columns:
        return []
    head = [_one(_t(col.get("head"))) for col in columns]
    body = []
    for row in _list(props.get("rows")):
        cells = row.get("cells") if isinstance(row, dict) else None
        cells = cells if isinstance(cells, dict) else {}
        line = []
        for col in columns:
            cell = cells.get(col.get("id"))
            if isinstance(cell, dict) and isinstance(cell.get("meter"), dict):
                line.append(_meter_cells(cell["meter"], 8))
            else:
                line.append(_one(_t(cell)))
        body.append(line)
    return _grid(columns, head, body, width)


def _tree_count(items):
    return sum(1 + _tree_count(_list(item.get("children"))) for item in items if isinstance(item, dict))


def _draw_tree(node, width):
    lines = []

    def walk(items, prefix, depth):
        items = [item for item in items if isinstance(item, dict)]
        for index, item in enumerate(items):
            last = index == len(items) - 1
            kids = [kid for kid in _list(item.get("children")) if isinstance(kid, dict)]
            opened = item.get("open") is True
            chevron = ("▾ " if opened else "▸ ") if kids else "  "
            glyph = _icon_glyph(item.get("icon"))
            label = _one(_t(item.get("label")))
            text = prefix + ("" if depth == 0 else ("└─ " if last else "├─ ")) + chevron + (glyph + " " if glyph else "") + label
            if kids and not opened:
                text += " (%d)" % _tree_count(kids)
            lines.append(_trunc(text, width))
            if kids and opened:
                walk(kids, prefix + ("" if depth == 0 else ("   " if last else "│  ")), depth + 1)

    walk(_list(_props(node).get("nodes")), "", 0)
    return lines


def _draw_badge(node, width):
    return ["[%s]" % _one(_t(_props(node).get("text")))]


def _draw_kbd(node, width):
    return ["[%s]" % "+".join(_keycap(key) for key in _list(_props(node).get("keys")) if isinstance(key, str))]


def _draw_icon(node, width):
    name = _props(node).get("name")
    if not isinstance(name, str) or not name:
        return ["•"]
    glyph = _icon_glyph(name)
    return [glyph if glyph is not None else "[%s]" % _one(_clean(name))]


def _draw_image(node, width):
    props = _props(node)
    label = _one(_t(props.get("alt"))) or _one(_t(props.get("title"))) or (
        _clean(str(props["builtin"])) if props.get("builtin") else "") or "picture"
    size = ""
    w, h = _int(props.get("w")), _int(props.get("h"))
    if w and h and w > 0 and h > 0:
        size = " %d×%d" % (w, h)
    lines = ["[image: %s%s]" % (label, size)]
    return lines + _indent(_children(node, max(1, width - len(INDENT))))


def _draw_media(node, width):
    props = _props(node)
    label = _one(_t(props.get("alt"))) or _one(_t(props.get("title"))) or _one(_clean(str(props.get("src") or "")))
    return ["[media: %s]" % label]


# ---------------------------------------------------------------- lists, tabs, pickers

def _item_row(node):
    """The parts of an `item` as text."""
    props = _props(node)
    glyph = _icon_glyph(props.get("icon")) or ""
    hint = "+".join(_keycap(key) for key in _list(props.get("hint")) if isinstance(key, str))
    value = _one(_t(props.get("value")))
    return {
        "icon": glyph, "label": _one(_t(props.get("label"))), "detail": _one(_t(props.get("detail"))),
        "value": " ".join(part for part in (value, ("[%s]" % hint) if hint else "") if part),
        "disabled": _flag(props, "disabled"), "id": node.get("id"),
    }


def _item_lines(rows, width, selected):
    """`item` rows aligned: marker, icon, label, detail, right-aligned value."""
    icon_w = max((_w(row["icon"]) for row in rows), default=0)
    value_w = min(max((_w(row["value"]) for row in rows), default=0), max(4, width * 2 // 5))
    has_detail = any(row["detail"] for row in rows)
    label_w = max((_w(row["label"]) for row in rows), default=0)
    lead = 2 + (icon_w + 1 if icon_w else 0)
    label_w = min(label_w, max(4, width - lead - (value_w + 2 if value_w else 0) - (6 if has_detail else 0)))
    lines = []
    for row in rows:
        marker = "›" if row["id"] is not None and row["id"] == selected else ("×" if row["disabled"] else " ")
        text = marker + " " + (_pad(row["icon"], icon_w) + " " if icon_w else "")
        label = _trunc(row["label"], label_w)
        if row["detail"]:
            room = width - lead - label_w - 2 - (value_w + 2 if value_w else 0)
            detail = _trunc(row["detail"], max(0, room)) if room > 0 else ""
            text += _pad(label, label_w) + ("  " + detail if detail else "")
        else:
            text += label
        if value_w and row["value"]:
            value = _trunc(row["value"], value_w)
            text = _pad(text, width - value_w) + _pad(value, value_w, "end")
        lines.append(text.rstrip())
    return lines


def _draw_list(node, width):
    props = _props(node)
    kids = [kid for kid in _kids(node) if _props(kid).get("hidden") is not True]
    if not kids:
        empty = _one(_t(props.get("empty"))).strip()
        return [empty] if empty else []
    selected = props.get("selected")
    out, rows = [], []
    for kid in kids:
        if kid.get("k") == "item":
            rows.append(_item_row(kid))
            continue
        if rows:
            out.extend(_item_lines(rows, width, selected))
            rows = []
        out.extend(_indent(_sub(kid, max(1, width - len(INDENT)))))
    if rows:
        out.extend(_item_lines(rows, width, selected))
    return out


def _draw_item(node, width):
    return _item_lines([_item_row(node)], width, None)


def _draw_tabs(node, width):
    props = _props(node)
    parts = []
    for item in _list(props.get("items")):
        if isinstance(item, dict):
            label = _one(_t(item.get("label")))
            parts.append("[%s]" % label if item.get("id") == props.get("active") else label)
    return _wrap(" ".join(parts), width) if parts else []


def _u16_index(text, offset):
    """The index in `text` of a UTF-16 offset (a split surrogate pair rounds down, past the end clamps)."""
    units = 0
    for index, ch in enumerate(text):
        following = units + (2 if ord(ch) > 0xFFFF else 1)
        if offset < following:
            return index
        units = following
    return len(text)


def _picker_columns(props, width):
    columns = [col for col in _list(props.get("columns")) if isinstance(col, dict) and col.get("id") not in (None, "")]
    return columns[:8]


def _picker_fact(value, column):
    if isinstance(value, bool):
        return ""
    number = _num(value) if isinstance(value, (int, float)) else None
    if number is not None:
        if column.get("format") == "bar" and 0 <= number <= 1:
            return _meter_cells({"value": number}, 4)
        return _fmt_count(number)
    return _one(_t(value))


def _draw_picker(node, width):
    props = _props(node)
    noun = _one(_t(props.get("noun"))) or "items"
    state = props.get("state") if props.get("state") in ("loading", "error") else "ready"
    catalog, order_ids = {}, []
    for item in _list(props.get("items")):
        if isinstance(item, dict) and item.get("id") not in (None, ""):
            catalog[str(item["id"])] = item
            order_ids.append(str(item["id"]))
    order = props.get("order") if isinstance(props.get("order"), list) else order_ids
    entries = []
    for entry in order:
        if isinstance(entry, dict) and "group" in entry:
            entries.append(("group", entry))
        elif isinstance(entry, (str, int)) and not isinstance(entry, bool) and str(entry) in catalog:
            entries.append(("item", catalog[str(entry)]))
    shown = sum(1 for kind, _entry in entries if kind == "item")
    selected = None if props.get("selected") is None else str(props.get("selected"))
    current = set(str(item) for item in _list(props.get("current")))
    columns = _picker_columns(props, width)
    # head: title, subtitle, count
    title = _one(_t(props.get("title"))) or "Select"
    subtitle = _one(_t(props.get("subtitle")))
    head = title + (" — " + subtitle if subtitle else "")
    total = _int(props.get("total"))
    if state == "loading":
        count = "…"
    elif total is not None and total != shown:
        count = "%d of %d" % (shown, total)
    else:
        count = str(shown)
    head = _trunc(head, max(1, width - _w(count) - 2))
    lines = [head + " " * max(2, width - _w(head) - _w(count)) + count if width > _w(head) + _w(count) + 1 else head]
    query = props.get("query")
    if isinstance(query, str):
        query = _clean(query).replace("\n", " ")
        if query == "":
            lines.append("⌕ " + (_one(_t(props.get("placeholder"))) or "Search %s…" % noun))
        else:
            cursor = _num(props.get("cursor"))
            at = len(query) if cursor is None else _u16_index(query, int(cursor))
            lines.append("⌕ " + query[:at] + ("▏" if props.get("focus") in (None, "list") else "") + query[at:])
    for key, active in (("scopes", props.get("scope")), ("tabs", props.get("tab"))):
        parts = []
        for item in _list(props.get(key)):
            if not isinstance(item, dict):
                continue
            label = _one(_t(item.get("label")))
            if _num(item.get("count")) is not None:
                label += " (%s)" % _fmt_count(item.get("count"))
            if item.get("disabled"):
                label += " ⊘"
            parts.append("[%s]" % label if item.get("id") == active else label)
        if parts:
            lines.extend(_wrap(("scopes: " if key == "scopes" else "") + "  ".join(parts), width))
    # fact columns, hidden by priority while the label would not keep room
    visible = list(columns)
    cells = {}
    for column in columns:
        values = []
        for kind, entry in entries:
            if kind == "item" and isinstance(entry.get("facts"), dict):
                values.append(_picker_fact(entry["facts"].get(column["id"]), column))
        cells[column["id"]] = values
    col_w = {}
    for column in columns:
        size = max([_w(_one(_t(column.get("head"))))] + [_w(v) for v in cells[column["id"]]] + [1])
        col_w[column["id"]] = min(size, 20)
    while visible and sum(col_w[c["id"]] + 1 for c in visible) > max(0, width - 30):
        victim = min(range(len(visible)), key=lambda k: ((_num(visible[k].get("priority"), 100) or 0), -k))
        visible.pop(victim)
    facts_w = sum(col_w[c["id"]] + 1 for c in visible)
    if any(_one(_t(c.get("head"))) for c in visible):
        lines.append(" " * max(0, width - facts_w) + " ".join(
            _pad(_trunc(_one(_t(c.get("head"))), col_w[c["id"]]), col_w[c["id"]],
                 "end" if c.get("format") in ("num", "price", "time", "elapsed") else "start") for c in visible)
            if facts_w else "")
    if state == "loading":
        lines.append("  … loading")
    elif state == "error":
        lines.append("  ⚠ " + (_one(_t(props.get("message"))) or "Error"))
    elif not shown:
        if isinstance(query, str) and query.strip():
            lines.append('  No %s match "%s"' % (noun, query.strip()))
        else:
            lines.append("  " + (_one(_t(props.get("empty"))) or "No %s yet" % noun))
    layout = props.get("layout") if props.get("layout") in ("cards", "timeline", "tree") else "rows"
    confirm = props.get("confirm") if isinstance(props.get("confirm"), dict) else None
    if state == "ready":
        for kind, entry in entries:
            if kind == "group":
                label = _one(_t(entry.get("label"))) or _one(_t(entry.get("group")))
                if _num(entry.get("count")) is not None:
                    label += " (%s)" % "{:,}".format(int(_num(entry.get("count"))))
                rule = "── " + label + " "
                lines.append(_trunc(rule + "─" * max(0, min(width, 80) - _w(rule)), width))
                continue
            item_id = str(entry.get("id"))
            is_sel = item_id == selected
            lead = ""
            mark = entry.get("mark") if isinstance(entry.get("mark"), dict) else None
            glyph = _icon_glyph(entry.get("icon")) or (
                _icon_glyph(str(entry.get("role")).split(".")[1]) if isinstance(entry.get("role"), str)
                and "." in entry["role"] else None)
            if mark and _one(_t(mark.get("text"))).strip():
                lead = "".join(_one(_t(mark.get("text"))).split())[:2].upper()
            elif glyph:
                lead = glyph
            elif entry.get("dot"):
                lead = "•"
            if layout == "timeline":
                node_kind = entry.get("node")
                lead = "▸" if node_kind == "user" else (glyph or "⚙") if node_kind == "tool" else "●"
            prefix = ("›" if is_sel else " ") + (("✓" if item_id in current else " ") if current else "") + " "
            if layout == "tree":
                depth = max(0, min(8, _int(entry.get("depth"), 0) or 0))
                prefix += "  " * depth + ("▾ " if entry.get("open") is True else "▸ " if entry.get("open") is False else "  ")
            text = prefix + (lead + " " if lead else "") + _one(_t(entry.get("label")))
            badges = [b for b in _list(entry.get("badges")) if isinstance(b, dict)]
            tags = ["[%s]" % _one(_t(b.get("text"))) for b in badges[:3]]
            if len(badges) > 3:
                tags.append("[+%d]" % (len(badges) - 3))
            for chip in _list(entry.get("chips")):
                if isinstance(chip, dict):
                    tags.append("(%s%s)" % ("✓" if chip.get("on") else "", _one(_t(chip.get("text")))))
            if tags:
                text += " " + " ".join(tags)
            detail = _one(_t(entry.get("detail")))
            if detail and layout != "cards":
                text += "  " + detail
            if entry.get("disabled"):
                text += " ⊘" + (" " + _one(_clean(entry["disabled"])) if isinstance(entry.get("disabled"), str) else "")
            facts = ""
            if visible:
                values = entry.get("facts") if isinstance(entry.get("facts"), dict) else {}
                facts = " ".join(_pad(_trunc(_picker_fact(values.get(c["id"]), c), col_w[c["id"]]), col_w[c["id"]],
                                      "end" if c.get("format") in ("num", "price", "time", "elapsed") else "start")
                                 for c in visible)
            if facts:
                text = _pad(_trunc(text, max(1, width - facts_w - 1)), width - facts_w) + facts
            lines.append(text.rstrip())
            if detail and layout == "cards":
                lines.append(" " * (len(prefix) + (_w(lead) + 1 if lead else 0)) + detail)
            if confirm and is_sel:
                lines.append("    ⚠ %s  [%s]  [Keep]" % (_one(_t(confirm.get("text"))),
                                                           _one(_t(confirm.get("label"))) or "Delete"))
    strip = props.get("strip") if isinstance(props.get("strip"), dict) else None
    if strip:
        chips = []
        for chip in _list(strip.get("items")):
            if isinstance(chip, dict):
                chips.append(("›" if chip.get("id") == strip.get("selected") else "") +
                             ("[x] " if chip.get("on") else "[ ] ") + _one(_t(chip.get("label"))))
        label = _one(_t(strip.get("label")))
        lines.extend(_wrap((label + ": " if label else "") + "  ".join(chips), width))
    actions = [a for a in _list(props.get("actions")) if isinstance(a, dict)]
    if actions:
        def button(action):
            keys = "+".join(_keycap(key) for key in _list(action.get("keys")) if isinstance(key, str))
            label = _one(_t(action.get("label"))) or _one(_clean(str(action.get("id") or "")))
            return "[ %s%s%s ]" % (("✓ " if action.get("on") else ""), label, (" " + keys) if keys else "")
        left = [button(a) for a in actions if not a.get("end") and not a.get("primary")]
        right = [button(a) for a in actions if a.get("end") and not a.get("primary")]
        right += [button(a) for a in actions if a.get("primary")]
        lines.append(_trunc(_spread(["  ".join(left), "  ".join(right)], width, "  "), width))
    preview = props.get("preview")
    kids = _kids(node)
    if kids and preview != "none":
        body = _children(node, max(1, width - len(INDENT)))
        if body:
            lines.append("── preview ──")
            lines.extend(_indent(body))
    return lines


# ---------------------------------------------------------------- motion

_SPINNERS = {"braille": "⠋", "starburst": "✻", "dots": "···", "orbit": "◜"}


def _draw_spinner(node, width):
    props = _props(node)
    glyph = _get(_SPINNERS, props.get("style"), _SPINNERS["braille"])
    return [(glyph + " " + _one(_t(props.get("label")))).rstrip()]


def _draw_shimmer(node, width):
    props = _props(node)
    spans = props.get("spans")
    text = _t(spans) if isinstance(spans, list) else _clean(props["text"]) if isinstance(props.get("text"), str) else ""
    return _wrap(text, width, "none") if text else [""]


def _draw_elapsed(node, width):
    props = _props(node)
    if props.get("stopped") is not None:
        shown = _num(props.get("stopped"), 0)
    else:
        age = _num(props.get("age"), 0) or 0
        shown = abs(age)                       # a negative age is a countdown from that many ms
    return [_format_age(shown, props.get("format") == "clock")]


def _draw_rate(node, width):
    props = _props(node)
    value = _num(props.get("value"), 0) or 0
    text = ("%.1f" % value) if abs(value) < 100 else ("%d" % round(value))
    unit = _one(_clean(str(props["unit"]))) if props.get("unit") not in (None, "") else ""
    return [(text + " " + unit).rstrip()]


def _draw_progress(node, width):
    props = _props(node)
    value = _num(props.get("value"))
    label = _one(_t(props.get("label")))
    percent = "" if value is None else "%d%%" % round(_clamp01(value) * 100)
    if percent and percent in label:
        percent = ""
    rest = " ".join(part for part in (percent, label) if part)
    size = max(4, min(20, width - _w(rest) - 3))
    return [_bar(value, size) + (" " + rest if rest else "")]


def _draw_meter(node, width):
    props = _props(node)
    style = props.get("style") if props.get("style") in ("ring", "blocks") else "bar"
    value = _meter_value(props)
    tail = " ".join(part for part in (_one(_t(props.get("label"))), _one(_t(props.get("total")))) if part)
    level = _meter_level(props)
    if level:
        tail = (tail + " (%s)" % level).strip()
    if style == "ring":
        shape = "◌" if value is None else _RING[int(round(_clamp01(value) * 4))]
        return [(shape + " " + tail).rstrip()]
    if style == "blocks":
        steps = _int(props.get("steps"))
        if steps is not None and 1 <= steps <= 20:
            filled = 0 if value is None else int(round(_clamp01(value) * steps))
            return [("▮" * filled + "▯" * (steps - filled) + " " + tail).rstrip()]
        rows = _get({"md": 5, "lg": 10}, props.get("size"), 2)
        cells = _meter_cells(props, rows * 10).replace("█", "■").replace("░", "□").replace("▓", "▣").replace("▒", "▤")
        lines = [cells[k * 10:(k + 1) * 10] for k in range(rows)]
        lines[0] = (lines[0] + " " + tail).rstrip()
        return lines
    size = 12
    cells = list(_meter_cells(props, size))
    for mark in _list(props.get("marks")):
        at = _num(mark.get("at")) if isinstance(mark, dict) else None
        if at is not None:
            cells[int(round(_clamp01(at) * (size - 1)))] = "┃"
    return [("[" + "".join(cells) + "]" + (" " + tail if tail else ""))]


_SPARK = "▁▂▃▄▅▆▇█"
_SHADES = {0: "·", 1: "░", 2: "▒", 3: "▓", 4: "█"}


def _chart_values(series):
    values = []
    for entry in series:
        number = _num(entry.get("value")) if isinstance(entry, dict) else (
            _num(entry) if not isinstance(entry, (list, tuple)) else _num(entry[1] if len(entry) > 1 else None))
        values.append(number)
    return values


def _chart_labels(series):
    return [_one(_t(entry.get("label"))) if isinstance(entry, dict) else
            (_one(_t(entry[0])) if isinstance(entry, (list, tuple)) and entry else "") for entry in series]


def _draw_chart(node, width):
    props = _props(node)
    kind = props.get("kind") if props.get("kind") in ("bars", "spark") else "heatmap"
    summary = _one(_t(props.get("summary")))
    lines = []
    if kind in ("bars", "spark"):
        series = _list(props.get("series"))
        values = _chart_values(series)
        top = max([v for v in values if v is not None and v > 0] + [0])
        if not values:
            pass
        elif kind == "spark":
            row = ""
            for value in values:
                row += _SPARK[0] if value is None or value <= 0 or top <= 0 else _SPARK[
                    max(0, min(7, int(round(value / top * 7))))]
            lines.append(row)
        else:
            height = _get({"sm": 2, "lg": 8}, props.get("size"), 4)
            labels = _chart_labels(series)
            column = max(2, max([_w(label) for label in labels] + [0]) + 1)
            if column * len(values) > width:
                column, labels = 2, []
            eighths = [1 if (v is None or v <= 0 or top <= 0) else max(1, int(round(v / top * height * 8)))
                       for v in values]
            for level in range(height - 1, -1, -1):
                row = ""
                for eighth in eighths:
                    rest = eighth - level * 8
                    glyph = " " if rest <= 0 else "█" if rest >= 8 else _SPARK[rest - 1]
                    row += glyph * (column - 1) + " "
                lines.append(row.rstrip())
            if any(labels):
                lines.append("".join(_pad(label, column) for label in labels).rstrip())
    else:
        cells = [row for row in _list(props.get("cells")) if isinstance(row, (list, tuple))]
        row_labels = [_one(_t(label)) for label in _list(props.get("rows"))]
        label_w = max([_w(label) for label in row_labels] + [0])
        gutter = label_w + 1 if label_w else 0
        months = [m for m in _list(props.get("cols")) if isinstance(m, dict) and _int(m.get("at")) is not None
                  and m.get("label") not in (None, "")]
        if months:
            line = [" "] * (gutter + max([_int(m["at"]) for m in months] + [0]) + 8)
            for month in months:
                label = _one(_t(month.get("label")))
                start = gutter + _int(month["at"])
                for offset, ch in enumerate(label):
                    if start + offset < len(line):
                        line[start + offset] = ch
            lines.append("".join(line).rstrip())
        for index, row in enumerate(cells):
            text = ""
            for cell in row:
                number = _num(cell)
                if number is None:
                    text += " "
                else:
                    text += _SHADES[0 if number <= 0 else 1 if number <= 0.25 else 2 if number <= 0.5
                                    else 3 if number <= 0.75 else 4]
            label = row_labels[index] if index < len(row_labels) else ""
            lines.append((_pad(label, label_w) + " " + text if label_w else text).rstrip())
    if summary:
        lines.append(summary)
    return lines or ["[chart]"]


_EFFORT = {"off": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}


def _draw_effort(node, width):
    level = _props(node).get("level")
    if isinstance(level, str) and level in _EFFORT:
        rung = _EFFORT[level]
        return ["effort: %s %s" % (level, "▰" * rung + "▱" * (6 - rung))]
    return ["effort: %s" % (_one(_clean(plain(level) if isinstance(level, (list, dict)) else str(level)))
                            if level not in (None, "") else "?")]


# ---------------------------------------------------------------- inputs

def _sgr_change(before, after):
    out = ""
    if before & 1 and not after & 1:
        out += "\x1b[27m"
    elif after & 1 and not before & 1:
        out += "\x1b[7m"
    if before & 2 and not after & 2:
        out += "\x1b[22m"
    elif after & 2 and not before & 2:
        out += "\x1b[2m"
    return out


def _emit_cells(cells):
    out, state = [], 0
    for ch, flags in cells:
        flags &= 3 if ALLOW_SGR else 0
        if flags != state:
            out.append(_sgr_change(state, flags))
            state = flags
        out.append(ch)
    if state:
        out.append(_sgr_change(state, 0))
    return "".join(out)


def _cells_width(cells):
    return sum(_cw(ch) for ch, _flags in cells)


def _cells_window(cells, room, caret):
    """The part of one line that stays visible in `room` cells, scrolled to keep the caret in view."""
    total = _cells_width(cells)
    if total <= room:
        return cells
    start = 0
    if caret is not None:
        column = _cells_width(cells[:caret])
        if column >= room - 1:
            start = column - room + 2
    kept, column = [], 0
    for cell in cells:
        size = _cw(cell[0])
        if column >= start and column + size <= start + room:
            kept.append(list(cell))
        column += size
    if kept and start > 0:
        kept[0] = [_ELL, 0]
    if kept and start + room < total:
        kept[-1] = [_ELL, 0]
    return kept


def _draw_editor(node, width):
    props = _props(node)
    raw = plain(props.get("text"))
    ghost = _clean(plain(props.get("ghost"))).replace("\n", " ")
    placeholder = _one(_t(props.get("placeholder")))
    prompt = _one(_t(props.get("prompt"))) or "> "
    readonly = _flag(props, "readonly")
    show_caret = not readonly
    cursor = _num(props.get("cursor"))
    caret = len(raw) if cursor is None else _u16_index(raw, int(max(0, cursor)))
    anchor = _num(props.get("anchor"))
    low = high = None
    if anchor is not None:
        other = _u16_index(raw, int(max(0, anchor)))
        if other != caret:
            low, high = min(other, caret), max(other, caret)
    cells = []

    def add_caret(following):
        """Ghost completion and caret at the caret position; True when the next character wears the caret."""
        if not ALLOW_SGR:
            if show_caret:
                cells.append(["▏", 4])
            cells.extend([ch, 0] for ch in ghost)
            return False
        if ghost:
            for number, ch in enumerate(ghost):
                cells.append([ch, 2 | (1 | 4 if number == 0 and show_caret else 0)])
            return False
        if show_caret:
            if following is not None and following != "\n":
                return True
            cells.append([" ", 1 | 4])
        return False

    wear = False
    for index, ch in enumerate(raw):
        if index == caret:
            wear = add_caret(ch)
        text = _clean(ch)
        flag = (1 if low is not None and low <= index < high and ALLOW_SGR else 0)
        if ch == "\n":
            cells.append(["\n", 0])
            wear = False
            continue
        for piece in text:
            cells.append([piece, flag | (1 | 4 if wear else 0)])
            wear = False
    if caret >= len(raw):
        add_caret(None)
    if not raw and not ghost and placeholder:
        cells = [[ch, 2] for ch in placeholder]
        if show_caret and ALLOW_SGR:
            cells[0][1] |= 1 | 4
        elif show_caret:
            cells.insert(0, ["▏", 4])
    # logical lines
    logical, current = [], []
    for cell in cells:
        if cell[0] == "\n":
            logical.append(current)
            current = []
        else:
            current.append(cell)
    logical.append(current)
    gutter = [_one(_t(entry)) for entry in _list(props.get("gutter"))]
    virt = [_one(_t(entry)) for entry in _list(props.get("virt"))]
    gutter_w = max([_w(entry) for entry in gutter] + [0])
    prompt_w = _w(prompt)
    rows, caret_row, first_rows = [], 0, []
    single = node.get("k") == "input" or _flag(props, "nowrap")
    for number, line in enumerate(logical):
        lead = (prompt if number == 0 else " " * prompt_w) + (_pad(gutter[number] if number < len(gutter) else "",
                                                                    gutter_w) if gutter_w else "")
        room = max(1, width - _w(lead))
        if single:
            marker = next((k for k, cell in enumerate(line) if cell[1] & 4), None)
            pieces = [_cells_window(line, room, marker)]
        else:
            pieces, piece, used = [], [], 0
            for cell in line:
                size = _cw(cell[0])
                if piece and used + size > room:
                    pieces.append(piece)
                    piece, used = [], 0
                piece.append(cell)
                used += size
            pieces.append(piece)
        first_rows.append(len(rows))
        for k, piece in enumerate(pieces):
            text = (lead if k == 0 else " " * _w(lead)) + _emit_cells(piece)
            if any(cell[1] & 4 for cell in piece):
                caret_row = len(rows)
            if k == len(pieces) - 1 and number < len(virt) and virt[number]:
                text += "  " + virt[number]
            rows.append(text)
    mode = _one(_t(props.get("mode"))).strip()
    if mode and rows:
        rows[-1] += "  [%s]" % mode.upper()
    limit = _int(props.get("maxLines"), 0) or 0
    if node.get("k") == "editor" and limit > 0 and len(rows) > limit:
        top = _int(props.get("topline"))
        if top is not None and 0 <= top < len(first_rows):
            start = first_rows[top]
        else:
            start = max(0, caret_row - limit + 1)
        start = max(0, min(start, len(rows) - limit))
        rows = rows[start:start + limit]
    return [_trunc(row, width) for row in rows]


# ---------------------------------------------------------------- chrome

def _seg_text(node, width):
    props = _props(node)
    glyph = _icon_glyph(props.get("icon")) or ""
    spans = props.get("spans")
    text = _one(_t(spans)) if isinstance(spans, list) else (
        _one(_clean(props["text"])) if isinstance(props.get("text"), str) else "")
    inner = " ".join(line.strip() for line in _children(node, width) if line.strip())
    return " ".join(part for part in (glyph, inner, text) if part)


def _draw_seg(node, width):
    text = _seg_text(node, width)
    return [text] if text else []


def _draw_status(node, width):
    sep = " · "
    entries = []              # [text, priority, rank, side]
    counts = {"left": 0, "right": 0}
    for child in _kids(node):
        props = _props(child)
        if props.get("hidden") is True:
            continue
        if child.get("k") == "seg":
            text = _seg_text(child, width)
            priority = _num(props.get("priority"), 0) or 0
            side = "right" if props.get("side") == "right" else "left"
        else:
            text = " ".join(line.strip() for line in _sub(child, width) if line.strip())
            priority, side = float("inf"), "left"
        if not text:
            continue
        rank = 2 * counts[side] + (1 if side == "right" else 0)
        counts[side] += 1
        entries.append([text, priority, rank, side])

    def parts():
        return [sep.join(e[0] for e in entries if e[3] == side) for side in ("left", "right")]

    while len(entries) > 1 and sum(_w(part) for part in parts()) + len(sep) > width:
        droppable = [e for e in entries if e[1] != float("inf")]
        if not droppable:
            break
        entries.remove(min(droppable, key=lambda e: (e[1], -e[2])))
    if not entries:
        return []
    left, right = parts()
    if not right:
        return [_trunc(left, width)]
    if not left:
        return [_pad(_trunc(right, width), width, "end")]
    return [_trunc(_spread([left, right], width, sep), width)]


def _draw_toast(node, width):
    props = _props(node)
    text = _one(_t(props.get("text")))
    sub = _clean(plain(props.get("sub"))).split("\n")[0][:120].strip()
    tone = props.get("tone")
    icon = ""
    if tone is not None:
        icon = "⚠" if tone == "error" else "ℹ" if tone in ("info", "accent") else "✓"
    inner = " ".join(part for part in (icon, text, ("  " + sub) if sub else "") if part)
    return ["[ %s ]" % inner]


def _draw_overlay(node, width):
    head = _one(_t(_props(node).get("head"))).strip()
    return ([head] if head else []) + _children(node, width)


# ---------------------------------------------------------------- work kinds

def _target_text(props):
    target = props.get("target")
    if target is None:
        return ""
    text = _one(_t(target)).strip()
    if isinstance(target, str) and props.get("targetKind") in ("pattern", "query") and text:
        return "“%s”" % text
    return text


def _meta_texts(props):
    return [_one(_t(item)).strip() for item in _list(props.get("meta")) if _one(_t(item)).strip()]


def _badge_texts(items):
    return ["[%s]" % _one(_t(item.get("text"))) for item in _list(items) if isinstance(item, dict)
            and _one(_t(item.get("text")))]


def _draw_tool(node, width):
    props = _props(node)
    status = props.get("status") if isinstance(props.get("status"), str) and props.get("status") in _MARKS else "pending"
    folded = _is_folded(props)
    head = [_MARKS[status]]
    title = _one(_t(props.get("title"))).strip()
    target = _target_text(props)
    head += [part for part in (title, target) if part]
    meta = _meta_texts(props)
    if meta:
        head.append("  " + "  ".join(meta))
    head.extend(_badge_texts(props.get("badges")))
    note = _one(_t(props.get("note"))).strip()
    if note:
        head.append(note)
    code = _num(props.get("exit"))
    if code:
        head.append("exit %d" % code)
    live = status in ("running", "pending")
    stamp = _num(props.get("age")) if live else (_num(props.get("took")) if _num(props.get("took")) is not None
                                                 else _num(props.get("age")))
    if stamp is not None:
        head.append(_format_age(stamp))
    if _flag(props, "collapsible"):
        head.insert(0, "▸" if folded else "▾")
    lines = _hang_wrap(" ".join(head), width, INDENT)
    inline = props.get("frame") == "inline"
    room = max(1, width - 2)
    body = _body_lines(node, room, None, _flag(props, "collapsible") and not folded)
    if folded:
        preview = props.get("preview")
        tail = _int(preview.get("tail"), 0) if isinstance(preview, dict) else 0
        count = _int(preview.get("lines"), 0) if isinstance(preview, dict) else 0
        if tail and tail >= 1:
            hidden = max(0, len(body) - tail)
            body = ([_more_lines(hidden, True)] if hidden else []) + body[-tail:]
        elif count and count >= 1:
            hidden = max(0, len(body) - count)
            body = body[:count] + ([_more_lines(hidden)] if hidden else [])
        else:
            body = []
    prefix = "│ " if inline else INDENT
    return lines + [(prefix + line).rstrip() for line in body]


def _agent_stats(props, status):
    stats = props.get("stats") if isinstance(props.get("stats"), dict) else {}
    parts = []
    model = _one(_clean(str(props["model"]))).rsplit("/", 1)[-1] if props.get("model") else ""
    if model:
        parts.append(model)
    if (_num(stats.get("tools"), 0) or 0) > 0:
        parts.append("⚒ %s" % _fmt_count(stats.get("tools")))
    if (_num(stats.get("requests"), 0) or 0) > 0:
        parts.append("%s req" % _fmt_count(stats.get("requests")))
    if status == "running" and _num(stats.get("done")) is not None:
        parts.append("%d%%" % round(_clamp01(_num(stats.get("done"))) * 100))
    context = _num(stats.get("context"))
    if context is not None:
        fraction = context / 100.0 if context > 1 else context
        label = _one(_t(stats.get("contextLabel"))) or "%d%% context" % round(_clamp01(fraction) * 100)
        parts.append(_bar(fraction, 6) + " " + label)
    elif _num(stats.get("tokens")) is not None:
        parts.append("%s tok" % _fmt_count(stats.get("tokens")))
    cost = _num(stats.get("cost"))
    if cost is not None and cost > 0:
        parts.append("<$0.01" if cost < 0.01 else "$%.2f" % cost)
    live = status in ("running", "pending")
    stamp = _num(stats.get("age")) if live else (_num(stats.get("took")) if _num(stats.get("took")) is not None
                                                 else _num(stats.get("age")))
    if stamp is not None:
        parts.append(_format_age(stamp))
    return " · ".join(parts)


def _agent_lines(node, width):
    props = _props(node)
    status = props.get("status") if props.get("status") in (
        "pending", "running", "done", "failed", "aborted", "idle", "parked") else "pending"
    folded = _is_folded(props)
    glyph = {"running": "●", "done": "✓", "failed": "✗", "aborted": "⊘"}.get(status, "○")
    name = _one(_t(props.get("name"))) or "agent"
    head = "%s %s (%s)" % (glyph, name, status)
    if props.get("agent") not in (None, ""):
        head += " [%s]" % _one(_clean(str(props["agent"])))
    badges = _badge_texts(props.get("badges"))
    if badges:
        head += " " + " ".join(badges)
    task = _one(_t(props.get("task"))).strip()
    if task:
        head += " " + task
    if _flag(props, "collapsible"):
        head = ("▸ " if folded else "▾ ") + head
    stats = _agent_stats(props, status)
    if stats and _w(head) + 2 + _w(stats) <= width:
        lines = [head + " " * (width - _w(head) - _w(stats)) + stats]
    else:
        lines = _hang_wrap(head, width, INDENT) + ([INDENT + stats] if stats else [])
    retry = props.get("retry") if isinstance(props.get("retry"), dict) else None
    tool = props.get("tool") if isinstance(props.get("tool"), dict) else None
    if retry:
        left = max(0, (_num(retry.get("delay"), 0) or 0) - (_num(retry.get("age"), 0) or 0))
        lines.append(INDENT + "↻ retry %s/%s in %s" % (_int(retry.get("attempt"), 1), _int(retry.get("max"), 1),
                                                       _format_age(left, True)))
    elif tool and status == "running":
        parts = [_icon_glyph(tool.get("name")) or "", _one(_t(tool.get("name"))), _one(_t(tool.get("intent")))]
        if _num(tool.get("age")) is not None:
            parts.append(_format_age(tool.get("age")))
        text = " ".join(part for part in parts if part)
        if text:
            lines.append(INDENT + text)
    if not folded:
        lines += _indent(_body_lines(node, max(1, width - len(INDENT)), None, _flag(props, "collapsible")))
    return lines


def _draw_agent(node, width):
    pad = " " * (2 * max(0, min(6, _int(_props(node).get("depth"), 0) or 0)))
    return [(pad + line) if line else line for line in _agent_lines(node, max(1, width - len(pad)))]


_CHECK = {"done": "[x]", "active": "[>]", "blocked": "[!]", "dropped": "[-]", "pending": "[ ]"}


def _phase_lines(phases, width, only_open=False):
    lines = []
    shown = [phase for phase in phases if isinstance(phase, dict)]
    for phase in shown:
        items = [item for item in _list(phase.get("items")) if isinstance(item, dict)]
        if only_open:
            items = [item for item in items if item.get("status") in ("pending", "active", "blocked")
                     or not (isinstance(item.get("status"), str) and item.get("status") in _CHECK)]
        finished = sum(1 for item in _list(phase.get("items")) if isinstance(item, dict)
                       and item.get("status") in ("done", "dropped"))
        total = sum(1 for item in _list(phase.get("items")) if isinstance(item, dict))
        title = _one(_t(phase.get("title"))).strip()
        indent = INDENT
        if title or len(shown) > 1:
            lines.append(_trunc((title + "  " if title else "") + "%d/%d" % (finished, total), width))
        else:
            indent = ""
        if phase.get("collapsed") is True:
            continue
        for item in items:
            mark = _get(_CHECK, item.get("status"), "[ ]")
            text = indent + mark + " " + _one(_t(item.get("text")))
            lines.extend(_hang_wrap(text, width, indent + "    "))
            note = _one(_t(item.get("note"))).strip()
            if note:
                lines.extend(_wrap(indent + "    " + note, width))
    return lines


def _draw_checklist(node, width):
    props = _props(node)
    phases = [phase for phase in _list(props.get("phases")) if isinstance(phase, dict)]
    mode = props.get("mode") if props.get("mode") in ("hud", "reminder") else "full"
    items = [item for phase in phases for item in _list(phase.get("items")) if isinstance(item, dict)]
    total = len(items)
    finished = sum(1 for item in items if item.get("status") in ("done", "dropped"))
    if mode == "hud":
        pick = next((i for i in items if i.get("status") == "active"), None) or next(
            (i for i in items if i.get("status", "pending") == "pending"), None)
        label = _one(_t(pick.get("text"))) if pick else ("All done" if total else "Todos")
        return ["[%s %s %d/%d]" % (_RING[int(round(finished / total * 4))] if total else "○", label, finished, total)]
    if mode == "reminder":
        open_count = sum(1 for item in items if item.get("status") in ("pending", "active", "blocked"))
        head = "⚠ %d todo%s still open" % (open_count, "" if open_count == 1 else "s")
        note = _one(_t(props.get("note"))).strip()
        return [_trunc(head + ("  " + note if note else ""), width)] + _phase_lines(phases, width, True)
    return _phase_lines(phases, width)


def _draw_block(node, width):
    props = _props(node)
    kids = _kids(node)
    raw = next((k for k in kids if k.get("id") == "raw"), None) or next(
        (k for k in reversed(kids) if k.get("k") == "ansi"), None)
    native = next((k for k in kids if k is not raw), None)
    shown = native if native is not None else raw
    command = _one(_t(props.get("cmd"))) or _one(_t(props.get("line")))
    running = props.get("state") == "running"
    lines = []
    if command or running:
        lines.append(_trunc(("$ " + command + ("  ●" if running else "")).strip(), width))
    if shown is not None:
        lines.extend(_sub(shown, width))
    foot = []
    code = _num(props.get("exit"))
    if not running and code:
        foot.append("exit %d" % code)
    took = _num(props.get("took"))
    if not running and took is not None and took > 1000:
        foot.append(_format_age(took))
    if foot:
        lines.append(" · ".join(foot))
    return lines


def _prefs_control(control, row):
    kind = control.get("k")
    if kind == "switch":
        return "[x] on" if control.get("on") is True else "[ ] off"
    if kind == "choice":
        options = [o for o in _list(control.get("options")) if isinstance(o, dict)]
        parts = [("[%s]" if o.get("value") == control.get("value") else "%s") % (
            _one(_t(o.get("label"))) or _one(_clean(str(o.get("value"))))) for o in options]
        return "  ".join(parts) if parts else _one(_clean(str(control.get("value") or "")))
    if kind == "number":
        value = _num(control.get("value"), 0)
        labels = control.get("labels") if isinstance(control.get("labels"), dict) else {}
        key = ("%d" % value) if value == int(value) else ("%g" % value)
        label = _one(_t(labels.get(key))) if key in labels else key + (
            (" " + _one(_clean(str(control["unit"])))) if control.get("unit") else "")
        return "− %s +" % label
    if kind == "text":
        value = _one(_t(control.get("value")))
        if control.get("secret") is True and value:
            return "•" * min(12, len(value))
        return value or _one(_t(control.get("placeholder"))) or "Not set"
    if kind == "keys":
        chords = ["+".join(_keycap(k) for k in chord if isinstance(k, str)) for chord in _list(control.get("keys"))
                  if isinstance(chord, (list, tuple))]
        return "  ".join("[%s]" % chord for chord in chords)
    if kind == "multi":
        values = [str(v) for v in _list(control.get("values"))]
        options = [o for o in _list(control.get("options")) if isinstance(o, dict)]
        if control.get("ordered") is True:
            ranked = [o for v in values for o in options if str(o.get("value")) == v]
            rest = [o for o in options if str(o.get("value")) not in values]
            parts = ["%d. [x] %s" % (n, _one(_t(o.get("label")))) for n, o in enumerate(ranked, 1)]
            parts += ["[ ] %s" % _one(_t(o.get("label"))) for o in rest]
        else:
            parts = [("[x] " if str(o.get("value")) in values else "[ ] ") + _one(_t(o.get("label")))
                     for o in options]
        return "  ".join(parts)
    if kind == "action":
        return "[ %s ]" % (_one(_t(control.get("label"))) or "Edit…")
    return ""


def _draw_prefs(node, width):
    props = _props(node)
    title = _one(_t(props.get("title"))) or "Settings"
    lines = [title]
    pages = [page for page in _list(props.get("pages")) if isinstance(page, dict)]
    if pages:
        parts = []
        for page in pages:
            label = _one(_t(page.get("label"))) or _one(_clean(str(page.get("id") or "")))
            if _int(page.get("changed"), 0) and page.get("changed") is not True:
                label += " (%d)" % _int(page.get("changed"))
            if page.get("disabled"):
                label += " ⊘"
            parts.append("[%s]" % label if page.get("id") == props.get("page") else label)
        lines.extend(_wrap("  ".join(parts), width))
    query = _clean(plain(props.get("query"))).strip()
    if query:
        lines.append("⌕ " + query)
    current = next((p for p in pages if p.get("id") == props.get("page")), None)
    if current is not None:
        number = pages.index(current) + 1
        lines.append("%02d · %s" % (number, _one(_t(current.get("label"))) or _one(_clean(str(current.get("id"))))))
    lead = _one(_t(props.get("lead"))).strip()
    if lead:
        lines.extend(_wrap(lead, width))
    editing = props.get("editing") if isinstance(props.get("editing"), dict) else {}
    for section in _list(props.get("sections")):
        if not isinstance(section, dict):
            continue
        rows = [row for row in _list(section.get("rows")) if isinstance(row, dict)]
        if not rows:
            continue
        heading = _one(_t(section.get("title"))).strip()
        lines.append("")
        if heading:
            lines.append(INDENT + heading)
        for row in rows:
            ident = _one(_clean(str(row.get("id") or "")))
            label = _one(_t(row.get("label"))) or ident
            marks = ("›" if props.get("focus") in (row.get("id"), section.get("id")) and props.get("focus") is not None
                     else " ") + ("●" if row.get("changed") is True else " ") + ("⚠" if row.get("warning") else " ")
            control = row.get("control") if isinstance(row.get("control"), dict) else {}
            text = _prefs_control(control, row)
            if editing.get("row") == row.get("id") and control.get("k") == "text":
                draft = _one(_t(editing.get("draft")))
                cursor = _int(editing.get("cursor"))
                text = draft[:cursor] + "▏" + draft[cursor:] if cursor is not None else draft + "▏"
            head = INDENT + marks + " " + label
            if text:
                room = max(1, width - _w(head) - 2)
                lines.append(_trunc(head, width - 2) + "  " + _trunc(text, room) if _w(head) + 2 + _w(text) <= width
                             else _trunc(head, width))
                if _w(head) + 2 + _w(text) > width:
                    lines.extend(_hang_wrap(INDENT * 3 + text, width, INDENT * 3))
            else:
                lines.append(_trunc(head, width))
            hint = _one(_t(row.get("hint"))).strip()
            if hint:
                lines.extend(_wrap(INDENT * 3 + hint, width))
            warning = _one(_t(row.get("warning"))).strip()
            if warning:
                lines.extend(_wrap(INDENT * 3 + "⚠ " + warning, width))
            if isinstance(row.get("disabled"), str) and row["disabled"]:
                lines.extend(_wrap(INDENT * 3 + "(" + _one(_clean(row["disabled"])) + ")", width))
            if row.get("changed") is True and row.get("defaultLabel") not in (None, ""):
                lines.extend(_wrap(INDENT * 3 + "default: " + _one(_t(row.get("defaultLabel"))), width))
    lines.extend(_children(node, width))
    return lines


# ---------------------------------------------------------------- el (HTML elements)

_EL_INLINE_TAGS = frozenset("span label a b i em strong code small kbd u s del mark button td th input sub sup abbr "
                            "cite q time var samp ins".split())
_EL_INLINE_KINDS = frozenset(("badge", "kbd", "icon", "spinner", "shimmer", "elapsed", "rate", "effort"))
_EL_MARGIN = frozenset("p h1 h2 h3 h4 h5 h6 ul ol dl blockquote pre figure".split())
_EL_NUMBER = re.compile(r"^[-\d.,]+[KMGT]?$")


def _el_tag(node):
    return str(_props(node).get("tag") or "div").lower() if node.get("k") == "el" else ""


def _el_is_inline(node):
    kind = node.get("k")
    return kind in _EL_INLINE_KINDS or (kind == "el" and _el_tag(node) in _EL_INLINE_TAGS)


def _el_text(props):
    return re.sub(r"[ \t\n]+", " ", _clean(plain(props.get("spans") if props.get("spans") else props.get("text"))))


def _el_input(props):
    kind = str(props.get("type") or "text").lower()
    if kind == "radio":
        return "(o)" if props.get("checked") is True else "( )"
    if kind == "checkbox":
        return "[x]" if props.get("checked") is True else "[ ]"
    return "[%s]" % (_one(_t(props.get("value"))) or _one(_t(props.get("placeholder"))) or "____")


def _el_inline_text(node):
    if _props(node).get("hidden") is True:
        return ""
    if node.get("k") != "el":
        return " ".join(line.strip() for line in _sub(node, _WIDE) if line.strip())
    props = _props(node)
    tag = _el_tag(node)
    if tag == "br":
        return "\n"
    if tag == "input":
        return _el_input(props)
    parts = []
    text = _el_text(props)
    if text:
        parts.append(text)
    for child in _kids(node):
        if _props(child).get("hidden") is True:
            continue
        if _el_is_inline(child):
            parts.append(_el_inline_text(child))
        else:
            parts.append(" ".join(line.strip() for line in _sub(child, _WIDE) if line.strip()))
    if tag in ("label", "td", "th"):
        joined = " ".join(part.strip() for part in parts if part.strip())
    else:
        joined = "".join(parts)
    if tag == "label":
        return " " + joined.strip() + " "
    if tag == "button":
        return " [ %s ] " % joined.strip()
    if tag == "a":
        href = _one(_t(props.get("href"))).strip()
        if href and href != joined.strip():
            return "%s (%s)" % (joined.strip(), href)
    return joined


def _el_run_lines(run, width):
    segments = run.split("\n")
    lines = []
    for number, segment in enumerate(segments):
        segment = re.sub(r" {2,}", " ", segment.strip())
        if segment:
            lines.extend(_wrap(segment, width))
        elif 0 < number < len(segments) - 1:
            lines.append("")
    return lines


def _el_margin(tag, parent):
    return tag in _EL_MARGIN and not (parent == "li" and tag in ("ul", "ol"))


def _el_flow(node, width):
    """The content of an element: its `text`, then its children; inline ones run together and wrap, block ones stack."""
    parent = _el_tag(node)
    blocks = []
    run = _el_text(_props(node))
    for child in _kids(node):
        if _props(child).get("hidden") is True:
            continue
        if _el_is_inline(child):
            run += _el_inline_text(child)
            continue
        if run.strip() or "\n" in run.strip(" "):
            blocks.append((_el_run_lines(run, width), ""))
        run = ""
        lines = _sub(child, width)
        if lines:
            blocks.append((lines, _el_tag(child)))
    if run.strip():
        blocks.append((_el_run_lines(run, width), ""))
    out, previous = [], ""
    for number, (lines, tag) in enumerate(blocks):
        if number and out and (_el_margin(tag, parent) or _el_margin(previous, parent)):
            if out[-1] != "" and lines and lines[0] != "":
                out.append("")
        out.extend(lines)
        previous = tag
    return out


def _el_raw(node):
    props = _props(node)
    text = _clean(plain(props.get("text")))
    for child in _kids(node):
        if _props(child).get("hidden") is True:
            continue
        text += _el_raw(child) if child.get("k") == "el" else " ".join(_sub(child, _WIDE))
    return text


def _el_cell_rows(node, width, header=False):
    """Rows of an el table, found at any depth: [(cells, is_header)]."""
    rows = []
    for child in _kids(node):
        if child.get("k") != "el" or _props(child).get("hidden") is True:
            continue
        tag = _el_tag(child)
        if tag == "tr":
            cells, all_th = [], True
            for cell in _kids(child):
                if _props(cell).get("hidden") is True:
                    continue
                if _el_tag(cell) != "th":
                    all_th = False
                text = " ".join(line.strip() for line in _el_flow(cell, width) if line.strip()) \
                    if cell.get("k") == "el" else " ".join(line.strip() for line in _sub(cell, width) if line.strip())
                cells.append(text)
                attrs = _props(cell).get("attrs")
                span = _int(attrs.get("colspan"), 1) if isinstance(attrs, dict) else 1
                cells.extend([""] * max(0, min(span, 20) - 1))
            rows.append((cells, header or (all_th and bool(cells))))
        else:
            rows.extend(_el_cell_rows(child, width, header or tag == "thead"))
    return rows


def _el_table(node, width):
    rows = _el_cell_rows(node, width)
    if not rows:
        return None
    columns = max(len(cells) for cells, _head in rows)
    sizes = [max([_w(cells[i]) for cells, _head in rows if i < len(cells)] + [0]) for i in range(columns)]
    while sum(sizes) + 2 * (columns - 1) > width and max(sizes) > 3:
        sizes[sizes.index(max(sizes))] -= 1
    lines, header_done = [], False
    for cells, is_head in rows:
        parts = []
        for i, cell in enumerate(cells):
            cell = _trunc(cell, sizes[i])
            numeric = _EL_NUMBER.match(cell) and cell != "-" and not is_head
            parts.append(_pad(cell, sizes[i], "end" if numeric else "start"))
        lines.append("  ".join(parts).rstrip())
        if is_head and not header_done:
            lines.append("  ".join("─" * size for size in sizes))
            header_done = True
        elif not is_head:
            header_done = True
    return lines


def _el_list(node, width, ordered):
    depth = getattr(_ctx, "lists", 0)
    _ctx.lists = depth + 1
    try:
        attrs = _props(node).get("attrs")
        number = _int(attrs.get("start"), 1) if isinstance(attrs, dict) and ordered else 1
        entries = []
        for child in _kids(node):
            if _props(child).get("hidden") is True:
                continue
            if _el_tag(child) == "li":
                mark = ("%d." % number) if ordered else ("•", "◦", "▪")[min(depth, 2)]
                number += 1
                entries.append((mark, child))
            else:
                entries.append((None, child))
        mark_w = max([_w(mark) for mark, _child in entries if mark] + [1]) + 1
        out = []
        text = _el_text(_props(node))
        if text.strip():
            out.extend(_wrap(text.strip(), width))
        for mark, child in entries:
            if mark is None:
                out.extend(_sub(child, width))
                continue
            inner = _el_flow(child, max(1, width - mark_w))
            if not inner:
                out.append(mark)
                continue
            out.append(_pad(mark, mark_w - 1, "end" if ordered else "start") + " " + inner[0])
            out.extend((" " * mark_w + line) if line else line for line in inner[1:])
        return out
    finally:
        _ctx.lists = depth


def _draw_el(node, width):
    """An HTML element node (the Tern SDK's `el`)."""
    props = _props(node)
    tag = _el_tag(node)
    if tag in _EL_INLINE_TAGS and tag not in ("td", "th"):
        return _el_run_lines(_el_inline_text(node), width)
    if tag == "br":
        return [""]
    if tag == "hr":
        return ["─" * min(width, 40)]
    if tag == "table":
        grid = _el_table(node, width)
        if grid is not None:
            return grid
    if tag in ("tr", "td", "th"):
        cells = []
        if tag != "tr":
            return _el_run_lines(_el_inline_text(node), width)
        for cell in _kids(node):
            if _props(cell).get("hidden") is True:
                continue
            text = " ".join(line.strip() for line in (_el_flow(cell, width) if cell.get("k") == "el"
                                                       else _sub(cell, width)) if line.strip())
            if text:
                cells.append(text)
        own = _el_text(props).strip()
        return [_trunc("  ".join(([own] if own else []) + cells), width)] if own or cells else []
    if tag == "pre":
        return [piece for line in _el_raw(node).split("\n") for piece in _hard_wrap(line, width)]
    if tag in ("ul", "ol"):
        return _el_list(node, width, tag == "ol")
    if tag == "li":
        inner = _el_flow(node, max(1, width - 2))
        return ["• " + inner[0]] + _indent(inner[1:]) if inner else []
    if tag == "blockquote":
        return [("│ " + line).rstrip() for line in _el_flow(node, max(1, width - 2))]
    if tag == "dd":
        return _indent(_el_flow(node, max(1, width - 4)), "    ")
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        text = " ".join(line.strip() for line in _el_flow(node, _WIDE) if line.strip())
        lines = _wrap(text, width)
        if tag == "h1" and lines:
            return lines + ["═" * min(width, max(_w(line) for line in lines))]
        if tag == "h2" and lines:
            return lines + ["─" * min(width, max(_w(line) for line in lines))]
        return lines
    return _el_flow(node, width)


# ---------------------------------------------------------------- dispatch and public API

# One entry per kind of the vocabulary (tsp_document.TSP_KINDS and TSP_EXTRA_KINDS); a kind missing here is
# drawn as its children.
_DRAWERS = {
    "el": _draw_el, "media": _draw_media, "block": _draw_block,
    "col": _draw_col, "row": _draw_row, "card": _draw_card,
    "section": _draw_section, "rule": _draw_rule, "spacer": _draw_spacer,
    "text": _draw_text, "md": _draw_md, "math": _draw_math,
    "code": _draw_code, "diff": _draw_diff, "ansi": _draw_ansi,
    "image": _draw_image, "kv": _draw_kv, "table": _draw_table, "tree": _draw_tree,
    "badge": _draw_badge, "kbd": _draw_kbd, "icon": _draw_icon,
    "spinner": _draw_spinner, "shimmer": _draw_shimmer, "elapsed": _draw_elapsed,
    "progress": _draw_progress, "rate": _draw_rate, "list": _draw_list, "item": _draw_item,
    "tabs": _draw_tabs, "editor": _draw_editor, "input": _draw_editor,
    "status": _draw_status, "seg": _draw_seg, "overlay": _draw_overlay,
    "toast": _draw_toast, "rows": _draw_rows, "picker": _draw_picker,
    "prefs": _draw_prefs, "tool": _draw_tool, "checklist": _draw_checklist,
    "agent": _draw_agent, "chart": _draw_chart, "meter": _draw_meter,
    "effort": _draw_effort,
}


def _sanitize(line):
    """A line without escape sequences or control characters (the editor's own SGR codes survive)."""
    if ALLOW_SGR and "\x1b" in line:
        return "".join(part if number % 2 else _clean(part) for number, part in enumerate(_SGR_SPLIT.split(line)))
    return _clean(line)


def _final(line, width):
    """One output line: clean, at most `width` cells (cut with an ellipsis), no trailing blanks."""
    if not isinstance(line, str):
        line = str(line)
    if "\x1b" in line or not line.isprintable():
        line = _sanitize(line)
    if _w(line) > width:
        line = _trunc(line, width)
    return line.rstrip(" ")


def _draw(node, width):
    """The lines of one node (any node of a document) for a view `width` cells wide."""
    width = max(1, _int(width, 100) or 1)
    out = []
    for line in _sub(node, width):
        for part in (line if isinstance(line, str) else str(line)).split("\n"):
            out.append(_final(part, width))
    return out


def render_lines(document, width=100, unfold=None):
    """The whole document as a list of text lines.

    ``unfold`` opens collapsed nodes as if the reader had expanded them: True for all, or a list of `role`
    prefixes (e.g. ["omp.thinking"]). The document keeps the full content of a folded node, only the drawing
    hides it; the session log asks for it open so nothing the program said is lost.
    """
    _ctx.unfold = unfold
    try:
        return _draw(document.snapshot(), width)
    finally:
        _ctx.unfold = None
