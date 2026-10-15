"""TSP drawers for: kv, table, tree, badge, kbd, icon, image, list, item, tabs, picker.

DRAWERS maps a kind to a drawer function(node, ctx) -> HTML; tsp_html registers it over its own.

Sublime's minihtml has no tables, flexbox or absolute positioning, so columns are monospace cells
(``<tt>`` with non-breaking space padding, measured in terminal cells) and "right-aligned" parts are
pushed right with a run of padding.  Everything here is a pure function of the node and the context:
drawing the same node twice gives the same HTML.
"""

import base64
import html
import re
import struct
import unicodedata
import zlib

from . import tsp_html

# ---- tones and colours --------------------------------------------------------------------------

TONES = ("neutral", "accent", "info", "success", "warning", "error", "pending", "muted", "user")

_TONE_TOKENS = {"neutral": "muted", "accent": "accent", "success": "success", "warning": "warning",
                "error": "error", "muted": "dim"}
# Tern keeps its own colours for these tones (they are not palette tokens).
_TONE_FIXED = {"info": ("syntaxType", "#4fc1ff"), "pending": (None, "#8da4ef"), "user": (None, "#c792ea")}


def _props(node):
    return node.get("p") or {}


def _tone_of(props):
    tone = props.get("tone")
    return tone if tone in TONES else None


def tone_color(tone, ctx):
    """The colour of a tone name (palette token when the program's palette has one)."""
    if tone in _TONE_FIXED:
        token, fixed = _TONE_FIXED[tone]
        return ctx.theme.color(token, fixed) if token else fixed
    return ctx.theme.color(_TONE_TOKENS.get(tone, "muted"))


def _alpha(color, alpha):
    return tsp_html._hex_with_alpha(color, alpha)


def _color(ctx, name):
    return ctx.theme.color(name)


def _avail(ctx):
    """The cells a line may use here (a drawer that indents shrinks it for its children)."""
    return int(getattr(ctx, "avail", ctx.cols))


def _cell_px(ctx):
    return float(getattr(ctx, "cell_px", 8))


# ---- text measurement and cutting ---------------------------------------------------------------

def _cw(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def cells(text):
    """The width of ``text`` in terminal cells."""
    return sum(_cw(char) for char in text)


def _runs(value):
    """[(text, style tokens)] from a str or a list of {"t","s"} spans; hidden spans are dropped."""
    if value is None:
        return []
    if isinstance(value, str):
        return [(value, "")]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [(("%g" % value), "")]
    out = []
    if isinstance(value, (list, tuple)):
        for span in value:
            if isinstance(span, dict):
                tokens = span.get("s") or ""
                if "hide" in str(tokens).split():
                    continue
                text = span.get("t", "")
                out.append((text if isinstance(text, str) else str(text), str(tokens)))
            elif isinstance(span, str):
                out.append((span, ""))
    return out


def _plain(runs):
    return "".join(text for text, _tokens in runs)


def _slice_runs(runs, start, stop):
    out, position = [], 0
    for text, tokens in runs:
        low, high = max(start, position), min(stop, position + len(text))
        if low < high:
            out.append((text[low - position:high - position], tokens))
        position += len(text)
    return out


def _fit_prefix(text, width):
    used = 0
    for number, char in enumerate(text):
        used += _cw(char)
        if used > width:
            return number
    return len(text)


def _fit_suffix(text, width):
    used = 0
    for number, char in enumerate(reversed(text)):
        used += _cw(char)
        if used > width:
            return number
    return len(text)


def _cut_runs(runs, width, mode="end"):
    """``runs`` cut to ``width`` cells with an ellipsis: ``end`` keeps the start, ``start`` the end,
    ``middle`` both ends (a path drops its directories first)."""
    text = _plain(runs)
    if cells(text) <= width:
        return runs
    if width <= 0:
        return []
    if width == 1:
        return [("\u2026", runs[0][1] if runs else "")]
    if mode == "start":
        keep = _fit_suffix(text, width - 1)
        return [("\u2026", runs[0][1])] + _slice_runs(runs, len(text) - keep, len(text))
    if mode == "middle":
        position = text.find("/")
        while position >= 0:
            if position + 1 < len(text) and 2 + cells(text[position + 1:]) <= width:
                return [("\u2026/", "")] + _slice_runs(runs, position + 1, len(text))
            position = text.find("/", position + 1)
        room = width - 1
        left, right = _fit_prefix(text, (room + 1) // 2), _fit_suffix(text, room // 2)
        return _slice_runs(runs, 0, left) + [("\u2026", "")] + _slice_runs(runs, len(text) - right, len(text))
    keep = _fit_prefix(text, width - 1)
    return _slice_runs(runs, 0, keep) + [("\u2026", runs[-1][1] if runs else "")]


def _runs_html(runs, ctx, oneline=False, nbsp=False):
    out = []
    for text, tokens in runs:
        if oneline:
            text = text.replace("\r", "").replace("\n", " ").replace("\t", " ")
        if nbsp:
            text = text.replace(" ", "\u00a0")
        out.append(tsp_html._span_html(text, tokens, ctx))
    result = "".join(out)
    return result.replace("\u00a0", "&nbsp;") if nbsp else result


def _spans_html(value, ctx):
    return _runs_html(_runs(value), ctx)


def _visible_cells(html_text):
    """The width in cells of the text inside an HTML fragment."""
    return cells(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def _nbsp(count):
    return "&nbsp;" * max(0, count)


# ---- search marks -------------------------------------------------------------------------------

def _match_ranges(text, query):
    """Char ranges of ``text`` that ``query`` matches: the first whole occurrence (case-insensitive),
    else the fuzzy subsequence match with adjacent hits merged; nothing when there is no match."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    low = text.lower()
    found = low.find(needle)
    if found >= 0:
        return [(found, found + len(needle))]
    ranges, position = [], 0
    for char in needle:
        if char.isspace():
            continue
        spot = low.find(char, position)
        if spot < 0:
            return []
        if ranges and ranges[-1][1] == spot:
            ranges[-1] = (ranges[-1][0], spot + 1)
        else:
            ranges.append((spot, spot + 1))
        position = spot + 1
    return ranges


def _u16_ranges(text, ranges):
    """[from, to) UTF-16 offset pairs -> char index ranges of ``text``."""
    index_of, units = {}, 0
    for number, char in enumerate(text):
        index_of[units] = number
        units += 2 if ord(char) > 0xFFFF else 1
    index_of[units] = len(text)
    out = []
    for pair in ranges or []:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2 and all(isinstance(v, int) for v in pair[:2]):
            start, stop = index_of.get(pair[0]), index_of.get(pair[1])
            if start is not None and stop is not None and start < stop:
                out.append((start, stop))
    return sorted(out)


def _marked_html(runs, ranges, ctx, hit_style, oneline=True):
    """``runs`` as HTML with the char ``ranges`` wrapped in a span carrying ``hit_style``; the spans'
    own styles survive the cut."""
    if not ranges:
        return _runs_html(runs, ctx, oneline)
    out, position = [], 0
    for text, tokens in runs:
        end = position + len(text)
        pieces, at = [], position
        for start, stop in ranges:
            low, high = max(start, position), min(stop, end)
            if low < high:
                if low > at:
                    pieces.append((text[at - position:low - position], False))
                pieces.append((text[low - position:high - position], True))
                at = high
        if at < end:
            pieces.append((text[at - position:], False))
        for piece, hit in pieces:
            if oneline:
                piece = piece.replace("\n", " ")
            inner = tsp_html._span_html(piece, tokens, ctx)
            out.append('<span style="%s">%s</span>' % (hit_style, inner) if hit else inner)
        position = end
    return "".join(out)


# ---- icons --------------------------------------------------------------------------------------

# Every icon in Tern's set, drawn as the nearest plain Unicode glyph (BMP symbols that ordinary
# fonts carry).  Bold/italic/underline are styled letters (see _ICON_STYLE).
ICON_GLYPHS = {
    # Apps and brands
    "app-btop": "\u25a5", "app-bun": "\u25d7", "app-claude": "\u273b", "app-codex": "\u232c",
    "app-deno": "\u25d0", "app-docker": "\u25a3", "app-go": "\u21e2", "app-hammer": "\u2692",
    "app-helix": "\u2726", "app-hermes": "\u263f", "app-htop": "\u25a7", "app-java": "\u2668",
    "app-node": "\u2b22", "app-omp": "\u03c0", "app-python": "\u2119", "app-ruby": "\u2666",
    "app-rust": "\u2699", "app-vim": "\u24cb", "logo": "\u2b21", "tern": "\u25e5", "pi-mark": "\u03c0",
    # Arrows and navigation
    "arrow-up": "\u2191", "arrow-down": "\u2193", "arrow-left": "\u2190", "arrow-right": "\u2192",
    "back": "\u21a9", "forward": "\u21aa", "chev": "\u25be", "chev-r": "\u25b8", "chev-up": "\u25b4",
    "corner-down-right": "\u21b3", "expand": "\u2922", "shrink": "\u2921", "minimize": "\u2581",
    "fit": "\u22a1", "zoom-in": "\u2295", "zoom-out": "\u2296", "more": "\u22ef", "grip": "\u283f",
    "compass": "\u2316",
    # Status and alerts
    "check": "\u2713", "x": "\u2717", "x-circle": "\u24e7", "warn": "\u25b2", "info": "\u25cf",
    "help": "?", "bell": "\u25ce", "shield": "\u26e8", "shield-alert": "\u26a0", "slash": "\u2044",
    "slash-circle": "\u2298", "lock": "\u26bf", "key": "\u26b7", "key-round": "\u26b7", "plus": "+",
    "minus": "\u2212", "clock": "\u25f7", "timer": "\u25f4",
    # Files and documents
    "doc": "\u2261", "file": "\u25af", "file-code": "\u2039\u203a", "file-image": "\u25a8",
    "file-pdf": "PDF", "file-plus": "\u25af+", "files": "\u25af\u25af", "folder": "\u25a4",
    "folder-go": "\u25a4\u2192", "folder-minus": "\u25a4\u2212", "folder-open": "\u25a5",
    "folder-plus": "\u25a4+", "markdown": "M\u2193", "note": "\u25f1", "clipboard": "\u2398",
    "save": "\u2b12", "inbox": "\u2b13", "image": "\u26f0", "newspaper": "\u2637", "trash": "\u2327",
    # Version control
    "branch": "\u2442", "commit": "\u25c9", "merge": "\u22c8", "rebase": "\u2934", "pull": "\u2913",
    "push": "\u2912", "fetch": "\u21e3", "stash": "\u229f", "cherry": "\u229a", "diff": "\u00b1",
    "unified": "\u25ad", "split": "\u25eb", "split-down": "\u2b12", "revert": "\u21ba", "history": "\u21ba\u2009",
    # Playback and actions
    "play": "\u25b6", "pause": "\u2016", "stop": "\u25a0", "rewind": "\u00ab", "fast-forward": "\u00bb",
    "frame-next": "|\u25b6", "frame-prev": "\u25c0|", "repeat": "\u27f3", "redo": "\u21b7", "undo": "\u21b6",
    "run": "\u25b8", "reel": "\u29bf", "mic": "\u266c", "wave": "\u223f", "vibrate": "\u224b",
    "power": "\u23fb", "send": "\u27a4", "share": "\u2197", "open": "\u21f1", "download": "\u21e9",
    "copy": "\u29c9", "scissors": "\u2702",
    # Text and editing
    "bold": "B", "italic": "I", "underline": "U", "type": "T", "align-left": "\u21e4",
    "align-center": "\u2194", "align-right": "\u21e5", "align-justify": "\u21f9", "pen": "\u270e",
    "eraser": "\u232b", "wand": "\u2727", "cursor": "\u2196", "selection": "\u2b1a", "path-insert": "\u2937",
    "prompt": "\u276f", "vector": "\u25c7", "blur": "\u2591", "ink": "\u2712", "palette": "\u2756",
    # Layout and windows
    "columns": "\u25a5", "grid": "\u25a6", "sidebar": "\u25e7", "tabs-h": "\u25ad", "tabs-v": "\u25af",
    "dock-up": "\u2b12", "dock-down": "\u2b13", "dock-left": "\u25e7", "dock-right": "\u25e8", "pip": "\u25a3",
    "pip-tl": "\u25f0", "pip-tr": "\u25f3", "pip-bl": "\u25f1", "pip-exit": "\u21f2", "layers": "\u29c8",
    "stack": "\u2630", "kanban": "\u25ae\u25af", "canvas": "\u25a2", "diagram": "\u232c", "flow": "\u21e2",
    "peek": "\u2299", "tree": "\u22a2", "box": "\u25a3", "eye": "\u25c9", "eye-off": "\u25cc",
    # Development and system
    "terminal": ">_", "code": "</>", "braces": "{}", "binary": "01", "bug": "\u2689", "cpu": "\u229e",
    "database": "\u26c1", "server": "\u25a5", "laptop": "\u25ac", "monitor": "\u25ad", "gauge": "\u25d1",
    "activity": "\u2301", "chart": "\u2581\u2583\u2585", "plug": "\u23da", "puzzle": "\u2318", "wrench": "\u26cf",
    "gear": "\u2699", "sliders": "\u269f", "stethoscope": "\u2695", "hash": "#", "tag": "\u2311",
    "link": "\u26d3", "globe": "\u25cd", "broadcast": "\u25ce\u25ce", "search": "\u2315", "funnel": "\u25bd",
    "funnel-x": "\u25bd\u00d7", "sort-x": "\u21c5", "swap": "\u21c4", "list": "\u2630", "list-checks": "\u2611",
    "pin": "\u2691", "keyboard": "\u2328",
    # People and misc
    "user": "\u263a", "users": "\u263a\u263a", "message": "\u2709", "brain": "\u2742", "sparkle": "\u2726",
    "lightbulb": "\u25c6", "bolt": "\u03df", "flame": "\u2739", "rocket": "\u27b9", "moon": "\u263e",
    "sun": "\u2600", "cart": "\u26d2", "footprints": "\u2234", "scale": "\u2696", "log-in": "\u21a6",
    "log-out": "\u21a4",
}

_ICON_STYLE = {"bold": "b", "italic": "i", "underline": "u"}

# Names omp used before the full set was drawn; kept so its documents look the same.
_ICON_LEGACY = {"model": "\u25c8"}

# Aliases (omp's symbol names and common words): used when no icon has that name.
ICON_ALIASES = {
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


def icon_name(name):
    """The icon's own name for an icon name or alias (anything up to the last '.' dropped), or None."""
    if not isinstance(name, str) or not name:
        return None
    name = name.rsplit(".", 1)[-1]
    if name in ICON_GLYPHS or name in _ICON_LEGACY:
        return name
    alias = ICON_ALIASES.get(name)
    return alias if alias in ICON_GLYPHS else None


def icon_glyph(name):
    """The glyph text for an icon name or alias, or None when the name is unknown."""
    resolved = icon_name(name)
    if resolved is None:
        return None
    return _ICON_LEGACY.get(resolved) or ICON_GLYPHS[resolved]


def _icon_inner(name):
    """Escaped glyph HTML for an icon name (None when unknown)."""
    resolved = icon_name(name)
    if resolved is None:
        return None
    glyph = _ICON_LEGACY.get(resolved) or ICON_GLYPHS[resolved]
    inner = html.escape(glyph, quote=False)
    tag = _ICON_STYLE.get(resolved)
    return "<%s>%s</%s>" % (tag, inner, tag) if tag else inner


def _icon_span(name, color):
    """An icon as an inline span in ``color`` (None: the text colour); None when the name is unknown."""
    inner = _icon_inner(name)
    if inner is None:
        return None
    return '<span style="color: %s">%s</span>' % (color, inner) if color else "<span>%s</span>" % inner


def _draw_icon(node, ctx):
    props = _props(node)
    tone = _tone_of(props)
    color = tone_color(tone, ctx) if tone else None
    span = _icon_span(props.get("name"), color)
    return span if span is not None else "<span>&nbsp;&nbsp;</span>"      # an unknown name draws an empty box


# ---- badge and kbd ------------------------------------------------------------------------------

def badge_html(text, tone, ctx):
    """A tone-tinted pill: text in the tone colour on a 10% tint with a 22% ring; no tone is a gray chip."""
    tone = tone if tone in TONES else None
    color = tone_color(tone or "neutral", ctx)
    return ('<span style="background-color: %s; color: %s; border: 1px solid %s; border-radius: 9px; '
            'padding: 0 7px; font-size: 0.85rem">%s</span>'
            % (_alpha(color, 0.10), color, _alpha(color, 0.22), tsp_html.esc(text).replace(" ", "&nbsp;")))


def _draw_badge(node, ctx):
    props = _props(node)
    text = props.get("text")
    return badge_html("" if text is None else str(text), _tone_of(props), ctx)


_KEY_GLYPHS = {
    "ctrl": "\u2303", "control": "\u2303", "alt": "\u2325", "option": "\u2325", "opt": "\u2325",
    "shift": "\u21e7", "cmd": "\u2318", "command": "\u2318", "meta": "\u2318", "super": "\u2318", "win": "\u2318",
    "enter": "\u23ce", "return": "\u23ce", "esc": "esc", "escape": "esc", "tab": "\u21e5",
    "backspace": "\u232b", "delete": "\u2326", "del": "\u2326", "space": "\u2423",
    "up": "\u2191", "down": "\u2193", "left": "\u2190", "right": "\u2192",
    "pageup": "\u21de", "pagedown": "\u21df", "home": "\u2196", "end": "\u2198",
}
_FUNCTION_KEY = re.compile(r"^f\d+$", re.I)


def key_label(name):
    """The keycap text for a key name (macOS glyphs for modifiers and named keys)."""
    lower = name.lower()
    if lower in _KEY_GLYPHS:
        return _KEY_GLYPHS[lower]
    if len(name) == 1 or _FUNCTION_KEY.match(name):
        return name.upper()
    return name


def keycaps_html(keys, ctx):
    """One keycap per key, 3px apart."""
    caps = []
    text = _color(ctx, "text")
    for key in keys or []:
        if not isinstance(key, str) or not key:
            continue
        caps.append('<span style="background-color: %s; color: %s; border: 1px solid %s; border-radius: 4px; '
                    'padding: 0 4px; font-size: 0.8rem"><tt>%s</tt></span>'
                    % (_alpha(text, 0.08), _color(ctx, "muted"), _alpha(text, 0.18), tsp_html.esc(key_label(key))))
    return "&#8201;".join(caps)


def _draw_kbd(node, ctx):
    caps = keycaps_html(_props(node).get("keys"), ctx)
    return "<span>%s</span>" % caps if caps else ""


# ---- kv -----------------------------------------------------------------------------------------

def _kv_items(props):
    return [item for item in props.get("items") or [] if isinstance(item, dict)]


def _draw_kv(node, ctx):
    props = _props(node)
    items = _kv_items(props)
    if not items:
        return ""
    muted, dim = _color(ctx, "muted"), _color(ctx, "dim")
    if props.get("layout") == "inline":
        pairs = []
        for item in items:
            key = _runs_html(_runs(item.get("k")), ctx, oneline=True, nbsp=True)
            value = _runs_html(_runs(item.get("v")), ctx, oneline=True, nbsp=True)
            pairs.append('<span style="color: %s">%s</span>&nbsp;%s' % (muted, key, value))
        separator = ' <span style="color: %s">&middot;</span> ' % dim
        return "<div>%s</div>" % separator.join(pairs)
    width = max(cells(_plain(_runs(item.get("k")))) for item in items)
    indent = "<tt>%s</tt>" % _nbsp(width + 2)
    lines = []
    for item in items:
        key_runs = _runs(item.get("k"))
        key = _runs_html(key_runs, ctx, oneline=True, nbsp=True)
        pad = width - cells(_plain(key_runs)) + 2
        value = _spans_html(item.get("v"), ctx).replace("<br>", "<br>" + indent)
        lines.append('<tt><span style="color: %s">%s</span>%s</tt>%s' % (muted, key, _nbsp(pad), value))
    return "<div>%s</div>" % "<br>".join(lines)


# ---- meter bars (table cells and picker bar facts) ----------------------------------------------

def _number(value, default=None):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def meter_cells_html(meter, width, ctx):
    """A thin bar exactly ``width`` terminal cells wide, in half-cell steps: filled cells are full
    blocks, a half step is a half block over the next colour.  ``meter`` reads like the meter kind."""
    meter = meter if isinstance(meter, dict) else {}
    units = max(1, width) * 2
    track = _color(ctx, "border")
    parts = [part for part in meter.get("parts") or [] if isinstance(part, dict)]
    value = _number(meter.get("value"))
    thresholds = meter.get("thresholds") if isinstance(meter.get("thresholds"), dict) else {}
    tone = meter.get("tone") if meter.get("tone") in TONES else None
    fill = tone_color(tone, ctx) if tone else _color(ctx, "accent")
    segments = []
    if parts:
        total = 0.0
        for part in parts:
            amount = max(0.0, _number(part.get("value"), 0.0))
            amount = min(amount, 1.0 - total)
            total += amount
            segments.append((amount, ctx.theme.color(part.get("token") or "accent"), bool(part.get("hatch"))))
        if value is None:
            value = total
    elif value is not None:
        segments.append((max(0.0, min(1.0, value)), fill, False))
    if value is not None:
        level = max(0.0, min(1.0, value))
        if _number(thresholds.get("bad")) is not None and level >= thresholds["bad"]:
            segments = [(a, _color(ctx, "error") if not parts else c, h) for a, c, h in segments]
        elif _number(thresholds.get("warn")) is not None and level >= thresholds["warn"]:
            segments = [(a, _color(ctx, "warning") if not parts else c, h) for a, c, h in segments]
    slots = [None] * units
    cumulative = 0.0
    for amount, color, hatch in segments:
        start = int(round(cumulative * units))
        cumulative += amount
        for slot in range(start, min(units, int(round(cumulative * units)))):
            slots[slot] = (color, hatch)
    unknown = value is None and not parts
    runs = []        # (char, colour, background) merged
    for cell in range(width):
        left, right = slots[2 * cell], slots[2 * cell + 1]
        if left == right:
            if left is None:
                char, color, back = ("\u2591" if unknown else "\u2588"), track, None
            else:
                char, color, back = ("\u2592" if left[1] else "\u2588"), left[0], None
        else:
            color = left[0] if left else track
            back = right[0] if right else track
            char = "\u258c"
        if runs and runs[-1][1] == color and runs[-1][2] == back:
            runs[-1][0] += char
        else:
            runs.append([char, color, back])
    return "".join('<span style="color: %s%s">%s</span>' % (c, "; background-color: %s" % b if b else "", ch)
                   for ch, c, b in runs)


# ---- table --------------------------------------------------------------------------------------

_METER_NATURAL = 8


def _table_cols(props):
    cols, seen = [], set()
    for entry in props.get("cols") or []:
        if not isinstance(entry, dict):
            continue
        col_id = entry.get("id")
        if not isinstance(col_id, str) or not col_id or col_id in seen:
            continue
        seen.add(col_id)
        grow = _number(entry.get("grow"), 0)
        cols.append({
            "id": col_id,
            "head": _runs(entry.get("head")),
            "align": entry.get("align") if entry.get("align") in ("center", "end") else "start",
            "truncate": entry.get("truncate") if entry.get("truncate") in ("start", "middle") else "end",
            "priority": _number(entry.get("priority"), 0),
            "grow": grow if grow > 0 else 0,
        })
    return cols


def _table_rows(props):
    rows, seen = [], set()
    for entry in props.get("rows") or []:
        if not isinstance(entry, dict):
            continue
        row_id = entry.get("id")
        if not isinstance(row_id, str) or not row_id or row_id in seen:
            continue
        seen.add(row_id)
        rows.append((row_id, entry.get("cells") if isinstance(entry.get("cells"), dict) else {}))
    return rows


def _is_meter(cell):
    return isinstance(cell, dict) and isinstance(cell.get("meter"), dict)


def _cell_width(cell):
    return _METER_NATURAL if _is_meter(cell) else cells(_plain(_runs(cell)).replace("\n", " "))


def _table_widths(cols, rows, available):
    """The shown columns (in order) and their widths in cells, as the spec sizes them."""
    natural = {}
    for col in cols:
        widest = max([cells(_plain(col["head"]))] + [_cell_width(cells_.get(col["id"])) for _id, cells_ in rows])
        natural[col["id"]] = max(1, widest)

    def sized(col):
        width = natural[col["id"]]
        return min(width, 24) if col["grow"] else width

    shown = list(cols)
    while len(shown) > 1 and sum(sized(col) + 2 for col in shown) > available:
        # lowest priority hides first; ties hide the rightmost first
        victim = min(range(len(shown)), key=lambda n: (shown[n]["priority"], -n))
        del shown[victim]
    widths = {col["id"]: natural[col["id"]] for col in shown}
    total = sum(widths[col["id"]] + 2 for col in shown)
    if total < available:
        growers = [col for col in shown if col["grow"]]
        if growers:
            spare = available - total
            shares = float(sum(col["grow"] for col in growers))
            given = 0
            for col in growers:
                extra = int(spare * col["grow"] / shares)
                widths[col["id"]] += extra
                given += extra
            widths[growers[-1]["id"]] += spare - given
    elif total > available:
        excess = total - available
        growers = [col for col in shown if col["grow"] and widths[col["id"]] > 24]
        room = sum(widths[col["id"]] - 24 for col in growers)
        if room:
            take = min(excess, room)
            cut = 0
            for col in growers:
                part = int(take * (widths[col["id"]] - 24) / float(room))
                widths[col["id"]] -= part
                cut += part
            for col in growers:
                if cut < take and widths[col["id"]] > 24:
                    widths[col["id"]] -= 1
                    cut += 1
            excess -= cut
        if excess > 0:
            total = float(sum(widths[col["id"]] for col in shown))
            scale = max(0.0, (total - excess) / total)
            for col in shown:
                widths[col["id"]] = max(3 if len(shown) > 1 else 1, int(widths[col["id"]] * scale))
    return shown, widths


def _aligned(html_text, used, width, align, pad_right=True):
    spare = max(0, width - used)
    if align == "end":
        return _nbsp(spare) + html_text
    if align == "center":
        return _nbsp(spare // 2) + html_text + (_nbsp(spare - spare // 2) if pad_right else "")
    return html_text + (_nbsp(spare) if pad_right else "")


def _draw_table(node, ctx):
    props = _props(node)
    cols, rows = _table_cols(props), _table_rows(props)
    if not cols or (not rows and not any(col["head"] for col in cols)):
        return ""
    shown, widths = _table_widths(cols, rows, max(10, _avail(ctx)))
    gap = _nbsp(2)

    def line(cell_html):
        return "<tt>%s</tt>" % gap.join(cell_html)

    lines = []
    if any(col["head"] for col in cols):
        heads = []
        for number, col in enumerate(shown):
            runs = _cut_runs(col["head"], widths[col["id"]], col["truncate"])
            inner = '<span style="color: %s"><b>%s</b></span>' % (_color(ctx, "muted"), _runs_html(runs, ctx, True, True))
            heads.append(_aligned(inner, cells(_plain(runs)), widths[col["id"]], col["align"], number < len(shown) - 1))
        lines.append('<div style="border-bottom: 1px solid %s; padding: 1px 0">%s</div>' % (_color(ctx, "border"), line(heads)))
    for number, (_row_id, row_cells) in enumerate(rows):
        out = []
        for position, col in enumerate(shown):
            width, cell = widths[col["id"]], row_cells.get(col["id"])
            last = position == len(shown) - 1
            if _is_meter(cell):
                bar_width = width if col["grow"] else min(width, _METER_NATURAL)
                out.append(_aligned(meter_cells_html(cell["meter"], bar_width, ctx), bar_width, width, col["align"], not last))
            else:
                runs = _cut_runs(_runs(cell), width, col["truncate"]) if cell is not None else []
                text = _runs_html(runs, ctx, True, True)
                out.append(_aligned(text, cells(_plain(runs).replace("\n", " ")), width, col["align"], not last))
        border = "" if number == len(rows) - 1 else "border-bottom: 1px solid %s; " % _color(ctx, "borderMuted")
        lines.append('<div style="%spadding: 1px 0">%s</div>' % (border, line(out)))
    return '<div style="margin: 4px 0">%s</div>' % "".join(lines)


# ---- tree ---------------------------------------------------------------------------------------

def _tree_items(nodes, seen):
    out = []
    for entry in nodes or []:
        if not isinstance(entry, dict):
            continue
        item_id = entry.get("id")
        if not isinstance(item_id, str) or not item_id or item_id in seen:
            continue
        seen.add(item_id)
        out.append((entry, item_id, _tree_items(entry.get("children"), seen)))
    return out


def tree_state_key(node_id, item_id):
    return "tree:%s/%s" % (node_id, item_id)


def _tree_is_open(entry, key, ctx):
    wire = entry.get("open") is True
    saved = ctx.local.get(key)
    if isinstance(saved, dict) and saved.get("w") == wire:
        return bool(saved.get("o"))        # the reader's own choice stands until the program sends a different `open`
    return wire


def _draw_tree(node, ctx):
    props = _props(node)
    items = _tree_items(props.get("nodes"), set())
    if not items:
        return ""
    node_id = node.get("id")
    dim, guide = _color(ctx, "dim"), _color(ctx, "borderMuted")
    rows = []

    def walk(entries, depth):
        for entry, item_id, kids in entries:
            key = tree_state_key(node_id, item_id)
            wire = entry.get("open") is True
            is_open = _tree_is_open(entry, key, ctx) if kids else False
            indent = ('<span style="color: %s">\u2502</span>&nbsp;' % guide) * depth
            if kids:
                glyph = "\u25be" if is_open else "\u25b8"
                chevron = '<span style="color: %s">%s</span>' % (dim, glyph)
                if ctx.surface_id is not None:
                    event = {"ev": "toggle", "sf": ctx.surface_id, "id": node_id, "key": item_id, "collapsed": is_open,
                             "also": {"local": "set", "key": key, "value": {"o": not is_open, "w": wire}}}
                    chevron = '<a href="%s" style="text-decoration: none">%s</a>' % (tsp_html.event_href(event), chevron)
            else:
                chevron = "&nbsp;"
            icon = _icon_span(entry.get("icon"), _color(ctx, "muted")) if entry.get("icon") else None
            icon_html = (icon if icon is not None else "&nbsp;") + "&nbsp;" if entry.get("icon") else ""
            budget = max(6, _avail(ctx) - 2 * depth - 6)
            label = _runs_html(_cut_runs(_runs(entry.get("label")), budget), ctx, True)
            rows.append("<div><tt>%s%s&nbsp;</tt>%s%s</div>" % (indent, chevron, icon_html, label))
            if kids and is_open:
                walk(kids, depth + 1)

    walk(items, 0)
    return "<div>%s</div>" % "".join(rows)


# ---- image --------------------------------------------------------------------------------------

def _png_from_pixels(width, height, pixel):
    """A PNG data URL built from pixel(x, y) -> (r, g, b, a)."""
    rows = b""
    for y in range(height):
        rows += b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(width))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# omp's mark: a pi sign in a pink-to-blue gradient, drawn on a 12x5 grid of 5-pixel cells.
_OMP_LOGO_GRID = ("############",
                  "   ##  ##   ",
                  "   ##  ##   ",
                  "   ##  ##   ",
                  "       ##   ")
_logo_cache = []


def _omp_logo_url():
    if not _logo_cache:
        cell, columns = 5, 12

        def pixel(x, y):
            column, row = x // cell, y // cell
            if _OMP_LOGO_GRID[row][column] != "#":
                return (0, 0, 0, 0)
            mix = column / float(columns - 1)
            return (int(235 + (125 - 235) * mix), int(81 + (116 - 81) * mix), int(209 + (242 - 209) * mix), 255)

        _logo_cache.append(_png_from_pixels(cell * columns, cell * len(_OMP_LOGO_GRID), pixel))
    return _logo_cache[0]


def _decode_prefix(base64_text, chars=8192):
    head = base64_text[:chars]
    try:
        return base64.b64decode(head + "=" * (-len(head) % 4))
    except (ValueError, TypeError):
        return b""


def _webp_size(data):
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    kind = data[12:16]
    if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if kind == b"VP8L" and data[20:21] == b"\x2f":
        bits = struct.unpack("<I", data[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X":
        return (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
    return None


_SVG_ATTR = re.compile(r'\b(viewBox|width|height)\s*=\s*["\']([^"\']*)["\']', re.I)


def _svg_size(data):
    text = data.decode("utf-8", "ignore")
    root = re.search(r"<svg\b[^>]*>", text, re.I)
    if not root:
        return None
    attrs = {name.lower(): value for name, value in _SVG_ATTR.findall(root.group(0))}

    def length(value):
        match = re.match(r"^\s*([\d.]+)\s*(px)?\s*$", value or "")
        return float(match.group(1)) if match else None

    width, height = length(attrs.get("width")), length(attrs.get("height"))
    if width and height:
        return int(round(width)), int(round(height))
    parts = (attrs.get("viewbox") or "").replace(",", " ").split()
    if len(parts) == 4:
        try:
            view_w, view_h = float(parts[2]), float(parts[3])
        except ValueError:
            return None
        if view_w > 0 and view_h > 0:
            return int(round(view_w)), int(round(view_h))
    return None


SVG_LIMIT = 256 * 1024


def image_info(base64_text):
    """(mime, width, height) of a blob's base64 text (PNG, JPEG, GIF, WebP, SVG), or None."""
    data = _decode_prefix(base64_text)
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", width, height
    if data[:4] == b"GIF8" and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return "image/gif", width, height
    if data[:2] == b"\xff\xd8":
        position = 2
        while position + 9 < len(data):
            if data[position] != 0xFF:
                position += 1
                continue
            marker = data[position + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                height, width = struct.unpack(">HH", data[position + 5:position + 9])
                return "image/jpeg", width, height
            position += 2 + struct.unpack(">H", data[position + 2:position + 4])[0]
    size = _webp_size(data)
    if size:
        return ("image/webp",) + size
    if b"<svg" in data[:2048].lower() and len(base64_text) * 3 // 4 <= SVG_LIMIT:
        size = _svg_size(data)
        if size:
            return ("image/svg+xml",) + size
    return None


_IMAGE_MAX_H = 320.0
_LINE_PX = 20.0


def _bound_px(value, parent, cell_px):
    """A `max` bound ("10lines", "40ch" or a fraction of the parent) in px, or None."""
    if isinstance(value, str):
        match = re.match(r"^\s*([\d.]+)\s*(lines|ch)\s*$", value)
        if match:
            return float(match.group(1)) * (_LINE_PX if match.group(2) == "lines" else cell_px)
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return value * parent
    return None


def _image_card(node, ctx, label, reason):
    props = _props(node)
    text = tsp_html.esc(tsp_html.plain_text(props.get("alt")) or label)
    glyph = icon_glyph("image")
    return ('<div style="border: 1px dashed %s; border-radius: 6px; padding: 2px 10px; margin: 2px 0; color: %s">'
            '%s&nbsp;%s</div>' % (_color(ctx, "border"), _color(ctx, "muted"), tsp_html.esc(glyph), text))


def _draw_image(node, ctx):
    props = _props(node)
    over = tsp_html._children(node, ctx)
    wide, high = _number(props.get("w"), 0), _number(props.get("h"), 0)
    if props.get("builtin") == "omp":
        width, height = 60, 25
        if wide > 0 and high > 0:
            width, height = int(wide), int(high)
        elif wide > 0:
            width, height = int(wide), max(1, int(round(wide * 25 / 60.0)))
        elif high > 0:
            width, height = max(1, int(round(high * 60 / 25.0))), int(high)
        picture = '<img src="%s" width="%d" height="%d">' % (_omp_logo_url(), width, height)
    elif props.get("builtin"):
        return "<div>%s%s</div>" % (_image_card(node, ctx, "Image unavailable", ""), over)
    else:
        blob_id = props.get("blob")
        blob = ctx.blobs.get(blob_id) if isinstance(blob_id, str) and blob_id else None
        if not blob:
            label = "Image loading" if blob_id else "Image unavailable"
            return "<div>%s%s</div>" % (_image_card(node, ctx, label, ""), over)
        info = image_info(blob)
        if not info or info[1] <= 0 or info[2] <= 0:
            return "<div>%s%s</div>" % (_image_card(node, ctx, "Image unavailable", ""), over)
        mime, natural_w, natural_h = info
        if wide > 0 and high > 0:
            box_w, box_h = float(wide), float(high)
        elif wide > 0:
            box_w, box_h = float(wide), wide * natural_h / float(natural_w)
        elif high > 0:
            box_w, box_h = high * natural_w / float(natural_h), float(high)
        else:
            box_w, box_h = float(natural_w), float(natural_h)
        scale = min(box_w / natural_w, box_h / natural_h)          # object-fit: contain, the default
        shown_w, shown_h = natural_w * scale, natural_h * scale
        limits = props.get("max") if isinstance(props.get("max"), dict) else {}
        parsed_w = _bound_px(limits.get("w"), _avail(ctx) * _cell_px(ctx), _cell_px(ctx))
        parsed_h = _bound_px(limits.get("h"), _IMAGE_MAX_H, _cell_px(ctx)) if isinstance(limits.get("h"), str) else None
        width_px = _avail(ctx) * _cell_px(ctx)
        bound_w = min(width_px, parsed_w) if parsed_w else width_px
        bound_h = parsed_h or _IMAGE_MAX_H
        shrink = min(1.0, bound_w / shown_w, bound_h / shown_h)
        picture = '<img src="data:%s;base64,%s" width="%d" height="%d">' % (
            mime, blob, max(1, int(shown_w * shrink)), max(1, int(shown_h * shrink)))
    if over:
        return "<div><div>%s</div>%s</div>" % (picture, over)
    return "<span>%s</span>" % picture


# ---- scrolling windows (list and picker) --------------------------------------------------------

def _window(ctx, node_id, total, shown, selected, selected_index, ids):
    """The index of the first row to draw.  The reader's scroll offset (ctx.local["scroll:<id>"]) wins;
    when ``selected`` changed since the last draw the selection is brought into view by the least
    distance.  Publishes the geometry TspView needs under ``scrollinfo:<id>``."""
    top = max(0, total - shown)
    scroll_key, seen_key = "scroll:%s" % node_id, "selseen:%s" % node_id
    first = ctx.local.get(scroll_key)
    first = first if isinstance(first, int) and not isinstance(first, bool) else None
    if selected_index is not None and ctx.local.get(seen_key) != selected:
        if first is None:
            first = selected_index - shown // 2
        elif selected_index < first:
            first = selected_index
        elif selected_index >= first + shown:
            first = selected_index - shown + 1
        first = max(0, min(first, top))
        ctx.local[scroll_key] = first
        ctx.local[seen_key] = selected
    elif first is None:
        first = max(0, min(selected_index - shown // 2, top)) if selected_index is not None else 0
    first = max(0, min(first, top))
    if ctx.local.get(scroll_key) is not None and ctx.local.get(scroll_key) != first:
        ctx.local[scroll_key] = first
    ctx.local["scrollinfo:%s" % node_id] = {"total": total, "shown": min(shown, total), "first": first,
                                            "follow": False, "ids": ids}
    return first


def _more_line(ctx, node_id, text, first, label):
    """A dim "N more above/below" line whose link scrolls the window to ``first``."""
    link = _local_link(ctx, "scroll:%s" % node_id, first, label)
    return '<div style="color: %s">%s &nbsp;%s</div>' % (_color(ctx, "dim"), text, link)


# ---- item and list ------------------------------------------------------------------------------

_LIST_VIRTUAL_AT = 200
_LIST_WINDOW = 30
_WINDOW_HEIGHT_ROWS = 40          # a fractional `max` is a share of the window height; one window is about 40 rows


def _item_click_event(child, ctx, owner_id):
    """The events an item sends: (select event, activate event); its own click/dblclick actions replace them."""
    props = _props(child)
    actions = props.get("actions") if isinstance(props.get("actions"), dict) else {}
    select = tsp_html._click_event(child, ctx) if actions.get("click") else None
    item_id = child.get("id")
    if select is None and not actions.get("click"):
        select = {"ev": "select", "sf": ctx.surface_id, "id": owner_id, "item": item_id}
    activate = {"ev": "activate", "sf": ctx.surface_id, "id": owner_id, "item": item_id}
    if actions.get("dblclick"):
        fake = {"id": item_id, "k": "item", "p": {"actions": {"click": actions["dblclick"]}}}
        activate = tsp_html._click_event(fake, ctx)
    return select, activate


def _item_row(child, ctx, owner_id, selected, query, wire):
    """One item as a single block (so a click action on the node can wrap it): icon, label, detail,
    a value pushed to the right, keycap hints.  ``wire`` adds this row's own click link."""
    props = _props(child)
    disabled = bool(props.get("disabled"))
    tone = _tone_of(props)
    total = max(20, _avail(ctx) - 4)
    hints = [key for key in props.get("hint") or [] if isinstance(key, str) and key]
    hint_html = keycaps_html(hints, ctx)
    hint_w = (sum(cells(key_label(key)) + 2 for key in hints) + max(0, len(hints) - 1)) if hints else 0
    glyph_color = tone_color(tone, ctx) if tone else (_color(ctx, "accent") if selected else _color(ctx, "muted"))
    icon = _icon_span(props.get("icon"), glyph_color) if props.get("icon") else None
    icon_w = 2 if icon else 0
    value_runs = _runs(props.get("value"))
    value_w = min(cells(_plain(value_runs)), int(total * 0.4))
    value_runs = _cut_runs(value_runs, value_w)
    room = max(4, total - icon_w - hint_w - (value_w + 1 if value_w else 0))
    label_runs = _cut_runs(_runs(props.get("label")), room)
    label_plain = _plain(label_runs)
    ranges = _match_ranges(label_plain, query) if query else []
    label_html = _marked_html(label_runs, ranges, ctx, "background-color: %s" % _alpha(_color(ctx, "accent"), 0.30))
    left_w = icon_w + cells(label_plain)
    detail_html = ""
    detail_room = room - cells(label_plain) - 1
    if detail_room >= 3:
        detail_runs = _cut_runs(_runs(props.get("detail")), detail_room)
        if detail_runs:
            detail_html = '&nbsp;<span style="color: %s">%s</span>' % (_color(ctx, "muted"), _runs_html(detail_runs, ctx, True))
            left_w += 1 + cells(_plain(detail_runs))
    pad = max(1, total - left_w - (value_w if value_w else 0) - hint_w)
    value_html = '<span style="color: %s">%s</span>' % (_color(ctx, "muted"), _runs_html(value_runs, ctx, True, True)) if value_w else ""
    inner = (icon + "&nbsp;" if icon else "") + label_html + detail_html + "<tt>%s</tt>" % _nbsp(pad) + value_html
    if hint_html:
        inner += "&nbsp;" + hint_html
    if disabled:
        inner = '<span style="color: %s">%s</span>' % (_color(ctx, "dim"), inner)
    elif wire is not None:
        inner = '<a href="%s" style="text-decoration: none; color: %s">%s</a>' % (
            tsp_html.event_href(wire), _color(ctx, "text"), inner)
    if selected:
        style = "background-color: %s; border-left: 2px solid %s; padding: 1px 8px" % (
            _alpha(_color(ctx, "accent"), 0.16), _color(ctx, "accent"))
    else:
        style = "padding: 1px 8px 1px 10px"
    return '<div style="%s">%s</div>' % (style, inner)


def _draw_item(node, ctx):
    """An item on its own: the same row, outside a list (its id is both ``id`` and ``item``)."""
    props = _props(node)
    wire = None
    if ctx.surface_id is not None and not props.get("disabled"):
        actions = props.get("actions") if isinstance(props.get("actions"), dict) else {}
        if not actions.get("click"):        # an own click action wraps the row in draw()
            select, activate = _item_click_event(node, ctx, node.get("id"))
            wire = dict(select, dbl=activate) if activate is not None else select
    return _item_row(node, ctx, node.get("id"), False, "", wire)


def _list_rows_limit(props, count):
    """How many children to draw at once, or None for all of them (the list scrolls with the region)."""
    limit = props.get("max")
    rows = None
    if isinstance(limit, dict):
        lines = _number(limit.get("lines"))
        if lines is not None and lines > 0:
            rows = max(1, int(lines))
    elif _number(limit) is not None and limit > 0:
        rows = max(1, int(limit * _WINDOW_HEIGHT_ROWS))
    virtual = props.get("virtual") is True or count > _LIST_VIRTUAL_AT
    if rows is None and virtual:
        rows = _LIST_WINDOW
    return rows, virtual


def _draw_list(node, ctx):
    props = _props(node)
    children = [child for child in node.get("c") or [] if not _props(child).get("hidden")]
    node_id = node.get("id")
    if not children:
        empty = props.get("empty")
        return '<div style="color: %s; padding: 1px 10px">%s</div>' % (_color(ctx, "dim"), _spans_html(empty, ctx)) if empty else ""
    selected = props.get("selected")
    query = props.get("filter") if isinstance(props.get("filter"), str) else ""
    rows, virtual = _list_rows_limit(props, len(children))
    first, last = 0, len(children)
    ids = [child.get("id") for child in children]
    if rows is not None:
        selected_index = ids.index(selected) if selected in ids else None
        first = _window(ctx, node_id, len(children), rows, selected, selected_index, ids)
        last = min(len(children), first + rows)
    out = []
    if first:
        out.append(_more_line(ctx, node_id, "&#8593; %d more above" % first, max(0, first - (rows or first)), "page up"))
    for child in children[first:last]:
        if child.get("k") != "item":
            out.append(tsp_html.draw(child, ctx))
            continue
        on = child.get("id") == selected
        wire = None
        if ctx.surface_id is not None and not _props(child).get("disabled"):
            select, activate = _item_click_event(child, ctx, node_id)
            if virtual:
                select, activate = {"ev": "select", "sf": ctx.surface_id, "id": node_id, "item": child.get("id")}, \
                    {"ev": "activate", "sf": ctx.surface_id, "id": node_id, "item": child.get("id")}
            wire = (dict(select, dbl=activate) if activate is not None else select) if select is not None else None
        out.append(_item_row(child, ctx, node_id, on, query, wire))
    if last < len(children):
        out.append(_more_line(ctx, node_id, "&#8595; %d more below" % (len(children) - last), min(len(children) - rows, last),
                              "page down"))
    return "<div>%s</div>" % "".join(out)


# ---- tabs ---------------------------------------------------------------------------------------

def _draw_tabs(node, ctx):
    props = _props(node)
    active = props.get("active")
    tabs = []
    for entry in props.get("items") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry.get("id"):
            continue
        on = entry["id"] == active
        label = _runs_html(_runs(entry.get("label")), ctx, True, True)
        if on:
            body = ('<span style="background-color: %s; color: %s; border-radius: 6px; padding: 2px 10px; '
                    'text-decoration: underline">%s</span>' % (_alpha(_color(ctx, "accent"), 0.16), _color(ctx, "accent"), label))
        else:
            body = '<span style="color: %s; padding: 2px 10px">%s</span>' % (_color(ctx, "muted"), label)
        tabs.append(tsp_html._item_link(ctx, node, entry["id"], "select", body))
    if not tabs:
        return ""
    return '<div style="border-bottom: 1px solid %s; margin: 2px 0">%s</div>' % (_color(ctx, "border"), "".join(tabs))


# ---- picker -------------------------------------------------------------------------------------

PICKER_ROWS = 10
PICKER_MORE = 20

_FACT_WIDTH = {"text": 12, "num": 6, "price": 9, "bar": 6, "time": 6, "elapsed": 7, "dim": 8}
_FACT_RIGHT = ("num", "price", "time", "elapsed")
_TEXT_FACT_MAX = 40
_LABEL_MIN = 32                      # fact columns hide until the label keeps this many cells


def _soft(ctx):
    return _alpha(_color(ctx, "accent"), 0.20)


def _local_link(ctx, key, value, inner_html):
    """Inline content that changes only what this view shows (nothing is sent to the program)."""
    event = {"local": "set", "key": key, "value": value}
    return '<a href="%s" style="text-decoration: none; color: %s">%s</a>' % (
        tsp_html.event_href(event), _color(ctx, "accent"), inner_html)


def _mark_chip(mark, ctx):
    """A small coloured tile with initials (provider or avatar mark); its colour comes from the seed."""
    text = (mark.get("text") or "")[:2]
    seed = mark.get("seed") or text
    hue = sum(ord(char) * (n + 1) for n, char in enumerate(str(seed))) % 360
    return ('<span style="background-color: hsl(%d, 45%%, 32%%); color: #ffffff; border-radius: 4px; '
            'padding: 0 5px; font-size: 0.8rem">%s</span>' % (hue, tsp_html.esc(text)))


def _role_chip(chip_data, ctx):
    """A role chip after an item's name (default / smol / ...): lit when it is the current assignment."""
    dot = chip_data.get("dot")
    color = _color(ctx, "accent" if chip_data.get("on") else "dim")
    lead = '<span style="color: %s">&#9679;</span> ' % _color(ctx, dot) if dot else ""
    style = "font-style: italic; " if chip_data.get("auto") else ""
    return ('<span style="%sbackground-color: %s; color: %s; border-radius: 9px; padding: 1px 8px; font-size: 0.85rem">%s%s</span>'
            % (style, _alpha(color, 0.16), color, lead, tsp_html.esc(chip_data.get("text", ""))))


def _compact(value):
    """272000 -> 272k, 1600000 -> 1.6M, 15000 -> 15k, 3 -> 3."""
    magnitude = abs(value)
    if magnitude >= 1000000:
        return ("%.1f" % (value / 1000000.0)).rstrip("0").rstrip(".") + "M"
    if magnitude >= 10000:
        return "%dk" % round(value / 1000.0)
    if magnitude >= 1000:
        return ("%.1f" % (value / 1000.0)).rstrip("0").rstrip(".") + "k"
    return "%g" % value


def _picker_entries(props):
    items = [item for item in props.get("items") or [] if isinstance(item, dict) and item.get("id") is not None]
    by_id = {}
    for item in items:
        by_id.setdefault(item.get("id"), item)
    order = props.get("order")
    entries = []
    for entry in (order if order is not None else [item.get("id") for item in items]):
        if isinstance(entry, dict) and "group" in entry:
            entries.append(("group", entry))
        elif not isinstance(entry, dict) and entry in by_id:
            entries.append(("item", by_id[entry]))
    return entries


def _picker_columns(props, available):
    """The shown fact columns and their widths: lowest priority hides first until the label keeps room."""
    columns = []
    for entry in props.get("columns") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            fmt = entry.get("format") if entry.get("format") in _FACT_WIDTH else "text"
            head = entry.get("head") if isinstance(entry.get("head"), str) else ""
            width = max(_FACT_WIDTH[fmt], int(_number(entry.get("min"), 0)), cells(head))
            columns.append({"id": entry["id"], "format": fmt, "width": width, "head": head,
                            "priority": _number(entry.get("priority"), 100)})
    columns = columns[:8]
    room = max(0, available - _LABEL_MIN)
    shown = list(columns)
    while shown and sum(col["width"] + 1 for col in shown) > room:
        victim = min(range(len(shown)), key=lambda n: (shown[n]["priority"], -n))
        del shown[victim]
    return shown


def _fact_runs(value, column):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [(_compact(value), "")]
    return _runs(value)


def _grow_text_columns(shown, entries, room):
    """A text column grows to its widest value (up to 40 cells) out of the room the label leaves."""
    spare = room - sum(col["width"] + 1 for col in shown)
    for col in shown:
        if col["format"] != "text" or spare <= 0:
            continue
        widest = 0
        for kind, item in entries:
            if kind == "item" and isinstance(item.get("facts"), dict) and col["id"] in item["facts"]:
                widest = max(widest, cells(_plain(_fact_runs(item["facts"][col["id"]], col)).replace("\n", " ")))
        want = min(_TEXT_FACT_MAX, widest) - col["width"]
        if want > 0:
            give = min(want, spare)
            col["width"] += give
            spare -= give


def _facts_html(item, shown, ctx):
    facts = item.get("facts") if isinstance(item.get("facts"), dict) else {}
    has_free = any(str(b.get("text", "")).lower() == "free" for b in item.get("badges") or [] if isinstance(b, dict))
    cells_html = []
    for col in shown:
        value = facts.get(col["id"])
        width = col["width"]
        if col["id"] == "price" and has_free and str(tsp_html.plain_text(value) if not isinstance(value, (int, float)) else value).lower() == "free":
            value = None
        if value is None:
            cells_html.append(_nbsp(width))
        elif col["format"] == "bar" and isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
            cells_html.append(meter_cells_html({"value": value}, width, ctx))
        else:
            runs = _cut_runs(_fact_runs(value, col), width)
            align = "end" if col["format"] in _FACT_RIGHT else "start"
            body = '<span style="color: %s">%s</span>' % (_color(ctx, "dim"), _runs_html(runs, ctx, True, True))
            cells_html.append(_aligned(body, cells(_plain(runs).replace("\n", " ")), width, align))
    return "&nbsp;".join(cells_html)


def _facts_width(shown):
    return sum(col["width"] + 1 for col in shown)


def _heads_html(shown, ctx):
    out = []
    for col in shown:
        runs = _cut_runs([(col["head"], "")], col["width"])
        align = "end" if col["format"] in _FACT_RIGHT else "start"
        out.append(_aligned('<span style="color: %s">%s</span>' % (_color(ctx, "dim"), _runs_html(runs, ctx, True, True)),
                            cells(_plain(runs)), col["width"], align))
    return "&nbsp;".join(out)


def _picker_label_html(item, ctx, hits, budget, bold_style):
    """The row's label: hits bold, a mono path as a dim directory and a strong name."""
    runs = _cut_runs(_runs(item.get("label")), budget)
    text = _plain(runs)
    ranges = _u16_ranges(_plain(_runs(item.get("label"))), hits) if hits else []
    ranges = [(a, b) for a, b in ranges if b <= len(text)]
    if item.get("mono") and "/" in text and text == _plain(_runs(item.get("label"))):
        cut = text.rindex("/") + 1
        directory = _marked_html(_slice_runs(runs, 0, cut), [(a, min(b, cut)) for a, b in ranges if a < cut], ctx, bold_style)
        name = _marked_html(_slice_runs(runs, cut, len(text)), [(max(a, cut) - cut, b - cut) for a, b in ranges if b > cut],
                            ctx, bold_style)
        return '<span style="color: %s">%s</span><b>%s</b>' % (_color(ctx, "dim"), directory, name), cells(text)
    if ranges:
        return _marked_html(runs, ranges, ctx, bold_style), cells(text)
    body = _runs_html(runs, ctx, True)
    return ("<b>%s</b>" % body if item.get("mono") else body), cells(text)


def _picker_lead(item, ctx, layout):
    """(HTML, cells) of the lead: initials mark, icon, or status dot; a corner dot beside mark/icon."""
    tone_name = item.get("tone") if item.get("tone") in TONES else None
    colour = tone_color(tone_name, ctx) if tone_name else _color(ctx, "muted")
    dot = item.get("dot") if item.get("dot") in TONES else None
    if layout == "timeline":
        node = item.get("node")
        if node == "user":
            return '<span style="color: %s">&#9679;</span>' % _color(ctx, "accent"), 1
        if node == "tool":
            span = _icon_span(item.get("icon") or "tool", _color(ctx, "muted")) or _icon_span("wand", _color(ctx, "muted"))
            return span, 2
        return '<span style="color: %s">&#9675;</span>' % _color(ctx, "dim"), 1
    mark = item.get("mark") if isinstance(item.get("mark"), dict) and item["mark"].get("text") else None
    lead, width = "", 0
    if mark:
        lead, width = _mark_chip(mark, ctx), min(2, len(mark["text"].strip()[:2])) + 2
    else:
        glyph = _icon_span(item.get("icon"), colour) if item.get("icon") else None
        if glyph is None and item.get("role"):
            glyph = _icon_span(item.get("role"), colour)
        if glyph is not None:
            lead, width = glyph, 2
    if dot and layout != "tree":
        dot_html = '<span style="color: %s">&#9679;</span>' % tone_color(dot, ctx)
        if lead:
            lead += '<span style="font-size: 0.6rem">%s</span>' % dot_html
        else:
            lead, width = dot_html, 1
    return lead, width


def _picker_row(node, ctx, item, entry_state):
    """One choice: selection marker, lead, name, tags, detail, then the fact columns aligned at the right."""
    node_id = node.get("id")
    layout, selected, current = entry_state["layout"], entry_state["selected"], entry_state["current"]
    shown, hits = entry_state["shown"], entry_state["hits"]
    item_id = item.get("id")
    on = item_id == selected
    disabled = bool(item.get("disabled"))
    total = max(30, _avail(ctx) - 6)
    facts_w = _facts_width(shown)
    bold = "color: %s; font-weight: bold" % _color(ctx, "accent")
    marker = '<span style="color: %s">%s</span>' % (_color(ctx, "accent"), "&#9656;" if on else "&nbsp;")
    guides, guide_w = "", 0
    if layout == "tree":
        depth = int(_number(item.get("depth"), 0))
        lit = item.get("dot") == "accent"
        guides = ('<span style="color: %s">\u2502</span>&nbsp;' % (_color(ctx, "accent") if lit else _color(ctx, "borderMuted"))) * depth
        guide_w = 2 * depth
        if item.get("open") is None:
            guides += "<tt>&nbsp;&nbsp;</tt>"
        else:
            guides += '<span style="color: %s">%s</span>&nbsp;' % (_color(ctx, "dim"), "\u25be" if item.get("open") else "\u25b8")
        guide_w += 2
    lead, lead_w = _picker_lead(item, ctx, layout)
    tags, tags_w = [], 0
    check = ""
    if item_id in current:
        check = ' <span style="color: %s">&#10003; in use</span>' % _color(ctx, "success")
        tags_w += 9
    for role_chip in item.get("chips") or []:
        if isinstance(role_chip, dict) and role_chip.get("on"):         # only the roles this item really has
            tags.append(_role_chip(role_chip, ctx))
            tags_w += cells(str(role_chip.get("text", ""))) + 3
    badges = [b for b in item.get("badges") or [] if isinstance(b, dict)]
    for badge in badges[:3]:
        tags.append(badge_html(str(badge.get("text", "")), badge.get("tone") if badge.get("tone") in TONES else "accent", ctx))
        tags_w += cells(str(badge.get("text", ""))) + 3
    if len(badges) > 3:
        tags.append(badge_html("+%d" % (len(badges) - 3), "neutral", ctx))
        tags_w += 5
    budget = max(8, total - facts_w - 3 - guide_w - lead_w - tags_w)
    label_html, label_w = _picker_label_html(item, ctx, hits.get(item_id) or item.get("hits"), budget, bold)
    detail_html, detail_w = "", 0
    detail_runs = _runs(item.get("detail"))
    if detail_runs and layout != "cards":
        room = total - facts_w - 3 - guide_w - lead_w - tags_w - label_w - 2
        if room >= 3:
            runs = _cut_runs(detail_runs, room)
            detail_html = '&nbsp;&nbsp;<span style="color: %s">%s</span>' % (_color(ctx, "dim"), _runs_html(runs, ctx, True))
            detail_w = cells(_plain(runs)) + 2
    reason = item.get("disabled") if isinstance(item.get("disabled"), str) and item.get("disabled") else ""
    reason_html = ""
    if reason:
        reason_html = '&nbsp;&nbsp;<span style="color: %s">(%s)</span>' % (_color(ctx, "dim"), tsp_html.esc(reason))
        detail_w += cells(reason) + 4
    left = marker + "&nbsp;" + guides + (lead + "&nbsp;" if lead else "") + label_html + check + \
        ("&nbsp;&nbsp;" + "&nbsp;".join(tags) if tags else "") + detail_html + reason_html
    left_w = 2 + guide_w + (lead_w + 1 if lead else 0) + label_w + (9 if check else 0) + (2 + tags_w if tags else 0) + detail_w
    inner = left
    if shown:
        pad = max(1, total - left_w - facts_w)
        inner += "<tt>%s%s</tt>" % (_nbsp(pad), _facts_html(item, shown, ctx))
    else:
        inner += "<tt>%s</tt>" % _nbsp(max(0, total - left_w))
    if disabled:
        inner = '<span style="color: %s">%s</span>' % (_color(ctx, "dim"), inner)
    if ctx.surface_id is not None:
        if on and not disabled:
            event = {"ev": "activate", "sf": ctx.surface_id, "id": node_id, "item": item_id}
        else:
            event = {"ev": "select", "sf": ctx.surface_id, "id": node_id, "item": item_id}
            if not disabled:
                event["dbl"] = {"ev": "activate", "sf": ctx.surface_id, "id": node_id, "item": item_id}
        inner = '<a href="%s" style="text-decoration: none; color: %s">%s</a>' % (tsp_html.event_href(event), _color(ctx, "text"), inner)
    background = "background-color: %s; border-radius: 4px; " % _soft(ctx) if on else ""
    out = '<div style="%spadding: 2px 8px">%s</div>' % (background, inner)
    if layout == "cards" and detail_runs:
        indent = _nbsp(2 + guide_w + (lead_w + 1 if lead else 0))
        out = out[:-len("</div>")] + '<br><tt>%s</tt><span style="color: %s">%s</span></div>' % (
            indent, _color(ctx, "dim"), _runs_html(_cut_runs(detail_runs, total - 8), ctx, True))
    return out


def _confirm_html(node, ctx, confirm):
    act = confirm.get("act") if isinstance(confirm.get("act"), str) and confirm.get("act") else "confirm"
    label = confirm.get("label") if isinstance(confirm.get("label"), str) and confirm.get("label") else "Delete"
    error = _color(ctx, "error")
    danger = ('<span style="background-color: %s; color: %s; border-radius: 6px; padding: 2px 10px">%s</span>'
              % (_alpha(error, 0.2), error, tsp_html.esc(label)))
    keep = '<span style="background-color: %s; color: %s; border-radius: 6px; padding: 2px 10px">Keep</span>' % (
        _soft(ctx), _color(ctx, "text"))
    return ('<div style="background-color: %s; border-radius: 6px; padding: 4px 8px; margin: 2px 0">'
            '<span style="color: %s">%s</span>&nbsp;%s &nbsp;%s&nbsp;&nbsp;%s</div>'
            % (_alpha(_color(ctx, "warning"), 0.16), error, tsp_html.esc(icon_glyph("trash")),
               tsp_html.text_html(confirm.get("text"), ctx), tsp_html._action_link(ctx, node, act, None, danger),
               tsp_html._action_link(ctx, node, "cancel", None, keep)))


def _picker_scope_chip(node, ctx, scope, chosen, ring):
    on = scope.get("id") == chosen
    parts = []
    mark = scope.get("mark") if isinstance(scope.get("mark"), dict) and scope["mark"].get("text") else None
    if mark:
        parts.append(_mark_chip(mark, ctx) + "&nbsp;")
    elif scope.get("icon"):
        glyph = _icon_span(scope.get("icon"), _color(ctx, "accent" if on else "muted"))
        if glyph:
            parts.append(glyph + "&nbsp;")
    if scope.get("dot") in TONES:
        parts.append('<span style="color: %s">&#9679;</span> ' % tone_color(scope["dot"], ctx))
    parts.append(tsp_html.text_html(scope.get("label"), ctx))
    if scope.get("count") is not None:
        parts.append(' <span style="color: %s">%s</span>' % (_color(ctx, "dim"), tsp_html.esc(scope["count"])))
    if scope.get("disabled"):
        parts.append(' <span style="color: %s">%s</span>' % (_color(ctx, "dim"), tsp_html.esc(icon_glyph("lock"))))
    outline = "border: 1px solid %s; " % _color(ctx, "accent") if ring and on else ""
    body = ('<span style="%sbackground-color: %s; color: %s; border-radius: 6px; padding: 1px 8px">%s</span>&nbsp; '
            % (outline, _soft(ctx) if on else "transparent", _color(ctx, "accent" if on else "muted"), "".join(parts)))
    return tsp_html._action_link(ctx, node, "scope", scope.get("id"), body)         # a disabled scope still sends the action


def _picker_scope_group(node, ctx, scopes, chosen, ring):
    """Chips of one run of scopes; a group name heads its run, scopes without a group follow a rule."""
    out, previous = [], None
    for scope in scopes:
        group = scope.get("group") or ""
        if group != previous:
            if group:
                out.append('<div style="color: %s; font-size: 0.85rem; margin-top: 2px">%s</div>' % (_color(ctx, "dim"), tsp_html.esc(group)))
            elif previous:
                out.append('<div style="border-top: 1px solid %s; margin: 2px 0"></div>' % _color(ctx, "borderMuted"))
            previous = group
        out.append(_picker_scope_chip(node, ctx, scope, chosen, ring))
    return "".join(out)


def _picker_scopes(node, ctx, scopes, chosen, ring):
    """Scope choices: the ones you can use, and omp's "Not signed in" group behind one small link."""
    scopes = [s for s in scopes if isinstance(s, dict)]
    usable = [s for s in scopes if s.get("group") != "Not signed in"]
    unsigned = [s for s in scopes if s.get("group") == "Not signed in"]
    out = [_picker_scope_group(node, ctx, usable, chosen, ring)]
    if unsigned:
        group = tsp_html.esc(unsigned[0].get("group") or "")
        key = "unsigned:%s" % node.get("id")
        if ctx.local.get(key):
            out.append(_local_link(ctx, key, False, "&#8722; %s" % group))
            out.append('<div style="margin-top: 2px">%s</div>' % "".join(_picker_scope_chip(node, ctx, s, chosen, ring) for s in unsigned))
        else:
            out.append(_local_link(ctx, key, True, "&#43; %s (%d)" % (group, len(unsigned))))
    return '<div style="margin: 2px 0 6px 0">%s</div>' % "".join(out)


def _picker_tabs(node, ctx, tabs, chosen, ring):
    out = []
    for tab in tabs:
        if not isinstance(tab, dict) or tab.get("id") is None:
            continue
        on = tab.get("id") == chosen
        label = tsp_html.text_html(tab.get("label"), ctx)
        if tab.get("count") is not None:
            label += ' <span style="color: %s">%s</span>' % (_color(ctx, "dim"), tsp_html.esc(tab["count"]))
        outline = "border: 1px solid %s; " % _color(ctx, "accent") if ring and on else ""
        body = ('<span style="%sbackground-color: %s; color: %s; border-radius: 6px; padding: 1px 8px">%s</span>&nbsp;'
                % (outline, _soft(ctx) if on else "transparent", _color(ctx, "accent" if on else "muted"), label))
        out.append(tsp_html._action_link(ctx, node, "tab", tab.get("id"), body))
    return '<div style="margin: 2px 0 6px 0">%s</div>' % "".join(out) if out else ""


def _picker_strip(node, ctx, strip, ring):
    chips = []
    chosen = strip.get("selected")
    for entry in strip.get("items") or []:
        if not isinstance(entry, dict) or entry.get("id") is None:
            continue
        on = bool(entry.get("on"))
        colour = _color(ctx, "accent" if on else "dim")
        dot = '<span style="color: %s">&#9679;</span> ' % tone_color(entry["dot"], ctx) if entry.get("dot") in TONES else ""
        check = "&#10003; " if on else ""
        outline = "border: 1px solid %s; " % _color(ctx, "accent") if entry.get("id") == chosen else ""
        body = ('<span style="%sbackground-color: %s; color: %s; border-radius: 9px; padding: 1px 8px; font-size: 0.85rem">%s%s%s</span>&nbsp;'
                % (outline, _alpha(colour, 0.16), colour, check, dot, tsp_html.text_html(entry.get("label"), ctx)))
        chips.append(tsp_html._action_link(ctx, node, "strip", entry.get("id"), body))
    return '<div style="margin-top: 4px">%s %s</div>' % (tsp_html.text_html(strip.get("label"), ctx), "".join(chips))


def _action_button(node, ctx, action, show_keys):
    keys = [k for k in action.get("keys") or [] if isinstance(k, str)]
    label = action.get("label")
    label = tsp_html.text_html(label, ctx) if label else tsp_html.esc(action.get("id") or "")
    disabled = bool(action.get("disabled"))
    accent, error = _color(ctx, "accent"), _color(ctx, "error")
    if disabled:
        look = "background-color: %s; color: %s" % (_alpha(_color(ctx, "dim"), 0.12), _color(ctx, "dim"))
    elif action.get("primary"):
        look = "background-color: %s; color: %s" % (accent, _color(ctx, "pageBg"))
    elif action.get("danger"):
        look = "background-color: %s; color: %s" % (_alpha(error, 0.2), error)
    elif action.get("on"):
        look = "background-color: %s; color: %s; border: 1px solid %s" % (_alpha(accent, 0.22), accent, accent)
    else:
        look = "background-color: %s; color: %s" % (_soft(ctx), _color(ctx, "text"))
    caps = ("&nbsp;" + keycaps_html(keys, ctx)) if keys and show_keys else ""
    body = '<span style="%s; border-radius: 6px; padding: 3px 12px">%s</span>%s' % (look, label, caps)
    width = cells(tsp_html.plain_text(action.get("label")) or str(action.get("id") or "")) + 6 + \
        (sum(cells(key_label(k)) + 3 for k in keys) if keys and show_keys else 0)
    if disabled:
        return body, width
    return tsp_html._action_link(ctx, node, action.get("id"), None, body), width


def _action_bar(node, ctx, actions):
    """Secondary actions at the left, `end` ones then the primary at the right; keycaps fold away when the bar overflows."""
    usable = [a for a in actions if isinstance(a, dict) and a.get("id") is not None]
    left = [a for a in usable if not a.get("end") and not a.get("primary")]
    right = [a for a in usable if a.get("end") and not a.get("primary")] + [a for a in usable if a.get("primary")]
    total = max(30, _avail(ctx) - 6)

    def build(show_secondary_keys):
        left_html = [_action_button(node, ctx, a, show_secondary_keys) for a in left]
        right_html = [_action_button(node, ctx, a, True) for a in right]
        return left_html, right_html

    left_html, right_html = build(True)
    used = sum(w + 2 for _h, w in left_html + right_html)
    if used > total:
        left_html, right_html = build(False)
        used = sum(w + 2 for _h, w in left_html + right_html)
    pad = max(2, total - used)
    left_text = "&nbsp;&nbsp;".join(h for h, _w in left_html)
    right_text = "&nbsp;&nbsp;".join(h for h, _w in right_html)
    middle = "<tt>%s</tt>" % _nbsp(pad) if left_html and right_html else ("&nbsp;&nbsp;" if left_html else "")
    if right_html and not left_html:
        middle = "<tt>%s</tt>" % _nbsp(pad)
    return '<div style="margin-top: 8px">%s%s%s</div>' % (left_text, middle, right_text)


def _utf16_cursor(query, cursor):
    """A caret position in UTF-16 units -> an index into ``query``."""
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        return len(query)
    units = 0
    for number, char in enumerate(query):
        if units >= cursor:
            return number
        units += 2 if ord(char) > 0xFFFF else 1
    return len(query)


def _search_html(props, ctx, noun, caret):
    query = props.get("query")
    theme = ctx.theme
    if query:
        at = _utf16_cursor(query, props.get("cursor"))
        mark = '<span style="color: %s">&#9615;</span>' % theme.color("accent") if caret else ""
        field = tsp_html.esc(query[:at]) + mark + tsp_html.esc(query[at:])
    else:
        placeholder = props.get("placeholder") if isinstance(props.get("placeholder"), str) else "Search %s\u2026" % noun
        field = ('<span style="color: %s">%s</span>' % (theme.color("dim"), tsp_html.esc(placeholder))
                 + ('<span style="color: %s">&#9615;</span>' % theme.color("accent") if caret else ""))
    return field


def _skeleton_rows(ctx):
    widths = (22, 30, 18, 26, 34, 20, 28, 16)
    return "".join('<div style="color: %s; padding: 2px 8px"><tt>%s</tt></div>' % (
        _alpha(_color(ctx, "border"), 1.0), "\u2591" * width) for width in widths)


def _draw_picker(node, ctx):
    """A choice sheet a person can follow: heading and search, scopes, tabs, a few rows around the
    selection with fact columns, a preview, a strip and the action bar.  Looks like omp's picker."""
    props = _props(node)
    theme = ctx.theme
    node_id = node.get("id")
    parts = []
    entries = _picker_entries(props)
    shown_total = sum(1 for kind, _ in entries if kind == "item")
    total = props.get("total")
    noun = props.get("noun") if isinstance(props.get("noun"), str) and props.get("noun") else "items"
    state = props.get("state") if props.get("state") in ("loading", "error") else "ready"
    query = props.get("query") if isinstance(props.get("query"), str) else None
    focus = props.get("focus") if props.get("focus") in ("list", "scopes", "tabs", "strip", "preview") else "list"
    size = props.get("size") if props.get("size") in ("md", "screen") else "lg"
    layout = props.get("layout") if props.get("layout") in ("cards", "timeline", "tree") else "rows"
    actions = [a for a in props.get("actions") or [] if isinstance(a, dict) and a.get("id") is not None]

    # 1. heading: icon, title, count, esc
    if state == "loading":
        counts = "\u2026"
    else:
        counts = "{:,} {}".format(shown_total, noun)
        if isinstance(total, int) and not isinstance(total, bool) and total != shown_total:
            counts += " (of {:,})".format(total)
    head_icon = _icon_span(props.get("icon") or "list", theme.color("accent")) or ""
    esc_action = next((a for a in actions if any(str(k).lower() in ("esc", "escape") for k in a.get("keys") or [])), None)
    esc_html = ""
    if esc_action is None:
        esc_html = "&nbsp;&nbsp;" + tsp_html._action_link(ctx, node, "close", None, keycaps_html(["esc"], ctx))
    title = props.get("title")
    has_search = query is not None
    if title:
        parts.append('<div>%s<b style="color: %s">%s</b> &nbsp;<span style="color: %s">%s</span>%s</div>'
                     % (head_icon + "&nbsp;" if head_icon else "", theme.color("accent"), tsp_html.text_html(title, ctx),
                        theme.color("dim"), tsp_html.esc(counts), esc_html))
        if props.get("subtitle") and not (size == "md" and query):
            parts.append('<div style="color: %s">%s</div>' % (theme.color("muted"), tsp_html.text_html(props["subtitle"], ctx)))
    # 2. search
    if has_search:
        field = _search_html(props, ctx, noun, focus == "list")
        lead = "&#8981; " if title else (head_icon + "&nbsp;" if head_icon else "&#8981; ")
        tail = "" if title else '&nbsp;&nbsp;<span style="color: %s">%s</span>%s' % (theme.color("dim"), tsp_html.esc(counts), esc_html)
        parts.append('<div style="background-color: %s; border-radius: 6px; padding: 3px 10px; margin: 4px 0">%s%s%s</div>'
                     % (theme.color("pageBg"), lead, field, tail))
    elif not title:
        parts.append("<div>%s%s</div>" % (head_icon, esc_html))
    # 3. narrowing choices
    if props.get("scopes"):
        parts.append(_picker_scopes(node, ctx, props["scopes"], props.get("scope"), focus == "scopes"))
    if props.get("tabs"):
        tabs_html = _picker_tabs(node, ctx, props["tabs"], props.get("tab"), focus == "tabs")
        if tabs_html:
            parts.append(tabs_html)
    # 4. state and rows
    selected = props.get("selected")
    current = set(x for x in props.get("current") or [] if isinstance(x, str))
    hits = props.get("hits") if isinstance(props.get("hits"), dict) else {}
    shown = _picker_columns(props, _avail(ctx))
    _grow_text_columns(shown, entries, max(0, _avail(ctx) - 6 - _LABEL_MIN))
    state_info = {"layout": layout, "selected": selected, "current": current, "has_cur": bool(current),
                  "shown": shown, "hits": hits}
    if state == "loading":
        parts.append(_skeleton_rows(ctx))
    elif state == "error":
        retry = next((a for a in actions if a.get("id") == "retry"), None)
        body = '<span style="color: %s">%s</span>' % (theme.color("error"), tsp_html.text_html(props.get("message"), ctx))
        if retry is not None:
            body += "&nbsp;&nbsp;" + _action_button(node, ctx, retry, False)[0]
        parts.append('<div style="background-color: %s; border-radius: 6px; padding: 4px 8px; margin: 4px 0">%s</div>'
                     % (_alpha(theme.color("error"), 0.14), body))
    elif not entries:
        if query:
            clear = tsp_html._action_link(ctx, node, "clear", None, badge_html("Clear search", "accent", ctx))
            parts.append('<div style="color: %s">No %s match &ldquo;%s&rdquo;. &nbsp;%s</div>'
                         % (theme.color("dim"), tsp_html.esc(noun), tsp_html.esc(query), clear))
        else:
            empty = props.get("empty")
            text = tsp_html.text_html(empty, ctx) if empty else "No %s yet" % tsp_html.esc(noun)
            tab_action = next((a for a in actions if any(str(k).lower() == "tab" for k in a.get("keys") or [])), None)
            button = "&nbsp;&nbsp;" + _action_button(node, ctx, tab_action, False)[0] if tab_action is not None else ""
            parts.append('<div style="color: %s">%s%s</div>' % (theme.color("dim"), text, button))
    if state == "ready" and entries:
        limit = ctx.local.get("rows:%s" % node_id)
        limit = limit if isinstance(limit, int) and limit > 0 else PICKER_ROWS
        ids = [entry.get("id") if kind == "item" else None for kind, entry in entries]
        position = next((n for n, (kind, entry) in enumerate(entries) if kind == "item" and entry.get("id") == selected), None)
        first = _window(ctx, node_id, len(entries), limit, selected, position, ids)
        window = entries[first:first + limit]
        if first:
            parts.append(_more_line(ctx, node_id, "&#8593; %d more above" % first, max(0, first - limit), "page up"))
        heads = any(col["head"] for col in shown)
        head_html = _heads_html(shown, ctx) if heads else ""
        head_pad = max(1, _avail(ctx) - 6 - _facts_width(shown)) if heads else 0
        if heads and window and window[0][0] != "group":
            parts.append('<div style="padding: 0 8px"><tt>%s%s</tt></div>' % (_nbsp(head_pad), head_html))
        confirm = props.get("confirm") if isinstance(props.get("confirm"), dict) else None
        for number, (kind, entry) in enumerate(window):
            if kind == "group":
                label_html = tsp_html.text_html(entry.get("label"), ctx)
                count = entry.get("count")
                if isinstance(count, (int, float)) and not isinstance(count, bool):
                    label_html += ' <span style="color: %s">(%s)</span>' % (theme.color("dim"), "{:,}".format(int(count)))
                line = '<span style="color: %s"><b>%s</b></span>' % (theme.color("muted"), label_html)
                if heads and number == 0:
                    used = _visible_cells(line)
                    line += "<tt>%s%s</tt>" % (_nbsp(max(1, _avail(ctx) - 6 - used - _facts_width(shown))), head_html)
                parts.append('<div style="margin-top: 4px; padding: 0 8px">%s</div>' % line)
            else:
                parts.append(_picker_row(node, ctx, entry, state_info))
                if confirm and entry.get("id") == selected:
                    parts.append(_confirm_html(node, ctx, confirm))
        below = len(entries) - first - len(window)
        if below > 0:
            more = _local_link(ctx, "rows:%s" % node_id, limit + PICKER_MORE, "show %d more rows" % PICKER_MORE)
            page = _local_link(ctx, "scroll:%s" % node_id, min(len(entries) - limit, first + limit), "page down")
            parts.append('<div style="color: %s">&#8595; %d more below &nbsp;%s &nbsp;%s</div>' % (theme.color("dim"), below, page, more))
    # 5. preview
    preview_mode = props.get("preview") if props.get("preview") in ("side", "below", "none") else ("none" if size == "md" else "side")
    if preview_mode != "none":
        inner = tsp_html._children(node, ctx) if node.get("c") else '<span style="color: %s">Nothing to preview</span>' % theme.color("dim")
        ring = "border: 1px solid %s; " % theme.color("accent") if focus == "preview" else ""
        parts.append('<div style="%sborder-top: 1px solid %s; margin-top: 6px; padding-top: 4px">%s</div>'
                     % (ring, theme.color("border"), inner))
    # 6. strip
    if isinstance(props.get("strip"), dict):
        parts.append(_picker_strip(node, ctx, props["strip"], focus == "strip"))
    # 7. buttons and how to use it
    if actions:
        parts.append(_action_bar(node, ctx, actions))
    hints = [(chr(8593) + chr(8595), "move"), ("Enter", "choose")] + ([("type", "search")] if has_search else []) + [("Esc", "close")]
    parts.append('<div style="color: %s; margin-top: 6px">%s</div>' % (
        theme.color("dim"), " &nbsp; ".join(tsp_html.keycap([key], ctx) + " " + label for key, label in hints)))
    return tsp_html._card_shell("".join(parts), ctx, "cardBg", "borderAccent")


DRAWERS = {
    "kv": _draw_kv, "table": _draw_table, "tree": _draw_tree, "badge": _draw_badge, "kbd": _draw_kbd,
    "icon": _draw_icon, "image": _draw_image, "list": _draw_list, "item": _draw_item, "tabs": _draw_tabs,
    "picker": _draw_picker,
}
