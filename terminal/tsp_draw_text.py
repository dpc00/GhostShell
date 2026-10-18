"""TSP drawers for: text, md, code, diff, ansi, rows, math (spec: el_text.md).

DRAWERS maps a kind to a drawer function(node, ctx) -> HTML; tsp_html registers it over its own.

Everything here is built for Sublime's minihtml: no tables, no flexbox, no pseudo classes, no
animation.  Text that must keep its columns (code, diffs, tables, math, terminal output) is laid out
in character cells and padded with no-break spaces; the view's font is monospace, so a cell is a
character.  Public helpers other drawers may call:

    spans_html(value, ctx, base_tokens="", key=None)   a str or span list as inline HTML (the shared span path)
    text_inline(node, ctx)                             a ``text`` node as inline HTML (no wrapping div)
    code_block_html(text, ctx, lang=None, ...)         a highlighted code block
    math_html(tex, ctx, display=False)                 a TeX formula
"""

import difflib
import functools
import html
import math
import re
import sys
import unicodedata

from . import tsp_html, tsp_style

DRAWERS = {}

# ---------------------------------------------------------------------------------------------
# colours
# ---------------------------------------------------------------------------------------------

_HEX6 = re.compile(r"^#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})(?:[0-9a-fA-F]{2})?$")
_HEX3 = re.compile(r"^#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])$")


def _rgb(color):
    if not isinstance(color, str):
        return None
    match = _HEX6.match(color)
    if match:
        return tuple(int(group, 16) for group in match.groups())
    match = _HEX3.match(color)
    if match:
        return tuple(int(group * 2, 16) for group in match.groups())
    return None


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def _tint(color, alpha):
    """``color`` at ``alpha`` strength as "#rrggbbaa" (a background tint); unchanged when it is not a hex colour."""
    rgb = _rgb(color)
    if rgb is None:
        return color
    return "%s%02x" % (_hex(rgb), int(max(0.0, min(1.0, alpha)) * 255))


def _mix(first, second, amount):
    """``first`` moved ``amount`` (0..1) of the way to ``second``."""
    a, b = _rgb(first), _rgb(second)
    if a is None or b is None:
        return first
    return _hex(tuple(a[i] + (b[i] - a[i]) * amount for i in range(3)))


def _avail(ctx):
    """The cells a line of this node may use (the host shrinks it for indentation), else the whole width."""
    value = getattr(ctx, "avail", None)
    return value if isinstance(value, int) and value > 0 else ctx.cols


def _tok(ctx, name):
    return ctx.theme.tokens.get(name)


def _col(ctx, name, default=None):
    return _tok(ctx, name) or default or ctx.theme.tokens.get("muted") or "#9ca3b0"


def _text_color(ctx):
    return _col(ctx, "text", "#e6eaf0")


def _page_bg(ctx):
    return _col(ctx, "pageBg", "#0d1117")


def _info_color(ctx):
    return _tok(ctx, "info") or _col(ctx, "accent")


# ---------------------------------------------------------------------------------------------
# character cells
# ---------------------------------------------------------------------------------------------

def _cw(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _cells(text):
    if text.isascii():
        return len(text)
    return sum(_cw(ch) for ch in text)


def _take(text, budget):
    """How many leading characters of ``text`` fit in ``budget`` cells (at least one when text is not empty)."""
    if text.isascii():
        return max(1, budget) if text else 0
    used = count = 0
    for ch in text:
        width = _cw(ch)
        if used + width > budget and count:
            break
        used += width
        count += 1
    return count


def _take_end(text, budget):
    """How many trailing characters of ``text`` fit in ``budget`` cells."""
    used = count = 0
    for ch in reversed(text):
        width = _cw(ch)
        if used + width > budget:
            break
        used += width
        count += 1
    return count


def _nbsp(text):
    return text.replace(" ", "&nbsp;")


def _e(text):
    return tsp_html.esc(text)


def _attr(text):
    return html.escape(text, quote=True)


# ---------------------------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------------------------

def _open_href(href):
    """The href a click on a link sends to the view (``local: open``); the view opens URLs and files."""
    return tsp_html.event_href({"local": "open", "href": href})


# ---------------------------------------------------------------------------------------------
# span tokens
# ---------------------------------------------------------------------------------------------

_SEMANTIC = frozenset(("muted", "dim", "strong", "em", "accent", "success", "warning", "error", "info", "code",
                       "mono", "path", "key", "link", "num", "ins", "del", "mark", "typo", "icon", "hide"))

# omp's theme tokens -> the semantic token that colours them (None = plain, "" = a fill: never colours text).
_PROGRAM_MAP = {}
for _names, _target in (
        (("text", "customMessageText", "syntaxVariable", "toolOutput", "userMessageText"), None),
        (("borderAccent", "customMessageLabel", "mdListBullet", "statusLineModel", "syntaxFunction",
          "thinkingMedium"), "accent"),
        (("borderMuted", "statusLineSep", "thinkingOff"), "dim"),
        (("border", "mdCodeBlockBorder", "mdHr", "mdLinkUrl", "mdQuote", "mdQuoteBorder", "statusLineOutput",
          "statusLineSpend", "statusLineUntracked", "syntaxComment", "syntaxOperator", "syntaxPunctuation",
          "thinkingText", "thinkingMinimal", "toolDiffContext"), "muted"),
        (("thinkingXhigh", "thinkingMax", "toolDiffRemoved"), "error"),
        (("statusLineCost", "statusLineGitClean", "statusLineStaged", "syntaxString"), "success"),
        (("bashMode", "statusLineDirty", "statusLineGitDirty", "thinkingHigh"), "warning"),
        (("pythonMode", "statusLineContext", "statusLineSubagents", "syntaxKeyword", "syntaxType", "thinkingLow"),
         "info"),
        (("mdCode",), "code"),
        (("mdCodeBlock",), "mono"),
        (("mdHeading", "toolTitle"), "strong"),
        (("mdLink",), "link"),
        (("statusLinePath",), "path"),
        (("syntaxNumber",), "num"),
        (("toolDiffAdded",), "ins"),
        (("customMessageBg", "selectedBg", "statusLineBg", "toolErrorBg", "toolPendingBg", "toolSuccessBg",
          "userMessageBg"), "")):
    for _name in _names:
        _PROGRAM_MAP[_name] = _target

_TOKEN_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def _semantic_color(ctx, sem):
    if sem == "info":
        return _info_color(ctx)
    if sem == "code":
        return _col(ctx, "mdCode")
    if sem == "path":
        return _col(ctx, "statusLinePath", _col(ctx, "muted"))
    if sem == "link":
        return _col(ctx, "mdLink", _col(ctx, "accent"))
    if sem == "num":
        return _col(ctx, "syntaxNumber", _col(ctx, "warning"))
    if sem == "ins":
        return _col(ctx, "toolDiffAdded", _col(ctx, "success"))
    if sem == "del":
        return _col(ctx, "toolDiffRemoved", _col(ctx, "error"))
    if sem == "key":
        return _col(ctx, "accent")
    return _col(ctx, sem)


class _Run(object):
    """A styled piece of inline text.  ``cls`` is "" or one of path/key/icon/code (the pieces that draw differently)."""

    __slots__ = ("text", "color", "bg", "css", "href", "title", "fx", "cls", "gap")

    def __init__(self, text, color=None, bg=None, css="", href=None, title=None, fx=None, cls="", gap=False):
        self.text, self.color, self.bg, self.css = text, color, bg, css
        self.href, self.title, self.fx, self.cls, self.gap = href, title, fx, cls, gap

    def clone(self, text):
        return _Run(text, self.color, self.bg, self.css, self.href, self.title, self.fx, self.cls, self.gap)


def _is_key_id(text):
    return bool(re.match(r"^[^\s+]+(?:\+[^\s+]+)*$", text))


_MAC_KEYS = {"ctrl": "⌃", "control": "⌃", "shift": "⇧", "alt": "⌥", "option": "⌥", "opt": "⌥", "cmd": "⌘",
             "command": "⌘", "meta": "⌘", "super": "⌘", "enter": "↵", "return": "↵", "esc": "⎋", "escape": "⎋",
             "tab": "⇥", "backspace": "⌫", "delete": "⌦", "del": "⌦", "space": "␣", "up": "↑", "down": "↓",
             "left": "←", "right": "→", "pageup": "⇞", "pagedown": "⇟", "home": "↖", "end": "↘"}
_WORD_KEYS = {"ctrl": "Ctrl", "control": "Ctrl", "shift": "Shift", "alt": "Alt", "option": "Alt", "opt": "Alt",
              "cmd": "Win" if sys.platform == "win32" else "Super", "command": "Win" if sys.platform == "win32" else "Super",
              "meta": "Win" if sys.platform == "win32" else "Super", "super": "Win" if sys.platform == "win32" else "Super",
              "enter": "Enter", "return": "Enter", "esc": "Esc", "escape": "Esc", "tab": "Tab", "backspace": "Backspace",
              "delete": "Del", "del": "Del", "space": "Space", "up": "↑", "down": "↓", "left": "←", "right": "→",
              "pageup": "PgUp", "pagedown": "PgDn", "home": "Home", "end": "End"}


def keycap_label(key_id):
    """The platform keycap text for a ``ctrl+r`` style id (``⌃R`` on a Mac, ``Ctrl+R`` elsewhere)."""
    parts = key_id.split("+")
    mac = sys.platform == "darwin"
    table = _MAC_KEYS if mac else _WORD_KEYS
    out = []
    for part in parts:
        low = part.lower()
        if low in table:
            out.append(table[low])
        elif len(part) == 1 or re.match(r"^[fF]\d{1,2}$", part):
            out.append(part.upper())
        else:
            out.append(part[:1].upper() + part[1:])
    return "".join(out) if mac else "+".join(out)


def _style_runs(text, tokens, ctx):
    """The runs for one span's text under the space-separated ``tokens`` (usually one run)."""
    sem = []
    palette_color = None
    for token in (tokens or "").split():
        if token in _SEMANTIC:
            sem.append(token)
        elif token in _PROGRAM_MAP:
            target = _PROGRAM_MAP[token]
            if target == "":
                continue                # a fill never colours text
            if palette_color is None and _tok(ctx, token):
                palette_color = _tok(ctx, token)
            if target:
                sem.append(target)
        elif _TOKEN_NAME.match(token) and not token.endswith("Bg"):
            if palette_color is None and _tok(ctx, token):
                palette_color = _tok(ctx, token)
    if "hide" in sem or not text:
        return []
    color = None
    bg = None
    css = []
    cls = ""
    underline = strike = False
    for token in sem:
        if token in ("muted", "dim", "accent", "success", "warning", "error", "info", "link", "num", "ins"):
            color = _semantic_color(ctx, token)
        elif token == "del":
            color = _semantic_color(ctx, "del")
            strike = True
        elif token == "strong":
            css.append("font-weight: bold")
        elif token == "em":
            css.append("font-style: italic")
        elif token == "code":
            color = _semantic_color(ctx, "code")
            bg = "#ffffff14"
            cls = cls or "code"
        elif token == "path":
            cls = "path"
        elif token == "key":
            cls = "key"
        elif token == "mark":
            bg = _col(ctx, "selectedBg", "#1b2733")
        elif token == "typo":
            underline = True
            color = _col(ctx, "error")
        elif token == "icon":
            cls = "icon"
    if "strong" in sem and color is None and palette_color is None:
        color = _text_color(ctx)
    if palette_color:
        color = palette_color
    if underline or strike:
        css.append("text-decoration: %s" % " ".join(
            name for name, on in (("underline", underline), ("line-through", strike)) if on))
    return [_Run(text, color, bg, "; ".join(css), cls=cls)]


def build_runs(value, ctx, base_tokens=""):
    """A str or a list of str / {"t","s","href","fx"} spans as runs."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    runs = []
    for span in value:
        if isinstance(span, str):
            text, tokens, href, fx = span, "", None, None
        elif isinstance(span, dict):
            text, tokens, href, fx = span.get("t"), span.get("s"), span.get("href"), span.get("fx")
        else:
            continue
        if not isinstance(text, str) or not text:
            continue
        tokens = ("%s %s" % (base_tokens, tokens if isinstance(tokens, str) else "")).strip()
        text = tsp_html._plain_symbols(text)
        pieces = _style_runs(text, tokens, ctx)
        for piece in pieces:
            if isinstance(href, str) and href:
                piece.href = href
            if fx in ("shimmer", "pulse"):
                piece.fx = fx
        runs.extend(pieces)
    # a visible icon gets a small gap when the next run does not start with white space
    for index, run in enumerate(runs[:-1]):
        if run.cls == "icon" and run.text:
            nxt = runs[index + 1].text
            run.gap = bool(nxt) and not nxt[0].isspace()
    return runs


# ---------------------------------------------------------------------------------------------
# drawing runs
# ---------------------------------------------------------------------------------------------

_POSITION = re.compile(r"^(.*?)((?::\d+(?::\d+)?(?:-\d+)?)|(?:#L\d+(?:-L?\d+)?))$", re.S)


def _now(ctx):
    return getattr(ctx.clock, "now", None)


def _effect_html(run, ctx, text):
    """Shimmer / pulse as colour changes driven by the document clock; None when the clock is still."""
    now = _now(ctx)
    if now is None or run.fx not in ("shimmer", "pulse") or not text.strip():
        return None
    ctx.animated = True
    if run.fx == "pulse":
        phase = (now % 1.6) / 1.6
        opacity = 0.45 + 0.55 * (0.5 + 0.5 * math.cos(phase * 2 * math.pi))
        opacity = round(opacity * 20) / 20.0
        base = run.color or _text_color(ctx)
        color = _mix(_page_bg(ctx), base, opacity)
        return '<span style="color: %s">%s</span>' % (color, _e(text))
    low, mid, high = _col(ctx, "dim"), _col(ctx, "muted"), _text_color(ctx)
    count = len(text)
    centre = (now * 18.0) % (count + 14) - 7
    out, level, buffer = [], None, []
    for index, ch in enumerate(text):
        distance = abs(index - centre)
        here = high if distance < 1.5 else mid if distance < 3.5 else low
        if here != level and buffer:
            out.append('<span style="color: %s">%s</span>' % (level, _e("".join(buffer))))
            buffer = []
        level = here
        buffer.append(ch)
    if buffer:
        out.append('<span style="color: %s">%s</span>' % (level, _e("".join(buffer))))
    return "".join(out)


def _run_style(run, color=None):
    style = []
    color = color or run.color
    if color:
        style.append("color: %s" % color)
    if run.bg:
        style.append("background-color: %s" % run.bg)
    if run.css:
        style.append(run.css)
    return "; ".join(style)


def _run_html(run, ctx, hard=False):
    """One run as inline HTML.  ``hard`` makes spaces no-break (text already laid out into lines)."""
    text = run.text
    if not text:
        return ""
    if run.cls == "key" and _is_key_id(text):
        label, title = keycap_label(text), text
        inner = ('<span style="background-color: %s; color: %s; border: 1px solid %s; border-radius: 4px; '
                 'padding: 0 5px"><tt>%s</tt></span>'
                 % (_col(ctx, "selectedBg", "#1b2733"), run.color or _col(ctx, "accent"), _col(ctx, "border"), _e(label)))
        return _anchor(inner, run, ctx, title)
    effect = None if run.cls else _effect_html(run, ctx, text)
    if run.cls == "path":
        inner = _path_html(run, ctx, hard)
    elif effect is not None:
        inner = effect
    else:
        body = _e(text)
        if hard:
            body = _nbsp(body)
        body = body.replace("\n", "<br>")
        style = _run_style(run)
        if run.cls == "code":
            style += "; border-radius: 3px; padding: 0 3px"
        inner = '<span style="%s">%s</span>' % (style, body) if style else body
    if run.gap:
        inner += "&nbsp;"
    return _anchor(inner, run, ctx, None)


def _anchor(inner, run, ctx, title):
    if not run.href:
        return inner
    tip = ' title="%s"' % _attr(title or run.title or run.href)
    return '<a href="%s"%s style="color: %s; text-decoration: none">%s</a>' % (
        _open_href(run.href), tip, run.color or _col(ctx, "mdLink", _col(ctx, "accent")), inner)


def _path_html(run, ctx, hard):
    """A path: the directory quiet, the file name in the path colour, a ``:12`` / ``#L3`` position quiet."""
    text = run.text
    position = ""
    match = _POSITION.match(text)
    if match and match.group(1):
        text, position = match.group(1), match.group(2)
    slash = text.rfind("/")
    if slash < 0:
        slash = text.rfind("\\")
    directory, name = (text[:slash + 1], text[slash + 1:]) if slash >= 0 else ("", text)
    quiet = _col(ctx, "dim") if run.color is None else _mix(run.color, _page_bg(ctx), 0.45)
    name_color = run.color or _col(ctx, "statusLinePath", _col(ctx, "muted"))
    fix = (lambda s: _nbsp(_e(s))) if hard else _e
    out = []
    if directory:
        out.append('<span style="color: %s">%s</span>' % (quiet, fix(directory)))
    style = "color: %s" % name_color
    if run.css:
        style += "; " + run.css
    out.append('<span style="%s">%s</span>' % (style, fix(name)))
    if position:
        out.append('<span style="color: %s">%s</span>' % (quiet, fix(position)))
    return "".join(out)


def _runs_html(runs, ctx, hard=False):
    return "".join(_run_html(run, ctx, hard) for run in runs)


def spans_html(value, ctx, base_tokens="", key=None):
    """A str or a list of spans as inline HTML: every span token, program token, link, key cap and effect."""
    if value is None:
        return ""
    return "".join(_run_html(run, ctx) for run in build_runs(value, ctx, base_tokens)).replace("\n", "<br>")


# ---------------------------------------------------------------------------------------------
# fitting runs to a width: wrap, truncate, clamp
# ---------------------------------------------------------------------------------------------

def _line_text(runs):
    return "".join(run.text for run in runs)


def _cut_line(runs, start, end):
    """The part of a line (list of runs) between character offsets ``start`` and ``end``."""
    out, pos = [], 0
    for run in runs:
        size = len(run.text)
        a, b = max(start, pos), min(end, pos + size)
        if a < b:
            out.append(run.clone(run.text[a - pos:b - pos]))
        pos += size
        if pos >= end:
            break
    return out


def _run_at(runs, offset):
    pos = 0
    for run in runs:
        pos += len(run.text)
        if offset < pos:
            return run
    return runs[-1] if runs else None


def _split_logical(runs):
    """Runs -> a list of lines (each a list of runs), split at newlines."""
    lines, current = [], []
    for run in runs:
        parts = run.text.split("\n")
        for index, part in enumerate(parts):
            if index:
                lines.append(current)
                current = []
            if part:
                current.append(run.clone(part))
    lines.append(current)
    return lines


def _ellipsis(like):
    return like.clone("…") if like is not None else _Run("…")


def _truncate_line(runs, width, where):
    text = _line_text(runs)
    if _cells(text) <= width:
        return runs, False
    if where == "middle":
        runs = _drop_directories(runs, width)
        text = _line_text(runs)
        if _cells(text) <= width:
            return runs, True
    keep = max(1, width - 1)
    size = len(text)
    if where == "start":
        count = _take_end(text, keep)
        first = size - count
        return [_ellipsis(_run_at(runs, first))] + _cut_line(runs, first, size), True
    if where == "middle":
        left = _take(text, (keep + 1) // 2)
        right = _take_end(text, keep // 2)
        out = _cut_line(runs, 0, left) + [_ellipsis(_run_at(runs, max(0, left - 1)))]
        if right:
            out += _cut_line(runs, size - right, size)
        return out, True
    count = _take(text, keep)
    return _cut_line(runs, 0, count) + [_ellipsis(_run_at(runs, max(0, count - 1)))], True


def _drop_directories(runs, width):
    """``truncate = "middle"`` with a path span: drop whole leading directories of it before cutting the text."""
    for index, run in enumerate(runs):
        if run.cls != "path":
            continue
        match = _POSITION.match(run.text)
        body, position = (match.group(1), match.group(2)) if match and match.group(1) else (run.text, "")
        segments = body.split("/")
        current = runs
        for drop in range(1, len(segments)):
            shortened = run.clone("…/" + "/".join(segments[drop:]) + position)
            current = runs[:index] + [shortened] + runs[index + 1:]
            if _cells(_line_text(current)) <= width:
                return current
        return current
    return runs


def _wrap_line(runs, width, mode):
    """One logical line -> visual lines of at most ``width`` cells, broken by word or by character."""
    text = _line_text(runs)
    if _cells(text) <= width:
        return [runs]
    lines, i, size = [], 0, len(text)
    while i < size:
        j = i + _take(text[i:], width)
        if j >= size:
            lines.append(_cut_line(runs, i, size))
            break
        if mode == "word" and text[j] != " ":
            k = text.rfind(" ", i, j)
            if k > i:
                j = k
        lines.append(_cut_line(runs, i, j))
        i = j
        if mode == "word":
            while i < size and text[i] == " ":
                i += 1
    return lines


def _rtrim(runs):
    runs = list(runs)
    while runs:
        stripped = runs[-1].text.rstrip(" ")
        if stripped:
            runs[-1] = runs[-1].clone(stripped)
            break
        runs.pop()
    return runs


def _clamp(lines, count, width):
    if len(lines) <= count:
        return lines, False
    kept = lines[:count]
    last = _rtrim(kept[-1])
    text = _line_text(last)
    like = last[-1] if last else None
    if _cells(text) + 1 > width and text:
        last = _cut_line(last, 0, len(text) - 1)
    kept[-1] = last + [_ellipsis(like)]
    return kept, True


def _plain_runs(runs):
    return "".join(run.text for run in runs)


# ---------------------------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------------------------

def _int_prop(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == value:
        return int(value)
    return None


def text_inline(node, ctx):
    """A text node as inline HTML (no wrapping div) so it can sit beside an icon or keycap."""
    props = node.get("p") or {}
    spans = props.get("spans")
    if isinstance(spans, list):
        return spans_html(spans, ctx, key=node.get("id"))
    text = props.get("text")
    text = text if isinstance(text, str) else ""
    if (props.get("role") or "").startswith("omp."):
        return tsp_html._md_inline(text, ctx)
    return spans_html(text, ctx, key=node.get("id"))


def _draw_text(node, ctx):
    props = node.get("p") or {}
    role = props.get("role") or ""
    spans = props.get("spans")
    text = props.get("text")
    title_attr = ""
    fitted = None
    if not isinstance(spans, list) and role.startswith("omp."):
        # omp writes light markdown (`code`, **bold**, links) into plain text nodes of its own roles
        body = tsp_html._md_inline(text if isinstance(text, str) else "", ctx).replace("\n", "<br>")
    else:
        runs = build_runs(spans if isinstance(spans, list) else (text if isinstance(text, str) else ""), ctx)
        fitted = _fit_text(runs, props, ctx)
        body, cut_title = fitted
        if cut_title and not props.get("title"):
            title_attr = ' title="%s"' % _attr(cut_title)
    if role.endswith("heading"):
        return '<div%s style="color: %s; font-weight: bold; margin-top: 4px">%s</div>' % (
            title_attr, ctx.theme.color("mdHeading"), body)
    if role == "omp.approval.title":
        return '<div%s style="color: %s; font-weight: bold">%s</div>' % (
            title_attr, ctx.theme.color(props.get("tone") if props.get("tone") == "accent" else "text"), body)
    if role.endswith(("version", "greeting")):
        return '<div%s style="color: %s">%s</div>' % (title_attr, ctx.theme.color("muted"), body)
    return "<div%s>%s</div>" % (title_attr, body)


def _fit_text(runs, props, ctx):
    """(HTML of the runs laid out as the text props ask, the full text when it was cut else None)."""
    wrap = props.get("wrap")
    wrap = wrap if wrap in ("word", "char", "none") else "word"
    explicit_wrap = props.get("wrap") is not None
    truncate = props.get("truncate")
    where = truncate if truncate in ("end", "start", "middle") else "end"
    count = _int_prop(props.get("lines"))
    count = count if count and count > 0 else None
    width = max(4, _avail(ctx))
    if props.get("measure") == "prose":
        width = min(width, 80)
    logical = _split_lines_trailing(runs)
    one_line = (wrap == "none" or count == 1
                or (truncate is not None and not explicit_wrap and count is None))
    if one_line:
        if count == 1 and wrap != "none":
            joined = []
            for index, line in enumerate(logical):
                if index:
                    joined.append(_Run(" "))
                joined.extend(line)
            logical = [joined]
        lines, cut = [], False
        for line in logical:
            line, here = _truncate_line(line, width, where)
            lines.append(line)
            cut = cut or here
        full = _plain_runs(runs) if cut else None
        return "<br>".join(_runs_html(line, ctx, True) for line in lines), full
    if count is not None:
        visual = []
        for line in logical:
            visual.extend(_wrap_line(line, width, wrap))
        visual, cut = _clamp(visual, count, width)
        full = _plain_runs(runs) if cut else None
        return "<br>".join(_runs_html(line, ctx, True) for line in visual), full
    if wrap == "char" or props.get("measure") == "prose":
        visual = []
        for line in logical:
            visual.extend(_wrap_line(line, width, wrap))
        return "<br>".join(_runs_html(line, ctx, True) for line in visual), None
    return "<br>".join(_runs_html(line, ctx) for line in logical), None


def _split_lines_trailing(runs):
    lines = _split_logical(runs)
    return lines if lines else [[]]


# ---------------------------------------------------------------------------------------------
# syntax highlighting (a small dependency-free tokenizer)
# ---------------------------------------------------------------------------------------------

def _words(text):
    return frozenset(text.split())


_C_KW = ("if else for while do switch case default break continue return goto sizeof typedef struct union enum "
         "static extern const volatile register inline void auto signed unsigned true false NULL nullptr")
_C_TYPES = ("int char short long float double bool size_t ssize_t uint8_t uint16_t uint32_t uint64_t int8_t int16_t "
            "int32_t int64_t FILE wchar_t")

_SPECS = {
    "python": dict(
        kw=_words("False None True and as assert async await break class continue def del elif else except finally "
                  "for from global if import in is lambda nonlocal not or pass raise return try while with yield "
                  "match case self cls"),
        types=_words("int str float bool list dict set tuple bytes object type Exception"),
        line=("#",), triple=True, strings="\"'", prefix=True, extra=[(r"@[A-Za-z_][\w.]*", "type")], caps=True),
    "js": dict(
        kw=_words("break case catch class const continue debugger default delete do else export extends finally for "
                  "function if import in instanceof new return super switch this throw try typeof var let void while "
                  "with yield async await of static get set true false null undefined NaN Infinity from as"),
        types=_words("Array Object String Number Boolean Promise Map Set Date RegExp Error Symbol JSON Math"),
        line=("//",), block=(("/*", "*/"),), strings="\"'`", caps=True),
    "ts": dict(
        kw=_words("break case catch class const continue debugger default delete do else export extends finally for "
                  "function if import in instanceof new return super switch this throw try typeof var let void while "
                  "with yield async await of static get set true false null undefined NaN Infinity from as type "
                  "interface enum implements namespace declare abstract readonly private public protected keyof "
                  "infer is satisfies override module"),
        types=_words("string number boolean any unknown never object symbol bigint Array Promise Record Partial "
                     "Readonly Map Set Date Error"),
        line=("//",), block=(("/*", "*/"),), strings="\"'`", caps=True),
    "rust": dict(
        kw=_words("as async await break const continue crate dyn else enum extern false fn for if impl in let loop "
                  "match mod move mut pub ref return self Self static struct super trait true type unsafe use where "
                  "while macro_rules"),
        types=_words("i8 i16 i32 i64 i128 isize u8 u16 u32 u64 u128 usize f32 f64 bool char str String Vec Option "
                     "Result Box Rc Arc Some None Ok Err"),
        line=("//",), block=(("/*", "*/"),), strings="\"'", char_quote=True, caps=True,
        extra=[(r"\b[A-Za-z_]\w*!(?=\s*[(\[{])", "function"), (r"#!?\[[^\]\n]*\]", "type")]),
    "go": dict(
        kw=_words("break case chan const continue default defer else fallthrough for func go goto if import "
                  "interface map package range return select struct switch type var true false nil iota"),
        types=_words("int int8 int16 int32 int64 uint uint8 uint16 uint32 uint64 uintptr float32 float64 complex64 "
                     "complex128 bool byte rune string error any"),
        line=("//",), block=(("/*", "*/"),), strings="\"'`", caps=False),
    "c": dict(kw=_words(_C_KW), types=_words(_C_TYPES), line=("//",), block=(("/*", "*/"),), strings="\"'",
              extra=[(r"^[ \t]*#[ \t]*\w+", "keyword")]),
    "cpp": dict(
        kw=_words(_C_KW + " class namespace template typename using new delete this public private protected "
                  "virtual override final explicit constexpr noexcept try catch throw operator friend mutable "
                  "static_cast dynamic_cast reinterpret_cast const_cast decltype co_await co_return co_yield "
                  "concept requires"),
        types=_words(_C_TYPES + " string vector map set unordered_map unique_ptr shared_ptr auto"),
        line=("//",), block=(("/*", "*/"),), strings="\"'", extra=[(r"^[ \t]*#[ \t]*\w+", "keyword")], caps=True),
    "java": dict(
        kw=_words("abstract assert break case catch class const continue default do else enum extends final finally "
                  "for goto if implements import instanceof interface native new package private protected public "
                  "return static strictfp super switch synchronized this throw throws transient try void volatile "
                  "while true false null var record sealed permits yield"),
        types=_words("int long short byte float double boolean char String Object Integer List Map Set"),
        line=("//",), block=(("/*", "*/"),), strings="\"'", caps=True, extra=[(r"@[A-Za-z_]\w*", "type")]),
    "cs": dict(
        kw=_words("abstract as base break case catch checked class const continue default delegate do else enum event "
                  "explicit extern finally fixed for foreach goto if implicit in interface internal is lock namespace "
                  "new operator out override params private protected public readonly ref return sealed sizeof "
                  "stackalloc static struct switch this throw try typeof unchecked unsafe using virtual void volatile "
                  "while async await var true false null record init get set yield"),
        types=_words("int long short byte sbyte uint ulong ushort float double decimal bool char string object dynamic"),
        line=("//",), block=(("/*", "*/"),), strings="\"'", caps=True, extra=[(r"^[ \t]*#[ \t]*\w+", "keyword")]),
    "kotlin": dict(
        kw=_words("as break class continue do else false for fun if in interface is null object package return super "
                  "this throw true try typealias val var when while by catch constructor delegate dynamic field file "
                  "finally get import init param property receiver set setparam value where abstract actual annotation "
                  "companion const crossinline data enum expect external final infix inline inner internal lateinit "
                  "noinline open operator out override private protected public reified sealed suspend tailrec vararg"),
        types=_words("Int Long Short Byte Float Double Boolean Char String Any Unit Nothing List Map Set Array"),
        line=("//",), block=(("/*", "*/"),), strings="\"'", caps=True, extra=[(r"@[A-Za-z_]\w*", "type")]),
    "swift": dict(
        kw=_words("associatedtype class deinit enum extension fileprivate func import init inout internal let open "
                  "operator private protocol public rethrows static struct subscript typealias var break case catch "
                  "continue default defer do else fallthrough for guard if in repeat return throw switch where while "
                  "as Any false is nil super self Self throws true try async await actor some any"),
        types=_words("Int Double Float Bool String Character Array Dictionary Set Optional Void"),
        line=("//",), block=(("/*", "*/"),), strings="\"", caps=True, extra=[(r"@[A-Za-z_]\w*", "type")]),
    "php": dict(
        kw=_words("abstract and array as break callable case catch class clone const continue declare default do echo "
                  "else elseif empty enddeclare endfor endforeach endif endswitch endwhile extends final finally fn for "
                  "foreach function global goto if implements include include_once instanceof insteadof interface isset "
                  "list match namespace new or print private protected public readonly require require_once return "
                  "static switch throw trait try unset use var while xor yield true false null self parent"),
        types=_words("int float string bool array object mixed void iterable callable"),
        line=("//", "#"), block=(("/*", "*/"),), strings="\"'", caps=True,
        extra=[(r"\$[A-Za-z_]\w*", "variable"), (r"<\?php|\?>", "keyword")]),
    "ruby": dict(
        kw=_words("alias and begin BEGIN break case class def defined? do else elsif end END ensure false for if in "
                  "module next nil not or redo rescue retry return self super then true undef unless until when while "
                  "yield require require_relative include extend attr_accessor attr_reader attr_writer puts"),
        types=_words("String Integer Float Array Hash Symbol Object"),
        line=("#",), strings="\"'`", caps=True,
        extra=[(r":[A-Za-z_]\w*[?!]?", "number"), (r"@{1,2}[A-Za-z_]\w*|\$[A-Za-z_]\w*", "variable")]),
    "lua": dict(
        kw=_words("and break do else elseif end false for function goto if in local nil not or repeat return then "
                  "true until while self"),
        types=_words("string table math io os coroutine"),
        line=("--",), block=(("--[[", "]]"),), strings="\"'", long_string=True),
    "sh": dict(
        kw=_words("if then else elif fi case esac for select while until do done in function time coproc return exit "
                  "break continue local export readonly declare typeset unset set shift source alias eval exec trap "
                  "echo cd test true false"),
        types=frozenset(), line=("#",), strings="\"'", extra=[(r"\$\{[^}\n]*\}|\$[A-Za-z_]\w*|\$[0-9@#?*!$-]", "variable")]),
    "powershell": dict(
        kw=_words("begin break catch class continue data do dynamicparam else elseif end exit filter finally for "
                  "foreach from function if in param process return switch throw trap try until using var while "
                  "workflow $true $false $null"),
        types=frozenset(), line=("#",), block=(("<#", "#>"),), strings="\"'", ci=True,
        extra=[(r"\$\{[^}\n]*\}|\$[A-Za-z_]\w*", "variable"), (r"\b[A-Z][a-z]+-[A-Z][A-Za-z]+\b", "function")]),
    "sql": dict(
        kw=_words("select from where and or not in is null like between join inner left right outer full cross on as "
                  "group by order having limit offset union all distinct insert into values update set delete create "
                  "table index view drop alter add column primary key foreign references unique default check "
                  "constraint if exists case when then else end begin commit rollback transaction with asc desc "
                  "true false"),
        types=_words("int integer bigint smallint text varchar char boolean bool date time timestamp numeric decimal "
                     "float real double serial uuid json jsonb blob"),
        line=("--",), block=(("/*", "*/"),), strings="'\"`", ci=True),
    "json": dict(
        kw=_words("true false null"), types=frozenset(), strings="\"",
        extra=[(r"\"(?:\\.|[^\"\\\n])*\"(?=\s*:)", "variable")], first_extra=True, line=("//",), block=(("/*", "*/"),)),
    "yaml": dict(
        kw=_words("true false null yes no on off True False Null ~"), types=frozenset(), line=("#",), strings="\"'",
        extra=[(r"[\w.\-/]+(?=[ \t]*:(?:[ \t]|$))", "variable"), (r"[&*]\w+", "type"), (r"^---|^\.\.\.", "comment")]),
    "toml": dict(
        kw=_words("true false"), types=frozenset(), line=("#", ), strings="\"'", triple=True,
        extra=[(r"^[ \t]*\[\[?[^\]\n]+\]\]?", "type"), (r"^[ \t]*[\w.\-\"]+(?=[ \t]*=)", "variable")]),
    "ini": dict(
        kw=_words("true false on off yes no"), types=frozenset(), line=("#", ";"), strings="\"'",
        extra=[(r"^[ \t]*\[[^\]\n]+\]", "type"), (r"^[ \t]*[\w.\-]+(?=[ \t]*[=:])", "variable")]),
    "html": dict(
        kw=frozenset(), types=frozenset(), strings="\"'", block=(("<!--", "-->"),), no_ops=True,
        extra=[(r"<![A-Za-z][^>\n]*>", "keyword"), (r"</?[A-Za-z][\w:.-]*", "keyword"), (r"/?>", "punct"),
               (r"[A-Za-z_:@][\w:.-]*(?=\s*=)", "function"), (r"&#?\w+;", "type")]),
    "css": dict(
        kw=frozenset(), types=frozenset(), strings="\"'", block=(("/*", "*/"),), css=True,
        extra=[(r"@[\w-]+", "keyword"), (r"#[\da-fA-F]{3,8}\b", "number"),
               (r"-?\d*\.?\d+(?:px|em|rem|%|vh|vw|vmin|vmax|ch|pt|cm|mm|in|s|ms|deg|fr)?\b", "number"),
               (r"--[\w-]+|\$[\w-]+", "variable"), (r"[\w-]+(?=\s*:(?!:))", "function"), (r"[.#][A-Za-z_][\w-]*", "type")]),
    "md": dict(
        kw=frozenset(), types=frozenset(), strings="", no_ops=True,
        extra=[(r"^```.*$", "comment"), (r"^#{1,6}[ \t].*$", "keyword"), (r"`[^`\n]+`", "string"),
               (r"\[[^\]\n]*\]\([^)\n]*\)", "function"), (r"\*\*[^*\n]+\*\*", "type"), (r"^[ \t]*(?:[-*+]|\d+\.)[ \t]", "operator"),
               (r"^>.*$", "comment")]),
}

_ALIASES = {
    "py": "python", "python3": "python", "pyi": "python", "pyw": "python", "py3": "python",
    "javascript": "js", "jsx": "js", "mjs": "js", "cjs": "js", "node": "js", "es6": "js",
    "typescript": "ts", "tsx": "ts", "mts": "ts", "cts": "ts",
    "rs": "rust", "golang": "go", "h": "c", "cc": "cpp", "cxx": "cpp", "hpp": "cpp", "hh": "cpp", "hxx": "cpp",
    "c++": "cpp", "csharp": "cs", "c#": "cs", "kt": "kotlin", "kts": "kotlin", "rb": "ruby", "rake": "ruby",
    "bash": "sh", "zsh": "sh", "shell": "sh", "console": "sh", "shellscript": "sh", "fish": "sh", "ksh": "sh",
    "ps1": "powershell", "pwsh": "powershell", "psm1": "powershell", "yml": "yaml", "jsonc": "json", "json5": "json",
    "htm": "html", "xhtml": "html", "xml": "html", "svg": "html", "vue": "html", "svelte": "html", "plist": "html",
    "scss": "css", "sass": "css", "less": "css", "markdown": "md", "mdx": "md", "patch": "diff", "udiff": "diff",
    "conf": "ini", "cfg": "ini", "properties": "ini", "toml": "toml", "mysql": "sql", "pgsql": "sql", "sqlite": "sql",
    "plpgsql": "sql", "phtml": "php", "lua": "lua",
}

_NAMES = {"Dockerfile": "sh", "Makefile": "sh", "makefile": "sh", "Gemfile": "ruby", "Rakefile": "ruby"}

_COMPILED = {}


def canonical_lang(lang):
    """The grammar name for a language name or file extension, or None for plain text."""
    if not isinstance(lang, str) or not lang.strip():
        return None
    name = lang.strip().lower().lstrip(".")
    name = _ALIASES.get(name, name)
    return name if name in _SPECS or name == "diff" else None


def lang_for_path(path):
    if not isinstance(path, str) or not path:
        return None
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    if base in _NAMES:
        return _NAMES[base]
    if "." not in base:
        return None
    return canonical_lang(base.rsplit(".", 1)[-1])


def _compile(name):
    if name in _COMPILED:
        return _COMPILED[name]
    spec = _SPECS[name]
    parts = []
    extras = []
    for index, (pattern, cls) in enumerate(spec.get("extra", ())):
        extras.append("(?P<x%d_%s>%s)" % (index, cls, pattern))
    comments = []
    for opener, closer in spec.get("block", ()):
        comments.append("%s[\\s\\S]*?(?:%s|\\Z)" % (re.escape(opener), re.escape(closer)))
    for marker in spec.get("line", ()):
        comments.append("%s[^\\n]*" % re.escape(marker))
    strings = []
    quotes = spec.get("strings", "")
    prefix = "(?:\\b[rRbBfFuU]{1,2})?" if spec.get("prefix") else ""
    if spec.get("triple"):
        strings.append(prefix + '"""[\\s\\S]*?(?:"""|\\Z)')
        strings.append(prefix + "'''[\\s\\S]*?(?:'''|\\Z)")
    if spec.get("long_string"):
        strings.append("\\[\\[[\\s\\S]*?(?:\\]\\]|\\Z)")
    for quote in quotes:
        if quote == "`":
            strings.append("`(?:\\\\[\\s\\S]|[^`\\\\])*`?")
        elif quote == "'" and spec.get("char_quote"):
            strings.append("b?'(?:\\\\[^'\\n]+|[^'\\\\\\n])'")
        else:
            strings.append(prefix + "%s(?:\\\\.|[^%s\\\\\\n])*%s?" % (quote, quote, quote))
    if spec.get("first_extra"):
        parts.extend(extras)
    if comments:
        parts.append("(?P<cm>%s)" % "|".join(comments))
    if not spec.get("first_extra"):
        parts.extend(extras)
    if strings:
        parts.append("(?P<st>%s)" % "|".join(strings))
    if not spec.get("css"):
        parts.append("(?P<nm>\\b(?:0[xX][\\da-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\\d[\\d_]*(?:\\.[\\d_]+)?(?:[eE][+-]?\\d+)?)[A-Za-z0-9]*)")
    parts.append("(?P<fn>[A-Za-z_$][\\w$]*(?=\\s*\\())")
    parts.append("(?P<id>[A-Za-z_$][\\w$]*)")
    if not spec.get("no_ops"):
        parts.append("(?P<op>[-+*/%=<>!&|^~?:]+)")
        parts.append("(?P<pu>[()\\[\\]{},;.])")
    flags = re.M | (re.I if spec.get("ci") else 0)
    _COMPILED[name] = re.compile("|".join(parts), flags)
    return _COMPILED[name]


def _classify_ident(spec, text):
    key = text.lower() if spec.get("ci") else text
    if key in spec["kw"]:
        return "keyword"
    if key in spec["types"]:
        return "type"
    if spec.get("caps") and text[0].isupper() and not text.isupper() and len(text) > 1:
        return "type"
    return None


@functools.lru_cache(maxsize=96)
def highlight(text, lang):
    """``text`` as a tuple of lines, each a tuple of (token class or None, text).  Unknown language: plain lines."""
    name = canonical_lang(lang) if lang else None
    if name is None:
        return tuple(((None, line),) if line else () for line in text.split("\n"))
    if name == "diff":
        out = []
        for line in text.split("\n"):
            cls = ("inserted" if line.startswith("+") and not line.startswith("+++")
                   else "deleted" if line.startswith("-") and not line.startswith("---")
                   else "keyword" if line.startswith("@@") else "comment" if line.startswith(("diff ", "index ", "+++", "---"))
                   else None)
            out.append(((cls, line),) if line else ())
        return tuple(out)
    regex = _compile(name)
    spec = _SPECS[name]
    segments, pos = [], 0
    for match in regex.finditer(text):
        if match.end() == match.start():
            continue
        if match.start() > pos:
            segments.append((None, text[pos:match.start()]))
        group = match.lastgroup
        value = match.group(0)
        if group == "cm":
            cls = "comment"
        elif group == "st":
            cls = "string"
        elif group == "nm":
            cls = "number"
        elif group == "fn":
            cls = _classify_ident(spec, value) or "function"
            if cls == "type" and spec.get("caps") and value not in spec["types"]:
                cls = "function" if not value[0].isupper() else "type"
        elif group == "id":
            cls = _classify_ident(spec, value)
        elif group == "op":
            cls = "operator"
        elif group == "pu":
            cls = "punct"
        else:
            cls = group.split("_", 1)[1]
        segments.append((cls, value))
        pos = match.end()
    if pos < len(text):
        segments.append((None, text[pos:]))
    lines, current = [], []
    for cls, value in segments:
        parts = value.split("\n")
        for index, part in enumerate(parts):
            if index:
                lines.append(tuple(current))
                current = []
            if part:
                current.append((cls, part))
    lines.append(tuple(current))
    return tuple(lines)


def _tk_color(cls, ctx):
    if cls is None:
        return None
    if cls == "comment":
        return _col(ctx, "syntaxComment", _col(ctx, "dim"))
    if cls == "keyword":
        return _tok(ctx, "syntaxKeyword") or _tok(ctx, "info") or "#c586c0"
    if cls == "function":
        return _col(ctx, "syntaxFunction", _col(ctx, "accent"))
    if cls == "variable":
        return _col(ctx, "syntaxVariable", _text_color(ctx))
    if cls == "string":
        return _col(ctx, "syntaxString", _col(ctx, "success"))
    if cls == "number":
        return _col(ctx, "syntaxNumber", _col(ctx, "warning"))
    if cls == "type":
        return _tok(ctx, "syntaxType") or "#4ec9b0"
    if cls == "operator":
        return _col(ctx, "syntaxOperator", _col(ctx, "muted"))
    if cls == "punct":
        return _col(ctx, "syntaxPunctuation", _col(ctx, "muted"))
    if cls == "inserted":
        return _col(ctx, "toolDiffAdded", _col(ctx, "success"))
    if cls == "deleted":
        return _col(ctx, "toolDiffRemoved", _col(ctx, "error"))
    return None


def _highlight_runs(text, lang, ctx):
    """``text`` as a list of lines, each a list of coloured runs."""
    return [[_Run(piece, color=_tk_color(cls, ctx)) for cls, piece in line] for line in highlight(text, lang)]


def _apply_ranges(runs, ranges, bg):
    """Give the characters inside ``ranges`` ([(start, end)] character offsets) a background."""
    if not ranges:
        return runs
    out, pos = [], 0
    for run in runs:
        size = len(run.text)
        cuts = {0, size}
        for start, end in ranges:
            for point in (start - pos, end - pos):
                if 0 < point < size:
                    cuts.add(point)
        points = sorted(cuts)
        for a, b in zip(points, points[1:]):
            piece = run.clone(run.text[a:b])
            if any(start <= pos + a and pos + b <= end for start, end in ranges):
                piece.bg = bg
            out.append(piece)
        pos += size
    return out


def _merge_runs(runs):
    out = []
    for run in runs:
        if out and out[-1].color == run.color and out[-1].bg == run.bg and out[-1].css == run.css and not run.href:
            out[-1] = out[-1].clone(out[-1].text + run.text)
        else:
            out.append(run)
    return out


def _cells_of(runs):
    return _cells(_line_text(runs))


# ---------------------------------------------------------------------------------------------
# code
# ---------------------------------------------------------------------------------------------

_TONES = ("neutral", "accent", "info", "success", "warning", "error", "pending", "muted", "user")


def _tone_color(ctx, tone):
    if tone == "neutral":
        return _col(ctx, "muted")
    if tone == "pending":
        return _col(ctx, "dim")
    if tone == "user":
        return _col(ctx, "accent")
    if tone == "info":
        return _info_color(ctx)
    return _col(ctx, tone, _col(ctx, "accent"))


def _byte_to_char(line, offset):
    if line.isascii():
        return offset
    return len(line.encode("utf-8")[:offset].decode("utf-8", "ignore"))


def _code_marks(marks):
    """{line number: (tone, [(start, end) byte ranges])} from the ``marks`` prop."""
    out = {}
    for mark in marks if isinstance(marks, list) else []:
        if not isinstance(mark, dict):
            continue
        number = _int_prop(mark.get("line"))
        if number is None:
            continue
        tone = mark.get("tone") if mark.get("tone") in _TONES else "accent"
        ranges = []
        for pair in mark.get("ranges") if isinstance(mark.get("ranges"), list) else []:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2 and all(_int_prop(v) is not None for v in pair[:2]):
                ranges.append((int(pair[0]), int(pair[1])))
        out[number] = (tone, ranges)
    return out


def _path_header(path, ctx):
    slash = path.replace("\\", "/").rfind("/")
    directory, name = path[:slash + 1], path[slash + 1:]
    return ('<div style="padding-bottom: 3px; margin-bottom: 4px; border-bottom: 1px solid %s" title="%s">'
            '<span style="color: %s">%s</span><span style="color: %s">%s</span>'
            '<span style="color: %s">%s</span></div>'
            % (_col(ctx, "mdCodeBlockBorder"), _attr(path), _col(ctx, "dim"), _e("▤ "), _col(ctx, "dim"),
               _nbsp(_e(directory)), _text_color(ctx), _nbsp(_e(name))))


def code_block_html(text, ctx, lang=None, path=None, numbers=False, start=1, marks=None, wrap=False, framed=True):
    """A highlighted code block: optional file header, line numbers, marked lines, wrapping."""
    text = text if isinstance(text, str) else ""
    grammar = canonical_lang(lang) if lang else (lang_for_path(path) if path else None)
    lines = _highlight_runs(text.rstrip("\n") if text.endswith("\n") else text, grammar, ctx)
    start = _int_prop(start)
    start = 1 if start is None else start
    mark_map = _code_marks(marks)
    width_digits = len(str(max(start + len(lines) - 1, 1))) if numbers else 0
    avail = max(10, _avail(ctx) - 4 - (width_digits + 2 if numbers else 0))
    block_bg = _col(ctx, "pageBg")
    out = []
    for index, runs in enumerate(lines):
        number = start + index
        tone, ranges = mark_map.get(number, (None, []))
        if ranges and tone:
            plain = _line_text(runs)
            char_ranges = [(_byte_to_char(plain, a), _byte_to_char(plain, b)) for a, b in ranges if b > a]
            runs = _apply_ranges(runs, char_ranges, _tint(_tone_color(ctx, tone), 0.38))
        visual = _wrap_line(runs, avail, "char") if wrap else [runs]
        for sub, piece in enumerate(visual):
            gutter = ""
            if numbers:
                label = str(number) if sub == 0 else ""
                gutter = '<span style="color: %s">%s</span>&nbsp;&nbsp;' % (
                    _col(ctx, "dim"), _nbsp(label.rjust(width_digits)))
            body = _runs_html(_merge_runs(piece), ctx, True)
            if tone:
                color = _tone_color(ctx, tone)
                style = "background-color: %s; border-left: 3px solid %s; padding-left: 4px" % (_tint(color, 0.14), color)
            elif mark_map:
                style = "border-left: 3px solid %s; padding-left: 4px" % block_bg
            else:
                style = ""
            out.append('<div%s><tt>%s%s</tt></div>' % (' style="%s"' % style if style else "", gutter, body or "&nbsp;"))
    unframed = getattr(ctx, "unframed", False)
    header = _path_header(path, ctx) if path and not unframed else ""
    inner = header + "".join(out)
    if unframed and framed:
        return "<div>%s</div>" % inner         # inside a tool: no head, no frame
    if not framed:
        return inner
    return ('<div style="background-color: %s; border: 1px solid %s; border-radius: 6px; padding: 6px 10px; margin: 4px 0; color: %s">%s</div>'
            % (block_bg, _col(ctx, "mdCodeBlockBorder"), _col(ctx, "mdCodeBlock"), inner))


def _draw_code(node, ctx):
    props = node.get("p") or {}
    text = props.get("text")
    numbers = bool(props.get("numbers"))
    path = props.get("path") if isinstance(props.get("path"), str) and props.get("path") else None
    return code_block_html(text if isinstance(text, str) else "", ctx, props.get("lang"), path, numbers,
                           props.get("start") if props.get("start") is not None else 1, props.get("marks"),
                           bool(props.get("wrap")))


# ---------------------------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------------------------

_HUNK_HEAD = re.compile(r"^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@+ ?(.*)$")


class _Hunk(object):
    def __init__(self, old_start, new_start, head=""):
        self.old_start, self.new_start, self.head = old_start, new_start, head
        self.rows = []              # (kind, text): kind is "ctx", "add" or "del"
        self.known = True           # the start numbers came from a header

    def finish(self):
        old_no, new_no, rows = self.old_start, self.new_start, []
        for kind, text in self.rows:
            if kind == "ctx":
                rows.append((kind, text, old_no, new_no))
                old_no += 1
                new_no += 1
            elif kind == "del":
                rows.append((kind, text, old_no, None))
                old_no += 1
            else:
                rows.append((kind, text, None, new_no))
                new_no += 1
        self.numbered = rows
        self.old_end, self.new_end = old_no, new_no


def _add_line(hunk, line):
    if line.startswith("\\"):
        return
    if line.startswith("+"):
        hunk.rows.append(("add", line[1:]))
    elif line.startswith("-"):
        hunk.rows.append(("del", line[1:]))
    elif line.startswith(" "):
        hunk.rows.append(("ctx", line[1:]))
    else:
        hunk.rows.append(("ctx", line))


def _parse_diff(props):
    """(hunks, file path named by headers or None)."""
    given = props.get("hunks")
    hunks, path = [], None
    if isinstance(given, list):
        for item in given:
            if not isinstance(item, dict):
                continue
            old, new = _int_prop(item.get("oldStart")), _int_prop(item.get("newStart"))
            hunk = _Hunk(old if old is not None else 1, new if new is not None else 1)
            hunk.known = old is not None or new is not None
            for line in item.get("lines") if isinstance(item.get("lines"), list) else []:
                if isinstance(line, str):
                    _add_line(hunk, line)
            hunks.append(hunk)
    else:
        text = props.get("text")
        lines = (text if isinstance(text, str) else "").replace("\r\n", "\n").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        current, old_left, new_left = None, 0, 0
        loose = [line for line in lines if _HUNK_HEAD.match(line)] == []
        if loose:
            current = _Hunk(1, 1)
            current.known = False
            hunks.append(current)
            for line in lines:
                if line.startswith(("diff --git", "index ", "--- ", "+++ ")) and not hunks[0].rows:
                    if line.startswith("+++ "):
                        path = _header_path(line[4:])
                    continue
                _add_line(current, line)
        else:
            for line in lines:
                head = _HUNK_HEAD.match(line)
                if head:
                    old_len = 1 if head.group(2) is None else int(head.group(2))
                    new_len = 1 if head.group(4) is None else int(head.group(4))
                    current = _Hunk(int(head.group(1)), int(head.group(3)), head.group(5))
                    old_left, new_left = old_len, new_len
                    hunks.append(current)
                    continue
                if current is not None and (old_left > 0 or new_left > 0):
                    _add_line(current, line)
                    if line.startswith("+"):
                        new_left -= 1
                    elif line.startswith("-"):
                        old_left -= 1
                    elif not line.startswith("\\"):
                        old_left -= 1
                        new_left -= 1
                    continue
                if line.startswith("+++ "):
                    path = _header_path(line[4:])
                elif line.startswith("diff --git"):
                    parts = line.split(" b/", 1)
                    if len(parts) == 2:
                        path = parts[1].strip()
                current = None
    for hunk in hunks:
        hunk.finish()
    return hunks, path


def _header_path(text):
    text = text.strip().split("\t")[0]
    if text == "/dev/null":
        return None
    return text[2:] if text.startswith(("a/", "b/")) else text


_WORD = re.compile(r"\w+|\s+|[^\w\s]")


def _word_ranges(old, new):
    """Character ranges of the words that differ between two lines: ([old ranges], [new ranges]) or None."""
    if len(old.encode("utf-8")) > 1000 or len(new.encode("utf-8")) > 1000:
        return None
    a, b = _WORD.findall(old), _WORD.findall(new)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if matcher.ratio() < 0.4:
        return None                     # mostly rewritten: tinting every word says nothing
    offsets_a, offsets_b = [0], [0]
    for token in a:
        offsets_a.append(offsets_a[-1] + len(token))
    for token in b:
        offsets_b.append(offsets_b[-1] + len(token))
    left, right = [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            left.append((offsets_a[i1], offsets_a[i2]))
        if j2 > j1:
            right.append((offsets_b[j1], offsets_b[j2]))
    return left, right


def _guide_runs(runs, color):
    """Replace the leading white space of a line by faint indent guides when it is 2 or more cells deep."""
    text = _line_text(runs)
    stripped = text.lstrip(" \t")
    lead = text[:len(text) - len(stripped)]
    cells = sum(4 if ch == "\t" else 1 for ch in lead)
    if cells < 2 or not stripped:
        return runs
    unit = 4 if ("\t" in lead or cells % 4 == 0) else 2
    guide = "".join("│" if k % unit == 0 else " " for k in range(cells))
    return [_Run(guide, color=color)] + _cut_line(runs, len(lead), len(text))


def _diff_rows(hunk, ctx, lang, ok, bad):
    """The rows of one hunk as (kind, old no, new no, runs): syntax colours and word emphasis applied."""
    old_text = "\n".join(text for kind, text, o, n in hunk.numbered if kind != "add")
    new_text = "\n".join(text for kind, text, o, n in hunk.numbered if kind != "del")
    old_lines = _highlight_runs(old_text, lang, ctx)
    new_lines = _highlight_runs(new_text, lang, ctx)
    old_at = new_at = 0
    cooked = []
    for kind, text, old_no, new_no in hunk.numbered:
        if kind == "add":
            runs = new_lines[new_at]
            new_at += 1
        elif kind == "del":
            runs = old_lines[old_at]
            old_at += 1
        else:
            runs = new_lines[new_at]
            old_at += 1
            new_at += 1
        if not lang:
            tone = ok if kind == "add" else bad if kind == "del" else _col(ctx, "toolDiffContext")
            runs = [_Run(text, color=tone)] if text else []
        cooked.append([kind, old_no, new_no, runs, text])
    # word-level emphasis between the i-th removed and the i-th added line of one change
    index = 0
    while index < len(cooked):
        if cooked[index][0] != "del":
            index += 1
            continue
        end = index
        while end < len(cooked) and cooked[end][0] == "del":
            end += 1
        stop = end
        while stop < len(cooked) and cooked[stop][0] == "add":
            stop += 1
        for k in range(min(end - index, stop - end)):
            old_row, new_row = cooked[index + k], cooked[end + k]
            ranges = _word_ranges(old_row[4], new_row[4])
            if ranges:
                old_row[3] = _apply_ranges(old_row[3], ranges[0], _tint(bad, 0.34))
                new_row[3] = _apply_ranges(new_row[3], ranges[1], _tint(ok, 0.34))
        index = stop if stop > end else end
    return [(row[0], row[1], row[2], row[3]) for row in cooked]


def _draw_diff(node, ctx):
    props = node.get("p") or {}
    hunks, header_path = _parse_diff(props)
    if not any(hunk.rows for hunk in hunks):
        return '<div style="color: %s; margin: 4px 0">No changes</div>' % _col(ctx, "dim")
    path = props.get("path") if isinstance(props.get("path"), str) else header_path
    lang = canonical_lang(props.get("lang")) if props.get("lang") else lang_for_path(path)
    ok = _col(ctx, "toolDiffAdded", _col(ctx, "success"))
    bad = _col(ctx, "toolDiffRemoved", _col(ctx, "error"))
    mode = props.get("mode")
    split = mode == "split"
    highest = max([row[2] or 0 for hunk in hunks for row in hunk.numbered] + [row[3] or 0 for hunk in hunks for row in hunk.numbered] + [1])
    gw = len(str(highest))
    guide_color = _mix(_col(ctx, "dim"), _page_bg(ctx), 0.55)
    out = []
    for position, hunk in enumerate(hunks):
        if position:
            previous = hunks[position - 1]
            gap = hunk.old_start - previous.old_end if hunk.known and previous.known else 0
            label = "⋯ %d unchanged line%s" % (gap, "" if gap == 1 else "s") if gap > 0 else "⋯"
            function = ('&nbsp;&nbsp;<span style="color: %s">%s</span>' % (_col(ctx, "muted"), _nbsp(_e(hunk.head)))
                        if hunk.head else "")
            out.append('<div style="color: %s; padding: 2px 8px; margin: 2px 0; background-color: %s"><tt>%s%s</tt></div>'
                       % (_col(ctx, "dim"), _tint(_col(ctx, "dim"), 0.10), _nbsp(_e(label)), function))
        rows = _diff_rows(hunk, ctx, lang, ok, bad)
        out.extend(_diff_split(rows, ctx, gw, ok, bad, guide_color) if split else _diff_unified(rows, ctx, gw, ok, bad, guide_color))
    return '<div style="margin: %s">%s</div>' % ("0" if getattr(ctx, "unframed", False) else "4px 0", "".join(out))


def _diff_prepare(kind, runs, guide_color):
    if kind != "ctx":
        runs = _guide_runs(runs, guide_color)
    return runs


def _gutter(number, gw, ctx):
    return '<span style="color: %s">%s</span>' % (_col(ctx, "dim"), _nbsp((str(number) if number is not None else "").rjust(gw)))


def _marker(kind, ok, bad, ctx):
    if kind == "add":
        return '<span style="color: %s">+</span>' % ok
    if kind == "del":
        return '<span style="color: %s">-</span>' % bad
    return "&nbsp;"


def _diff_unified(rows, ctx, gw, ok, bad, guide_color):
    out = []
    avail = max(10, _avail(ctx) - 2 * gw - 6)
    for kind, old_no, new_no, runs in rows:
        runs = _diff_prepare(kind, runs, guide_color)
        visual = _wrap_line(runs, avail, "char")
        bg = _tint(ok, 0.13) if kind == "add" else _tint(bad, 0.13) if kind == "del" else None
        for sub, piece in enumerate(visual):
            head = "%s&nbsp;%s&nbsp;%s&nbsp;" % (
                _gutter(old_no if sub == 0 else None, gw, ctx), _gutter(new_no if sub == 0 else None, gw, ctx),
                _marker(kind, ok, bad, ctx) if sub == 0 else "&nbsp;")
            style = ' style="background-color: %s; padding: 0 4px"' % bg if bg else ' style="padding: 0 4px"'
            out.append("<div%s><tt>%s%s</tt></div>" % (style, head, _runs_html(_merge_runs(piece), ctx, True) or "&nbsp;"))
    return out


def _diff_split(rows, ctx, gw, ok, bad, guide_color):
    total = max(24, _avail(ctx) - 2)
    half = (total - 3) // 2
    text_width = max(8, half - gw - 3)
    shade = _tint(_col(ctx, "dim"), 0.08)
    # pair each removed line with the added line of the same position inside one change
    paired, index = [], 0
    while index < len(rows):
        kind = rows[index][0]
        if kind == "ctx":
            paired.append((rows[index], rows[index]))
            index += 1
            continue
        dels = []
        while index < len(rows) and rows[index][0] == "del":
            dels.append(rows[index])
            index += 1
        adds = []
        while index < len(rows) and rows[index][0] == "add":
            adds.append(rows[index])
            index += 1
        for k in range(max(len(dels), len(adds))):
            paired.append((dels[k] if k < len(dels) else None, adds[k] if k < len(adds) else None))
    out = []
    for left, right in paired:
        sides = []
        for row, tint in ((left, bad), (right, ok)):
            if row is None:
                sides.append((None, None, shade))
                continue
            kind = row[0]
            runs = _wrap_line(_diff_prepare(kind, row[3], guide_color), text_width, "char")
            bg = _tint(tint, 0.13) if kind != "ctx" else None
            sides.append((row, runs, bg))
        count = max(len(side[1]) if side[1] else 1 for side in sides)
        for k in range(count):
            cells = []
            for side, (row, runs, bg) in enumerate(sides):
                if row is None:
                    cells.append('<span style="background-color: %s">%s</span>' % (bg, "&nbsp;" * half))
                    continue
                piece = runs[k] if k < len(runs) else []
                number = (row[1] if side == 0 else row[2]) if k == 0 else None
                body = "%s&nbsp;%s&nbsp;%s" % (_gutter(number, gw, ctx),
                                               _marker(row[0], ok, bad, ctx) if k == 0 else "&nbsp;", _runs_html(_merge_runs(piece), ctx, True))
                pad = max(0, half - gw - 2 - _cells_of(piece))
                style = ' style="background-color: %s"' % bg if bg else ""
                cells.append("<span%s>%s%s</span>" % (style, body, "&nbsp;" * pad))
            out.append('<div><tt>%s<span style="color: %s">&nbsp;│&nbsp;</span>%s</tt></div>' % (
                cells[0], _col(ctx, "dim"), cells[1]))
    return out


# ---------------------------------------------------------------------------------------------
# ansi and rows: a small terminal
# ---------------------------------------------------------------------------------------------

_ANSI_TOKEN = re.compile(
    r"\x1b\[([0-?]*)[ -/]*([@-~])"            # CSI
    r"|\x1b\](.*?)(?:\x07|\x1b\\)"            # OSC
    r"|\x1b[()*+][0-9A-Za-z]"                 # charset
    r"|\x1b[@-Z\\-_0-9=>]"                    # other two-character escapes
    r"|([^\x00-\x1f\x7f\x1b]+)"               # printable text
    r"|([\x00-\x1f\x7f])",                    # one control character
    re.S)

_DEFAULT_ATTR = (None, None, False, False, False, False, False, False, False)
# attr = (fg, bg, bold, dim, italic, underline, inverse, strike, hidden); fg/bg: None, "#rrggbb" or 0..15 index


def _xterm_color(index):
    if index < 16:
        return index
    if index < 232:
        index -= 16
        steps = (0, 95, 135, 175, 215, 255)
        return "#%02x%02x%02x" % (steps[index // 36], steps[(index // 6) % 6], steps[index % 6])
    level = 8 + (index - 232) * 10
    return "#%02x%02x%02x" % (level, level, level)


class _Terminal(object):
    """Enough of a terminal for program output: SGR, OSC 8 links, CR/BS/TAB, cursor moves, erase, auto wrap."""

    def __init__(self, cols):
        self.cols = max(8, cols)
        self.lines = [[]]            # each line: list of cells (char, attr, link)
        self.row = 0
        self.col = 0
        self.attr = _DEFAULT_ATTR
        self.link = None
        self.saved = (0, 0)
        self.wrap_pending = False

    # -- cells -------------------------------------------------------------------------------
    def _line(self, row):
        while len(self.lines) <= row:
            self.lines.append([])
        return self.lines[row]

    def _put(self, ch):
        width = _cw(ch)
        if width == 0:
            line = self._line(self.row)
            if self.col > 0 and self.col - 1 < len(line):
                last = line[self.col - 1]
                line[self.col - 1] = (last[0] + ch, last[1], last[2])
            return
        if self.wrap_pending or self.col + width > self.cols:
            self.row += 1
            self.col = 0
            self.wrap_pending = False
        line = self._line(self.row)
        while len(line) < self.col:
            line.append((" ", _DEFAULT_ATTR, None))
        cell = (ch, self.attr, self.link)
        if self.col < len(line):
            line[self.col] = cell
        else:
            line.append(cell)
        if width == 2:
            if self.col + 1 < len(line):
                line[self.col + 1] = ("", self.attr, self.link)
            else:
                line.append(("", self.attr, self.link))
        self.col += width
        if self.col >= self.cols:
            self.col = self.cols
            self.wrap_pending = True

    def _blank(self):
        return (" ", self.attr, None)

    # -- input -------------------------------------------------------------------------------
    def feed(self, text):
        for match in _ANSI_TOKEN.finditer(text):
            params, final, osc, printable, control = match.groups()
            if printable is not None:
                for ch in printable:
                    self._put(ch)
            elif control is not None:
                self._control(control)
            elif final is not None:
                self._csi(params, final)
            elif osc is not None:
                self._osc(osc)

    def _control(self, ch):
        if ch == "\n":
            self.row += 1
            self.col = 0
            self.wrap_pending = False
            self._line(self.row)
        elif ch == "\r":
            self.col = 0
            self.wrap_pending = False
        elif ch == "\b":
            self.col = max(0, self.col - 1)
            self.wrap_pending = False
        elif ch == "\t":
            self.wrap_pending = False
            target = min(self.cols - 1, (self.col // 8 + 1) * 8)
            line = self._line(self.row)
            while self.col < target:
                if self.col >= len(line):
                    line.append((" ", self.attr, None))
                self.col += 1

    def _osc(self, body):
        parts = body.split(";", 2)
        if parts[0] == "8" and len(parts) == 3:
            self.link = parts[2] or None

    def _csi(self, params, final):
        if params.startswith(("?", ">", "<", "=")):
            return
        numbers = [int(p) if p.isdigit() else None for p in params.split(";")] if params else []
        first = numbers[0] if numbers and numbers[0] is not None else None
        one = first if first else 1
        self.wrap_pending = False
        if final == "m":
            self._sgr(params)
        elif final == "A":
            self.row = max(0, self.row - one)
        elif final == "B" or final == "e":
            self.row += one
            self._line(self.row)
        elif final == "C" or final == "a":
            self.col = min(self.cols - 1, self.col + one)
        elif final == "D":
            self.col = max(0, self.col - one)
        elif final == "E":
            self.row += one
            self.col = 0
            self._line(self.row)
        elif final == "F":
            self.row = max(0, self.row - one)
            self.col = 0
        elif final in ("G", "`"):
            self.col = min(self.cols - 1, one - 1)
        elif final == "d":
            self.row = max(0, one - 1)
            self._line(self.row)
        elif final in ("H", "f"):
            row = numbers[0] if numbers and numbers[0] else 1
            col = numbers[1] if len(numbers) > 1 and numbers[1] else 1
            self.row = max(0, row - 1)
            self.col = min(self.cols - 1, col - 1)
            self._line(self.row)
        elif final == "K":
            line = self._line(self.row)
            mode = first or 0
            if mode == 0:
                del line[self.col:]
            elif mode == 1:
                for index in range(min(self.col + 1, len(line))):
                    line[index] = self._blank()
            else:
                del line[:]
        elif final == "J":
            mode = first or 0
            if mode == 0:
                del self._line(self.row)[self.col:]
                del self.lines[self.row + 1:]
            elif mode == 1:
                for r in range(self.row):
                    self.lines[r] = []
                line = self._line(self.row)
                for index in range(min(self.col + 1, len(line))):
                    line[index] = self._blank()
            else:
                self.lines = [[]]
                self.row = 0
        elif final == "X":
            line = self._line(self.row)
            for index in range(self.col, min(self.col + one, len(line))):
                line[index] = self._blank()
        elif final == "P":
            del self._line(self.row)[self.col:self.col + one]
        elif final == "@":
            line = self._line(self.row)
            for _ in range(one):
                line.insert(self.col, self._blank())
            del line[self.cols:]
        elif final == "L":
            for _ in range(one):
                self.lines.insert(self.row, [])
        elif final == "M":
            del self.lines[self.row:self.row + one]
            self._line(self.row)
        elif final == "s":
            self.saved = (self.row, self.col)
        elif final == "u":
            self.row, self.col = self.saved
            self._line(self.row)

    def _sgr(self, params):
        fg, bg, bold, dim, italic, underline, inverse, strike, hidden = self.attr
        items = params.split(";") if params else ["0"]
        i = 0
        while i < len(items):
            item = items[i]
            if ":" in item:                                     # 38:2::r:g:b / 38:5:n / 4:3
                sub = item.split(":")
                head = sub[0]
                if head in ("38", "48") and len(sub) >= 3:
                    value = None
                    if sub[1] == "2" and len(sub) >= 5:
                        rgb = [int(x) if x.isdigit() else 0 for x in sub[-3:]]
                        value = "#%02x%02x%02x" % tuple(min(255, v) for v in rgb)
                    elif sub[1] == "5" and sub[2].isdigit():
                        value = _xterm_color(min(255, int(sub[2])))
                    if value is not None:
                        if head == "38":
                            fg = value
                        else:
                            bg = value
                elif head == "4":
                    underline = len(sub) < 2 or sub[1] != "0"
                i += 1
                continue
            code = int(item) if item.isdigit() else 0
            if code == 0:
                fg, bg, bold, dim, italic, underline, inverse, strike, hidden = _DEFAULT_ATTR
            elif code == 1:
                bold = True
            elif code == 2:
                dim = True
            elif code == 3:
                italic = True
            elif code in (4, 21):
                underline = True
            elif code == 7:
                inverse = True
            elif code == 8:
                hidden = True
            elif code == 9:
                strike = True
            elif code == 22:
                bold = dim = False
            elif code == 23:
                italic = False
            elif code == 24:
                underline = False
            elif code == 27:
                inverse = False
            elif code == 28:
                hidden = False
            elif code == 29:
                strike = False
            elif 30 <= code <= 37:
                fg = code - 30
            elif 90 <= code <= 97:
                fg = code - 90 + 8
            elif code == 39:
                fg = None
            elif 40 <= code <= 47:
                bg = code - 40
            elif 100 <= code <= 107:
                bg = code - 100 + 8
            elif code == 49:
                bg = None
            elif code in (38, 48):
                value = None
                if i + 1 < len(items) and items[i + 1] == "5" and i + 2 < len(items) and items[i + 2].isdigit():
                    value = _xterm_color(min(255, int(items[i + 2])))
                    i += 2
                elif i + 1 < len(items) and items[i + 1] == "2" and i + 4 < len(items):
                    rgb = [int(x) if x.isdigit() else 0 for x in items[i + 2:i + 5]]
                    value = "#%02x%02x%02x" % tuple(min(255, v) for v in rgb)
                    i += 4
                else:
                    i += 1
                if value is not None:
                    if code == 38:
                        fg = value
                    else:
                        bg = value
            i += 1
        self.attr = (fg, bg, bold, dim, italic, underline, inverse, strike, hidden)


def _terminal_lines(term, ctx, fade=None):
    """A terminal's lines as HTML (one string per line), trailing blanks dropped, in the terminal's colours."""
    palette = list(tsp_style._ANSI)      # the theme's `ansiN` tokens below override these defaults
    for index in range(16):
        override = _tok(ctx, "ansi%d" % index)
        if override:
            palette[index] = override
    default_fg = _col(ctx, "toolOutput", _col(ctx, "muted"))
    page = _page_bg(ctx)

    def resolve(value):
        return palette[value] if isinstance(value, int) else value

    def style_of(attr):
        fg, bg, bold, dim, italic, underline, inverse, strike, hidden = attr
        fg_c = resolve(fg) if fg is not None else None
        bg_c = resolve(bg) if bg is not None else None
        if inverse:
            fg_c, bg_c = bg_c or page, fg_c or default_fg
        if hidden:
            fg_c = bg_c or page
        if dim and not hidden:
            fg_c = _mix(fg_c or default_fg, bg_c or page, 0.5)
        if fade and fg_c is None:
            fg_c = default_fg
        if fade and fg_c:
            fg_c = _mix(fg_c, page, fade)
        style = []
        if fg_c:
            style.append("color: %s" % fg_c)
        if bg_c:
            style.append("background-color: %s" % bg_c)
        if bold:
            style.append("font-weight: bold")
        if italic:
            style.append("font-style: italic")
        decoration = " ".join(name for name, on in (("underline", underline), ("line-through", strike)) if on)
        if decoration:
            style.append("text-decoration: " + decoration)
        return "; ".join(style)

    out = []
    for line in term.lines:
        cells = list(line)
        while cells and cells[-1][0] in (" ", "") and cells[-1][1][1] is None and not cells[-1][1][6] and cells[-1][2] is None:
            cells.pop()
        pieces, i = [], 0
        while i < len(cells):
            attr, link = cells[i][1], cells[i][2]
            j = i
            chunk = []
            while j < len(cells) and cells[j][1] == attr and cells[j][2] == link:
                chunk.append(cells[j][0])
                j += 1
            body = _nbsp(_e("".join(chunk)))
            style = style_of(attr)
            piece = '<span style="%s">%s</span>' % (style, body) if style else body
            if link:
                piece = '<a href="%s" title="%s" style="color: %s; text-decoration: underline">%s</a>' % (
                    _open_href(link), _attr(link), _col(ctx, "mdLink", _col(ctx, "accent")), piece)
            pieces.append(piece)
            i = j
        out.append("".join(pieces))
    return out


def _terminal_for(text, cols):
    term = _Terminal(cols)
    term.feed(text.replace("\r\n", "\n"))
    # a trailing newline leaves an empty last line that is not output
    if len(term.lines) > 1 and not term.lines[-1] and term.row == len(term.lines) - 1 and term.col == 0:
        term.lines.pop()
    return term


def _lines_prop(value):
    """"10lines" -> 10 (a text-line bound); anything else -> None."""
    if isinstance(value, str):
        match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*lines?\s*$", value)
        if match:
            return int(float(match.group(1)))
    return None


def _draw_ansi(node, ctx):
    props = node.get("p") or {}
    text = props.get("text")
    text = text if isinstance(text, str) else ""
    node_id = node.get("id")
    width = max(8, _avail(ctx))
    cols = _int_prop(props.get("cols"))
    if cols and cols > 0:
        width = min(width, cols)
    term = _terminal_for(text, width)
    follow = bool(props.get("follow"))
    total = len(term.lines)
    limit = props.get("max") if isinstance(props.get("max"), dict) else {}
    bound = _lines_prop(limit.get("h"))
    preview = props.get("preview") if isinstance(props.get("preview"), dict) else None
    keep = _int_prop(preview.get("lines")) if preview else None
    gives_way = getattr(tsp_html, "preview_gives_way", None)
    if keep and gives_way is not None and gives_way(ctx):
        keep = None                 # an open folding ancestor shows everything
    color = _col(ctx, "toolOutput", _col(ctx, "muted"))

    start, end, more = 0, total, ""
    fade_last = False
    if keep and keep > 0 and total > keep:
        key = "more:%s" % node_id
        expanded = ctx.local.get(key) == keep           # a new preview value resets the expansion
        hidden = total - keep
        if expanded:
            more = _local_button(ctx, key, None, "Show less ▴")
        else:
            if follow:
                start, end = total - keep, total
            else:
                start, end = 0, keep
            fade_last = True
            more = _local_button(ctx, key, keep, "%d more line%s ▾" % (hidden, "" if hidden == 1 else "s"))
    elif bound and total > bound:
        key = "scroll:%s" % node_id
        stored = ctx.local.get(key)
        first = total - bound if (stored is None and follow) else (stored if isinstance(stored, int) else 0)
        first = max(0, min(first, total - bound))
        start, end = first, first + bound
        ctx.local["scrollinfo:%s" % node_id] = {"total": total, "shown": bound, "first": first, "follow": follow}
    if bound and total <= bound:
        ctx.local["scrollinfo:%s" % node_id] = {"total": total, "shown": total, "first": 0, "follow": follow}
    lines = _terminal_lines(term, ctx)
    shown = lines[start:end]
    if fade_last and shown:
        faded = _terminal_lines(term, ctx, fade=0.55)
        shown[-1] = faded[end - 1]
        if len(shown) > 1:
            shown[-2] = _terminal_lines(term, ctx, fade=0.2)[end - 2]
    notes = ""
    out = []
    if bound and start > 0:
        out.append('<div style="color: %s"><tt>%s</tt></div>' % (_col(ctx, "dim"), _nbsp(_e("⋮ %d more line%s above" % (start, "" if start == 1 else "s")))))
    body = "<br>".join(line or "&nbsp;" for line in shown)
    out.append('<div style="color: %s"><tt>%s</tt></div>' % (color, body))
    if bound and end < total:
        out.append('<div style="color: %s"><tt>%s</tt></div>' % (_col(ctx, "dim"), _nbsp(_e("⋮ %d more line%s below" % (total - end, "" if total - end == 1 else "s")))))
    if more:
        out.append("<div>%s</div>" % more)
    return "<div>%s</div>" % ("".join(out) + notes)


def _local_button(ctx, key, value, label):
    event = {"local": "set", "key": key, "value": value}
    return '<a href="%s" style="color: %s; text-decoration: none">%s</a>' % (
        tsp_html.event_href(event), _col(ctx, "accent"), _nbsp(_e(label)))


def _draw_rows(node, ctx):
    props = node.get("p") or {}
    lines = [line for line in props.get("lines") if isinstance(line, str)] if isinstance(props.get("lines"), list) else []
    cols = _int_prop(props.get("cols"))
    cols = cols if cols and cols > 0 else 80
    rendered = []
    for line in lines:
        term = _Terminal(max(cols, 8))
        term.cols = cols
        term.feed(line.replace("\r\n", "\n").replace("\n", " "))
        rendered.append(_terminal_lines(term, ctx)[0] if term.lines else "")
    mark = ('<div style="color: %s; text-align: right" title="Fallback rows (%d)"><tt>fallback</tt></div>'
            % (_col(ctx, "dim"), len(lines)))
    body = "<br>".join(line or "&nbsp;" for line in rendered)
    return '<div>%s<div style="color: %s"><tt>%s</tt></div></div>' % (
        mark, _col(ctx, "toolOutput", _col(ctx, "muted")), body)


# ---------------------------------------------------------------------------------------------
# md
# ---------------------------------------------------------------------------------------------

_URL = r"(?:https?://|www\.)[^\s<>\"]+"
_EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
_PATHRE = (r"(?:\.{0,2}/|~/|[A-Za-z]:[\\/])?(?:[\w@+.-]+/)*[\w@+-][\w@+.-]*\.[A-Za-z][A-Za-z0-9]{0,7}"
           r"(?::\d+(?::\d+)?(?:-\d+)?|#L\d+(?:-L?\d+)?)?")
_AUTO = re.compile(r"(?P<url>(?<![\w/@])%s)|(?P<email>(?<![\w.@/-])%s)|(?P<path>(?<![\w/\\.@:~#-])%s(?![\w/-]))"
                   % (_URL, _EMAIL, _PATHRE))
_PATH_FULL = re.compile(r"^%s$" % _PATHRE)
_PATH_SPLIT = re.compile(r"^(.*?)(:\d+(?::\d+)?(?:-\d+)?|#L\d+(?:-L?\d+)?)?$", re.S)
_HEXCOLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_PUNCT = re.compile(r"[!-/:-@\[-`{-~\u2010-\u2027\u2030-\u205e]")
_PLAIN_CHARS = re.compile(r"[^\\`$!\[<*_~\n]+")
_SEVERITY = {"high": "error", "medium": "warning", "low": "success"}
_CALLOUTS = ("note", "tip", "important", "warning", "caution")
_MATH_ENV = re.compile(r"^\s*\\begin\{([A-Za-z*]+)\}")


def _visible_len(fragment):
    return _cells(html.unescape(re.sub(r"<[^>]*>", "", fragment)))


def _plain_of(fragment):
    return html.unescape(re.sub(r"<[^>]*>", "", fragment)).replace("\xa0", " ")


class _Md(object):
    """One markdown rendering: the link references, the theme and the options."""

    def __init__(self, ctx, stream=False, marks=None):
        self.ctx = ctx
        self.stream = stream
        self.refs = {}
        self.in_link = False
        self.marks = [span for span in marks if isinstance(span, dict) and isinstance(span.get("t"), str) and span.get("t")] \
            if isinstance(marks, list) else []

    # -- plain text with marks, bare URLs, e-mail and file paths --------------------------------
    def plain(self, raw):
        if "&" in raw:
            raw = html.unescape(raw)
        if self.in_link:
            return self._marked(raw)
        out, pos = [], 0
        for match in _AUTO.finditer(raw):
            kind = match.lastgroup
            value = match.group(0)
            if kind == "url":
                trimmed = value
                while trimmed and (trimmed[-1] in ".,;:!?\"'" or (trimmed[-1] == ")" and trimmed.count("(") < trimmed.count(")"))):
                    trimmed = trimmed[:-1]
                rest = value[len(trimmed):]
                href = trimmed if trimmed.startswith("http") else "https://" + trimmed
                out.append(self._marked(raw[pos:match.start()]))
                out.append(self._anchor(self._marked(trimmed), href))
                out.append(self._marked(rest))
            elif kind == "email":
                out.append(self._marked(raw[pos:match.start()]))
                out.append(self._anchor(self._marked(value), "mailto:" + value))
            else:
                split = _PATH_SPLIT.match(value)
                body, position = (split.group(1), split.group(2)) if split else (value, None)
                if not ("/" in body or "\\" in body or position):
                    continue
                out.append(self._marked(raw[pos:match.start()]))
                out.append(self.path_link(value))
            pos = match.end()
        out.append(self._marked(raw[pos:]))
        return "".join(out)

    def path_link(self, value):
        run = _Run(value, cls="path", css="font-weight: bold", href=value)
        return _run_html(run, self.ctx)

    def _marked(self, text):
        if not text:
            return ""
        if not self.marks:
            return _e(text)
        out = []
        while text:
            best = None
            for span in self.marks:
                at = text.find(span["t"])
                if at >= 0 and (best is None or at < best[0] or (at == best[0] and len(span["t"]) > len(best[1]["t"]))):
                    best = (at, span)
            if best is None:
                out.append(_e(text))
                break
            at, span = best
            out.append(_e(text[:at]))
            out.append(spans_html([span], self.ctx))
            text = text[at + len(span["t"]):]
        return "".join(out)

    def _anchor(self, inner, href):
        return '<a href="%s" title="%s" style="color: %s; text-decoration: none">%s</a>' % (
            _open_href(href), _attr(href), _col(self.ctx, "mdLink", _col(self.ctx, "accent")), inner)

    def code_span(self, content):
        ctx = self.ctx
        if _PATH_FULL.match(content):
            split = _PATH_SPLIT.match(content)
            body, position = (split.group(1), split.group(2)) if split else (content, None)
            if "/" in body or "\\" in body or position:
                return self.path_link(content)
        swatch = ""
        if _HEXCOLOR.match(content):
            swatch = ('<span style="background-color: %s; border: 1px solid %s; border-radius: 3px">&nbsp;&nbsp;&nbsp;</span>&nbsp;'
                      % (content, _col(ctx, "border")))
        return swatch + ('<span style="color: %s; background-color: #ffffff14; border-radius: 3px; padding: 0 3px"><tt>%s</tt></span>'
                         % (_col(ctx, "mdCode"), _e(content)))

    # -- inline ------------------------------------------------------------------------------------
    def inline(self, text):
        nodes = []          # ["h", html] or ["d", char, count, can_open, can_close, open_tags, close_tags]
        buffer = []

        def flush():
            if buffer:
                nodes.append(["h", self.plain("".join(buffer))])
                del buffer[:]

        i, size = 0, len(text)
        while i < size:
            ch = text[i]
            if ch not in "\\`$!<[*_~\n":
                match = _PLAIN_CHARS.match(text, i)
                buffer.append(match.group(0))
                i = match.end()
                continue
            if ch == "\\":
                if i + 1 < size and text[i + 1] == "\n":
                    flush()
                    nodes.append(["h", "<br>"])
                    i += 2
                    while i < size and text[i] == " ":
                        i += 1
                elif i + 1 < size and _PUNCT.match(text[i + 1]):
                    buffer.append(text[i + 1])
                    i += 2
                elif text.startswith("\\(", i):
                    end = text.find("\\)", i + 2)
                    if end > 0:
                        flush()
                        nodes.append(["h", math_html(text[i + 2:end], self.ctx, False)])
                        i = end + 2
                    else:
                        buffer.append("\\")
                        i += 1
                else:
                    buffer.append("\\")
                    i += 1
            elif ch == "\n":
                spaces = len("".join(buffer)) - len("".join(buffer).rstrip(" "))
                if spaces >= 2:
                    joined = "".join(buffer).rstrip(" ")
                    del buffer[:]
                    buffer.append(joined)
                    flush()
                    nodes.append(["h", "<br>"])
                else:
                    joined = "".join(buffer).rstrip(" ")
                    del buffer[:]
                    buffer.append(joined + " ")
                i += 1
                while i < size and text[i] == " ":
                    i += 1
            elif ch == "`":
                run = 1
                while i + run < size and text[i + run] == "`":
                    run += 1
                close = self._find_backticks(text, i + run, run)
                if close < 0:
                    buffer.append("`" * run)
                    i += run
                else:
                    content = text[i + run:close].replace("\n", " ")
                    if len(content) > 2 and content[0] == " " and content[-1] == " " and content.strip():
                        content = content[1:-1]
                    flush()
                    nodes.append(["h", self.code_span(content)])
                    i = close + run
            elif ch == "$":
                done = self._math_at(text, i, nodes, flush)
                if done:
                    i = done
                else:
                    buffer.append("$")
                    i += 1
            elif ch == "!" and text.startswith("![", i):
                link = self._link(text, i + 1)
                if link:
                    label, dest, title, end = link
                    flush()
                    nodes.append(["h", self._image(label, dest)])
                    i = end
                else:
                    buffer.append("!")
                    i += 1
            elif ch == "!":
                buffer.append("!")
                i += 1
            elif ch == "[":
                link = None if self.in_link else self._link(text, i)
                if link:
                    label, dest, title, end = link
                    flush()
                    self.in_link = True
                    try:
                        inner = self.inline(label)
                    finally:
                        self.in_link = False
                    nodes.append(["h", self._anchor(inner, dest)])
                    i = end
                else:
                    buffer.append("[")
                    i += 1
            elif ch == "<":
                match = re.match(r"<((?:https?|mailto|ftp):[^\s<>]+|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)>", text[i:])
                if match:
                    flush()
                    target = match.group(1)
                    href = target if ":" in target.split("@")[0] or target.startswith(("http", "mailto", "ftp")) else "mailto:" + target
                    nodes.append(["h", self._anchor(_e(target), href)])
                    i += match.end()
                else:
                    buffer.append("<")
                    i += 1
            else:                                   # * _ ~
                run = 1
                while i + run < size and text[i + run] == ch:
                    run += 1
                before = text[i - 1] if i else " "
                after = text[i + run] if i + run < size else " "
                if ch == "~" and run != 2:
                    buffer.append(ch * run)
                    i += run
                    continue
                before_space, after_space = before.isspace(), after.isspace()
                before_punct, after_punct = bool(_PUNCT.match(before)), bool(_PUNCT.match(after))
                left = not after_space and (not after_punct or before_space or before_punct)
                right = not before_space and (not before_punct or after_space or after_punct)
                if ch == "_":
                    can_open = left and (not right or before_punct)
                    can_close = right and (not left or after_punct)
                else:
                    can_open, can_close = left, right
                flush()
                nodes.append(["d", ch, run, can_open, can_close, [], []])
                i += run
        flush()
        return self._emphasis(nodes)

    @staticmethod
    def _find_backticks(text, start, run):
        i = start
        while True:
            at = text.find("`" * run, i)
            if at < 0:
                return -1
            end = at + run
            if (at == 0 or text[at - 1] != "`") and (end >= len(text) or text[end] != "`"):
                return at
            i = at + 1 if text[at + 1:at + 2] != "`" else end

    def _math_at(self, text, i, nodes, flush):
        """Inline math at ``text[i] == "$"``: returns the index after it, or 0."""
        if text.startswith("$$", i):
            end = text.find("$$", i + 2)
            if end > i + 2:
                flush()
                nodes.append(["h", math_html(text[i + 2:end], self.ctx, False)])
                return end + 2
            return 0
        if i + 1 >= len(text) or text[i + 1].isspace() or text[i + 1] == "$":
            return 0
        j = i + 1
        while True:
            j = text.find("$", j)
            if j < 0:
                return 0
            if text[j - 1] == "\\":
                j += 1
                continue
            break
        if text[j - 1].isspace() or (j + 1 < len(text) and text[j + 1].isdigit()):
            return 0
        flush()
        nodes.append(["h", math_html(text[i + 1:j].replace("\n", " "), self.ctx, False)])
        return j + 1

    def _link(self, text, i):
        """A link or image label at ``text[i] == "["``: (label, destination, title, end index) or None."""
        depth, j, size = 0, i, len(text)
        while j < size:
            ch = text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "`":
                run = 1
                while j + run < size and text[j + run] == "`":
                    run += 1
                close = self._find_backticks(text, j + run, run)
                j = close + run if close >= 0 else j + run
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        else:
            return None
        if j >= size:
            return None
        label = text[i + 1:j]
        rest = j + 1
        if rest < size and text[rest] == "(":
            parsed = self._destination(text, rest + 1)
            if parsed:
                dest, title, end = parsed
                return label, dest, title, end
        reference = None
        end = rest
        if rest < size and text[rest] == "[":
            close = text.find("]", rest + 1)
            if close >= 0:
                key = text[rest + 1:close] or label
                reference, end = self.refs.get(_ref_key(key)), close + 1
        elif _ref_key(label) in self.refs:
            reference = self.refs[_ref_key(label)]
        if reference:
            return label, reference[0], reference[1], end
        return None

    @staticmethod
    def _destination(text, i):
        size = len(text)
        while i < size and text[i] in " \n":
            i += 1
        if i < size and text[i] == "<":
            close = text.find(">", i)
            if close < 0:
                return None
            dest, i = text[i + 1:close], close + 1
        else:
            start, depth = i, 0
            while i < size:
                ch = text[i]
                if ch == "\\":
                    i += 2
                    continue
                if ch.isspace():
                    break
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        break
                    depth -= 1
                i += 1
            dest = text[start:i]
        while i < size and text[i] in " \n":
            i += 1
        title = None
        if i < size and text[i] in "\"'(":
            closer = ")" if text[i] == "(" else text[i]
            close = text.find(closer, i + 1)
            if close < 0:
                return None
            title, i = text[i + 1:close], close + 1
            while i < size and text[i] in " \n":
                i += 1
        if i < size and text[i] == ")":
            return re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", dest), title, i + 1
        return None

    def _image(self, label, dest):
        if dest.startswith("data:image/"):
            return '<img src="%s">' % _attr(dest)
        alt = _plain_of(self.inline(label)) or "image"
        return self._anchor('<span style="color: %s">%s</span>' % (_col(self.ctx, "dim"), _nbsp(_e("▧ " + alt))), dest)

    def _emphasis(self, nodes):
        """Pair emphasis delimiters (CommonMark's algorithm, simplified) and return the HTML."""
        for index, node in enumerate(nodes):
            if node[0] != "d" or not node[4]:
                continue
            while node[2] > 0 and node[4]:
                opener = None
                opener_at = -1
                for back in range(index - 1, -1, -1):
                    cand = nodes[back]
                    if cand[0] == "d" and cand[1] == node[1] and cand[3] and cand[2] > 0:
                        # the "multiple of three" rule
                        if (cand[4] or node[3]) and (cand[2] + node[2]) % 3 == 0 and not (cand[2] % 3 == 0 and node[2] % 3 == 0):
                            continue
                        opener = cand
                        opener_at = back
                        break
                if opener is None:
                    break
                if node[1] == "~":
                    take = 2
                    open_tag = '<span style="text-decoration: line-through">'
                    close_tag = "</span>"
                else:
                    take = 2 if opener[2] >= 2 and node[2] >= 2 else 1
                    open_tag, close_tag = ("<b>", "</b>") if take == 2 else ("<i>", "</i>")
                opener[2] -= take
                node[2] -= take
                opener[5].insert(0, open_tag)
                node[6].append(close_tag)
                # delimiters between opener and closer can no longer match
                for between in nodes[opener_at + 1:index]:
                    if between[0] == "d":
                        between[3] = between[4] = False
                if opener[2] == 0:
                    opener[3] = False
            node[3] = node[3] and node[2] > 0
        out = []
        for node in nodes:
            if node[0] == "h":
                out.append(node[1])
            else:
                literal = node[1] * node[2]
                out.append("".join(node[6]) + _e(literal) + "".join(node[5]))
        return "".join(out)

    # -- blocks ------------------------------------------------------------------------------------
    def blocks(self, lines):
        """Lines -> a list of {"k": kind, "h": html, "i": inline html for p/h}."""
        out = []
        i, size = 0, len(lines)
        ctx = self.ctx
        while i < size:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            fence = re.match(r"^( {0,3})(`{3,}|~{3,})\s*([^`\s]*)[^`]*$", line) if line.lstrip().startswith(("```", "~~~")) else None
            if fence:
                marker, info = fence.group(2), fence.group(3)
                body = []
                i += 1
                indent = len(fence.group(1))
                while i < size:
                    closing = re.match(r"^ {0,3}(%s{%d,})\s*$" % (re.escape(marker[0]), len(marker)), lines[i])
                    if closing:
                        i += 1
                        break
                    body.append(lines[i][indent:] if lines[i][:indent].strip() == "" else lines[i])
                    i += 1
                out.append({"k": "code", "h": self._fence(body, info)})
                continue
            heading = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$", line)
            if heading:
                out.append(self._heading(len(heading.group(1)), heading.group(2) or ""))
                i += 1
                continue
            if re.match(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$", line):
                out.append({"k": "hr", "h": '<div style="border-top: 1px solid %s; margin: 6px 0">&nbsp;</div>' % _col(ctx, "mdHr", _col(ctx, "border"))})
                i += 1
                continue
            math_block, i2 = self._math_block(lines, i)
            if math_block is not None:
                out.append({"k": "math", "h": math_block})
                i = i2
                continue
            definition = re.match(r"^ {0,3}\[([^\]]+)\]:\s*(\S+)(?:\s+[\"'(](.*)[\"')])?\s*$", line)
            if definition:
                self.refs.setdefault(_ref_key(definition.group(1)), (definition.group(2).strip("<>"), definition.group(3)))
                i += 1
                continue
            if line.lstrip().startswith(">") and len(line) - len(line.lstrip()) < 4:
                i = self._quote(lines, i, out)
                continue
            marker = _LIST_RE.match(line)
            if marker and len(marker.group(1)) < 4 and not (re.match(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$", line)):
                i = self._list(lines, i, out)
                continue
            if "|" in line and i + 1 < size and _TABLE_DELIM.match(lines[i + 1]):
                head = _split_row(line)
                delim = _split_row(lines[i + 1])
                if len(head) == len(delim):
                    j = i + 2
                    rows = []
                    while j < size and lines[j].strip() and "|" in lines[j]:
                        rows.append(_split_row(lines[j]))
                        j += 1
                    out.append({"k": "table", "h": self._table(head, delim, rows)})
                    i = j
                    continue
            if re.match(r"^ {0,3}<(?:[A-Za-z][\w-]*[\s/>]|[A-Za-z][\w-]*$|/[A-Za-z]|!--|\?)", line):
                block = []
                while i < size and lines[i].strip():
                    block.append(lines[i])
                    i += 1
                out.append({"k": "html", "h": '<div style="color: %s; margin: 3px 0"><tt>%s</tt></div>'
                            % (_col(ctx, "muted"), "<br>".join(_nbsp(_e(b)) for b in block))})
                continue
            # a paragraph (or a setext heading)
            para = [line.lstrip()]
            i += 1
            setext = None
            while i < size:
                nxt = lines[i]
                if not nxt.strip():
                    break
                under = re.match(r"^ {0,3}(=+|-+)[ \t]*$", nxt)
                if under:
                    setext = 1 if under.group(1)[0] == "=" else 2
                    i += 1
                    break
                if self._interrupts(nxt):
                    break
                para.append(nxt.lstrip())
                i += 1
            if setext:
                out.append(self._heading(setext, " ".join(part.strip() for part in para)))
            else:
                inner = self.inline("\n".join(para).rstrip())
                out.append({"k": "p", "i": inner, "h": '<div style="margin: 3px 0">%s</div>' % inner})
        return out

    def _interrupts(self, line):
        if re.match(r"^ {0,3}(#{1,6}([ \t]|$)|`{3,}|~{3,}|>)", line):
            return True
        if re.match(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$", line):
            return True
        marker = _LIST_RE.match(line)
        if marker and marker.group(4).strip() and (not marker.group(2)[0].isdigit() or marker.group(2).startswith("1")):
            return True
        return bool(re.match(r"^ {0,3}(\$\$|\\\[)", line))

    def _heading(self, level, text):
        ctx = self.ctx
        inner = self.inline(text)
        marker = '<span style="color: %s">%s&nbsp;</span>' % (_col(ctx, "dim"), "#" * level)
        return {"k": "h", "i": inner, "h": '<div style="color: %s; font-weight: bold; margin: 6px 0 2px 0">%s%s</div>'
                % (_col(ctx, "mdHeading"), marker, inner)}

    def _fence(self, body, info):
        ctx = self.ctx
        lang = info.split("{")[0].strip().lower() if info else ""
        text = "\n".join(body)
        if lang == "mermaid":
            first = next((line.split() for line in body if line.strip() and not line.strip().startswith("%%")), [])
            kind = first[0] if first else "diagram"
            head = '<div style="color: %s"><tt>%s</tt></div>' % (_col(ctx, "dim"), _nbsp(_e("⌬ mermaid · " + kind)))
            return ('<div style="background-color: %s; border: 1px solid %s; border-radius: 6px; padding: 6px 10px; margin: 4px 0; color: %s">%s%s</div>'
                    % (_col(ctx, "pageBg"), _col(ctx, "mdCodeBlockBorder"), _col(ctx, "mdCodeBlock"), head,
                       code_block_html(text, ctx, None, framed=False)))
        return code_block_html(text, ctx, lang or None)

    def _math_block(self, lines, i):
        """A display-math block starting at lines[i]: (html, next index) or (None, i)."""
        line = lines[i].strip()
        if line.startswith("$$"):
            rest = line[2:]
            if rest.rstrip().endswith("$$") and len(rest.rstrip()) >= 2 and rest.strip() != "":
                return math_html(rest.rstrip()[:-2], self.ctx, True), i + 1
            body = [rest] if rest.strip() else []
            j = i + 1
            while j < len(lines):
                if "$$" in lines[j]:
                    before = lines[j][:lines[j].index("$$")]
                    if before.strip():
                        body.append(before)
                    return math_html("\n".join(body), self.ctx, True), j + 1
                body.append(lines[j])
                j += 1
            return math_html("\n".join(body), self.ctx, True), j
        if line.startswith("\\["):
            rest = line[2:]
            if "\\]" in rest:
                return math_html(rest[:rest.index("\\]")], self.ctx, True), i + 1
            body = [rest] if rest.strip() else []
            j = i + 1
            while j < len(lines):
                if "\\]" in lines[j]:
                    body.append(lines[j][:lines[j].index("\\]")])
                    return math_html("\n".join(body), self.ctx, True), j + 1
                body.append(lines[j])
                j += 1
            return math_html("\n".join(body), self.ctx, True), j
        env = _MATH_ENV.match(line)
        if env:
            name = env.group(1)
            closing = "\\end{%s}" % name
            body = [lines[i]]
            j = i + 1
            if closing not in lines[i]:
                while j < len(lines):
                    body.append(lines[j])
                    if closing in lines[j]:
                        j += 1
                        break
                    j += 1
            else:
                j = i + 1
            return math_html("\n".join(body), self.ctx, True), j
        return None, i

    def _quote(self, lines, i, out):
        ctx = self.ctx
        gathered = []
        size = len(lines)
        while i < size:
            line = lines[i]
            if re.match(r"^ {0,3}>", line):
                gathered.append(re.sub(r"^ {0,3}> ?", "", line))
            elif line.strip() and gathered and gathered[-1].strip() and not self._interrupts(line) and not _LIST_RE.match(line):
                gathered.append(line)
            else:
                break
            i += 1
        callout = None
        match = re.match(r"^\s*\[!(\w+)\][ \t]*(.*)$", gathered[0]) if gathered else None
        if match and match.group(1).lower() in _CALLOUTS:
            callout = match.group(1).lower()
            gathered[0] = match.group(2)
        inner = self.blocks(gathered)
        body = "".join(block["h"] for block in inner)
        if callout:
            warn = callout in ("warning", "caution")
            color = _col(ctx, "warning") if warn else _col(ctx, "accent")
            icon = '<span style="color: %s">%s&nbsp;</span>' % (color, "⚠" if warn else "ⓘ")
            if inner and inner[0]["k"] == "p":
                body = '<div style="margin: 3px 0">%s%s</div>' % (icon, inner[0]["i"]) + "".join(b["h"] for b in inner[1:])
            else:
                body = "<div>%s</div>" % icon + body
            html_out = ('<div style="border-left: 3px solid %s; background-color: %s; padding: 3px 8px; margin: 4px 0">%s</div>'
                        % (color, _tint(color, 0.10), body))
            out.append({"k": "callout", "h": html_out})
        else:
            out.append({"k": "quote", "h": '<div style="border-left: 3px solid %s; padding-left: 8px; color: %s; margin: 3px 0">%s</div>'
                        % (_col(ctx, "mdQuoteBorder"), _col(ctx, "mdQuote"), body)})
        return i

    def _list(self, lines, i, out):
        size = len(lines)
        first = _LIST_RE.match(lines[i])
        ordered = first.group(2)[0].isdigit()
        delimiter = first.group(2)[-1] if ordered else first.group(2)
        number = int(first.group(2)[:-1]) if ordered else 0
        items = []
        while i < size:
            match = _LIST_RE.match(lines[i])
            if not match or len(match.group(1)) >= 4:
                break
            this_ordered = match.group(2)[0].isdigit()
            if this_ordered != ordered or (match.group(2)[-1] if this_ordered else match.group(2)) != delimiter:
                break
            spaces = len(match.group(3))
            rest = match.group(4)
            if not rest.strip() or spaces > 4:
                spaces = 1
            content_indent = len(match.group(1)) + len(match.group(2)) + spaces
            item = [rest]
            i += 1
            while i < size:
                line = lines[i]
                if not line.strip():
                    j = i + 1
                    while j < size and not lines[j].strip():
                        j += 1
                    if j < size and _indent_of(lines[j]) >= content_indent:
                        item.extend([""] * (j - i))
                        i = j
                        continue
                    break
                if _indent_of(line) >= content_indent:
                    item.append(line[content_indent:])
                    i += 1
                    continue
                if item and item[-1].strip() and not self._interrupts(line) and not _LIST_RE.match(line):
                    item.append(line.strip())
                    i += 1
                    continue
                break
            items.append(item)
            j = i
            while j < size and not lines[j].strip():
                j += 1
            if j > i and j < size and _LIST_RE.match(lines[j]) and len(_LIST_RE.match(lines[j]).group(1)) < 4:
                i = j
        rendered = []
        for index, item in enumerate(items):
            rendered.append(self._item(item, ordered, number + index))
        out.append({"k": "ol" if ordered else "ul", "h": '<div style="margin: 2px 0 2px 14px">%s</div>' % "".join(rendered)})
        return i

    def _item(self, lines, ordered, number):
        ctx = self.ctx
        task = None
        if lines:
            match = re.match(r"^\[([ xX])\][ \t]+(.*)$", lines[0])
            if match:
                task = match.group(1) in "xX"
                lines = [match.group(2)] + lines[1:]
        blocks = self.blocks(lines)
        bullet_color = _col(ctx, "mdListBullet", _col(ctx, "accent"))
        if task is not None:
            box = "☑" if task else "☐"
            bullet = '<span style="color: %s">%s&nbsp;</span>' % (_col(ctx, "success") if task else _col(ctx, "muted"), box)
        elif ordered:
            bullet = '<span style="color: %s">%d.&nbsp;</span>' % (bullet_color, number)
        else:
            bullet = '<span style="color: %s">•&nbsp;</span>' % bullet_color
        done = task is True
        parts = []
        rest = blocks
        if blocks and blocks[0]["k"] in ("p", "h") and blocks[0].get("i") is not None:
            text = blocks[0]["i"]
            if done:
                text = '<span style="color: %s; text-decoration: line-through">%s</span>' % (_col(ctx, "dim"), text)
            parts.append("<div>%s%s</div>" % (bullet, text))
            rest = blocks[1:]
        else:
            parts.append("<div>%s</div>" % bullet)
        if rest:
            parts.append('<div style="margin-left: 14px">%s</div>' % "".join(block["h"] for block in rest))
        return "".join(parts)

    def _table(self, head, delim, rows):
        ctx = self.ctx
        aligns = []
        for cell in delim:
            cell = cell.strip()
            aligns.append("center" if cell.startswith(":") and cell.endswith(":") else "right" if cell.endswith(":") else "left")
        count = len(head)
        grid = [[self._cell(c) for c in head]]
        for row in rows:
            row = (row + [""] * count)[:count]
            grid.append([self._cell(c) for c in row])
        widths = [max(_visible_len(r[k][0]) for r in grid) for k in range(count)]
        budget = max(count * 4, _avail(ctx) - 6 - 3 * (count - 1))
        while sum(widths) > budget:
            widest = max(range(count), key=lambda k: widths[k])
            if widths[widest] <= 4:
                break
            widths[widest] -= 1
        border = _col(ctx, "mdCodeBlockBorder")
        lines = []

        def line(cells, bold):
            parts = []
            for k, (fragment, plain) in enumerate(cells):
                width = widths[k]
                if _visible_len(fragment) > width:
                    fragment = _nbsp(_e(plain[:max(1, width - 1)] + "…"))
                    size = _cells(plain[:max(1, width - 1)] + "…")
                else:
                    size = _visible_len(fragment)
                pad = width - size
                if aligns[k] == "right":
                    left, right = pad, 0
                elif aligns[k] == "center":
                    left, right = pad // 2, pad - pad // 2
                else:
                    left, right = 0, pad
                body = "&nbsp;" * left + fragment + "&nbsp;" * right
                parts.append("<b>%s</b>" % body if bold else body)
            return '<div><tt>%s</tt></div>' % ('<span style="color: %s">&nbsp;│&nbsp;</span>' % border).join(parts)

        lines.append(line(grid[0], True))
        lines.append('<div style="color: %s"><tt>%s</tt></div>' % (border, "─┼─".join("─" * w for w in widths)))
        for row in grid[1:]:
            lines.append(line(row, False))
        return ('<div style="margin: 4px 0; padding: 4px 8px; background-color: %s; border-radius: 6px">%s</div>'
                % (_col(ctx, "pageBg"), "".join(lines)))

    def _cell(self, raw):
        raw = raw.strip()
        level = _SEVERITY.get(raw.lower())
        if level:
            color = _col(self.ctx, level)
            plain = "● " + raw
            return ('<span style="background-color: %s; color: %s; border-radius: 8px; padding: 0 4px">●&nbsp;%s</span>'
                    % (_tint(color, 0.16), color, _e(raw)), plain)
        fragment = self.inline(raw.replace("\\|", "|"))
        return _nbsp_html(fragment), _plain_of(fragment)


def _nbsp_html(fragment):
    """No-break spaces in the text parts of an HTML fragment (not inside tags)."""
    return re.sub(r"(<[^>]*>)|( )", lambda m: m.group(1) or "&nbsp;", fragment)


def _ref_key(label):
    return re.sub(r"\s+", " ", label.strip().lower())


def _indent_of(line):
    return len(line) - len(line.lstrip(" "))


_LIST_RE = re.compile(r"^( {0,3})([-+*]|\d{1,9}[.)])( +|$)(.*)$")
_TABLE_DELIM = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")


def _split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells, current, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == "|":
            current.append("\\|")
            i += 2
            continue
        if ch == "`":                       # a pipe inside a code span stays in the cell
            end = line.find("`", i + 1)
            if end > 0:
                current.append(line[i:end + 1])
                i = end + 1
                continue
        if ch == "|":
            cells.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    cells.append("".join(current))
    return cells


def md_html(text, ctx, stream=False, marks=None):
    """Markdown in omp's dialect as HTML: the whole dialect of el_text.md (``stream`` marks the tail provisional)."""
    state = _Md(ctx, stream, marks)
    lines = (text if isinstance(text, str) else "").replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n")
    # link reference definitions anywhere at the top level count before they are used
    fenced = False
    for line in lines:
        if re.match(r"^ {0,3}(`{3,}|~{3,})", line):
            fenced = not fenced
        if not fenced:
            definition = re.match(r"^ {0,3}\[([^\]]+)\]:\s*(\S+)(?:\s+[\"'(](.*)[\"')])?\s*$", line)
            if definition:
                state.refs.setdefault(_ref_key(definition.group(1)), (definition.group(2).strip("<>"), definition.group(3)))
    blocks = state.blocks(lines)
    parts = [block["h"] for block in blocks]
    if stream and blocks and blocks[-1]["k"] in ("p", "h", "ul", "ol", "quote", "callout"):
        parts[-1] = _with_caret(parts[-1], ctx)
    return "".join(parts)


def _with_caret(fragment, ctx):
    """Put the streaming caret at the end of the last text of ``fragment`` (before its closing divs)."""
    now = _now(ctx)
    color = _col(ctx, "accent")
    if now is not None:
        ctx.animated = True
        color = _mix(_page_bg(ctx), color, 0.35 + 0.65 * (0.5 + 0.5 * math.cos((now % 1.0) * 2 * math.pi)))
    caret = '<span style="color: %s">&nbsp;▍</span>' % color
    match = re.search(r"(?:</div>)+$", fragment)
    if not match:
        return fragment + caret
    return fragment[:match.start()] + caret + fragment[match.start():]


def _draw_md(node, ctx):
    props = node.get("p") or {}
    text = props.get("text")
    return "<div>%s</div>" % md_html(text if isinstance(text, str) else "", ctx, bool(props.get("stream")), props.get("marks"))


# ---------------------------------------------------------------------------------------------
# math: TeX typeset in character cells
# ---------------------------------------------------------------------------------------------
#
# A formula becomes a box of text rows with a baseline.  Display math stacks fractions, limits,
# roots and matrices over several rows; inline math is one row (fractions as a/b, scripts as
# Unicode super- and subscripts where the characters exist, else ^(...) and _(...)).

class _MathError(Exception):
    pass


_GREEK = {}
for _name, _char in zip("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau "
                        "upsilon phi chi psi omega".split(), "αβγδεζηθικλμνξοπρστυφχψω"):
    _GREEK[_name] = (_char, "ord")
for _name, _char in zip("Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega".split(), "ΓΔΘΛΞΠΣΥΦΨΩ"):
    _GREEK[_name] = (_char, "ord")
_GREEK.update({"varepsilon": ("ε", "ord"), "epsilon": ("ϵ", "ord"), "vartheta": ("ϑ", "ord"), "varpi": ("ϖ", "ord"),
               "varrho": ("ϱ", "ord"), "varsigma": ("ς", "ord"), "varphi": ("ϕ", "ord"), "varkappa": ("ϰ", "ord")})

_SYMBOLS = dict(_GREEK)
for _kind, _table in (
        ("bin", {"pm": "±", "mp": "∓", "times": "×", "div": "÷", "cdot": "·", "ast": "∗", "star": "⋆", "circ": "∘",
                 "bullet": "•", "cup": "∪", "cap": "∩", "wedge": "∧", "land": "∧", "vee": "∨", "lor": "∨",
                 "oplus": "⊕", "ominus": "⊖", "otimes": "⊗", "oslash": "⊘", "setminus": "∖", "uplus": "⊎",
                 "sqcup": "⊔", "sqcap": "⊓", "amalg": "⨿", "dagger": "†", "ddagger": "‡", "wr": "≀",
                 "triangleleft": "◁", "triangleright": "▷", "diamond": "⋄", "bigtriangleup": "△"}),
        ("rel", {"leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡",
                 "sim": "∼", "simeq": "≃", "cong": "≅", "propto": "∝", "ll": "≪", "gg": "≫", "in": "∈",
                 "notin": "∉", "ni": "∋", "subset": "⊂", "supset": "⊃", "subseteq": "⊆", "supseteq": "⊇",
                 "nsubseteq": "⊈", "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←",
                 "leftrightarrow": "↔", "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔", "iff": "⟺",
                 "implies": "⟹", "impliedby": "⟸", "mapsto": "↦", "longrightarrow": "⟶", "longleftarrow": "⟵",
                 "uparrow": "↑", "downarrow": "↓", "Uparrow": "⇑", "Downarrow": "⇓", "hookrightarrow": "↪",
                 "rightharpoonup": "⇀", "perp": "⊥", "parallel": "∥", "mid": "∣", "vdash": "⊢", "models": "⊨",
                 "prec": "≺", "succ": "≻", "preceq": "⪯", "succeq": "⪰", "asymp": "≍", "doteq": "≐",
                 "leadsto": "⇝", "nearrow": "↗", "searrow": "↘", "colon": ":", "coloneqq": "≔", "ncong": "≇",
                 "nmid": "∤", "sqsubseteq": "⊑", "sqsupseteq": "⊒", "bowtie": "⋈", "smile": "⌣", "frown": "⌢"}),
        ("ord", {"infty": "∞", "partial": "∂", "nabla": "∇", "forall": "∀", "exists": "∃", "nexists": "∄",
                 "neg": "¬", "lnot": "¬", "emptyset": "∅", "varnothing": "∅", "prime": "′", "angle": "∠",
                 "hbar": "ℏ", "ell": "ℓ", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "wp": "℘", "top": "⊤", "bot": "⊥",
                 "degree": "°", "surd": "√", "triangle": "△", "square": "□", "Box": "□", "Diamond": "◇",
                 "clubsuit": "♣", "diamondsuit": "♢", "heartsuit": "♡", "spadesuit": "♠", "flat": "♭",
                 "natural": "♮", "sharp": "♯", "imath": "ı", "jmath": "ȷ", "backslash": "\\", "lbrace": "{",
                 "rbrace": "}", "%": "%", "&": "&", "$": "$", "#": "#", "_": "_", "|": "‖", "S": "§", "P": "¶",
                 "copyright": "©", "checkmark": "✓", "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮",
                 "ddots": "⋱", "dotsc": "…", "dotsb": "⋯", "textbackslash": "\\", "lq": "‘", "rq": "’",
                 "ldotp": ".", "cdotp": "·", "mathbin": ""}),
        ("open", {"langle": "⟨", "lfloor": "⌊", "lceil": "⌈", "lbrack": "[", "lvert": "|", "lVert": "‖", "{": "{"}),
        ("close", {"rangle": "⟩", "rfloor": "⌋", "rceil": "⌉", "rbrack": "]", "rvert": "|", "rVert": "‖", "}": "}"}),
        ("op", {"sum": "Σ", "prod": "Π", "coprod": "∐", "int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮",
                "bigcup": "⋃", "bigcap": "⋂", "bigvee": "⋁", "bigwedge": "⋀", "bigoplus": "⨁", "bigotimes": "⨂",
                "biguplus": "⨄", "bigsqcup": "⨆"})):
    for _name, _text in _table.items():
        _SYMBOLS[_name] = (_text, _kind)

_SPACES = {",": " ", ";": " ", ":": " ", "!": "", " ": " ", "quad": "  ", "qquad": "    ", "enspace": " ",
           "thinspace": " ", "medspace": " ", "thickspace": " ", "negthinspace": ""}
_NAMED_OPS = _words("sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh coth log ln lg exp lim limsup liminf "
                    "max min sup inf det dim ker gcd lcm arg deg hom Pr mod bmod pmod sgn tr rank argmax argmin "
                    "Var Cov E")
_LIMIT_OPS = _words("lim limsup liminf max min sup inf det gcd Pr argmax argmin")
_LARGE_LIMITS = frozenset("ΣΠ∐⋃⋂⋁⋀⨁⨂⨄⨆")
_NOOPS = _words("displaystyle textstyle scriptstyle scriptscriptstyle limits nolimits nonumber notag allowbreak "
                "relax protect hfill vfill noindent centering")
_FONTS = _words("mathrm mathbf mathit mathsf mathtt mathnormal boldsymbol bm pmb mathbb mathcal mathscr mathfrak "
                "textrm textbf textit textsf texttt text mbox hbox operatorname mathop mathrel mathbin mathord "
                "mathpunct mathopen mathclose mathfrak mathbfit bf it rm sf tt")
_ACCENTS = {"hat": "\u0302", "widehat": "\u0302", "check": "\u030c", "tilde": "\u0303", "widetilde": "\u0303",
            "acute": "\u0301", "grave": "\u0300", "dot": "\u0307", "ddot": "\u0308", "dddot": "\u20db",
            "breve": "\u0306", "bar": "\u0304", "vec": "\u20d7", "mathring": "\u030a", "overline": "\u0305",
            "underline": "\u0332", "overrightarrow": "\u20d7", "overleftarrow": "\u20d6"}
_DELIMS = {"(": "(", ")": ")", "[": "[", "]": "]", "{": "{", "}": "}", "|": "|", "/": "/", "<": "⟨", ">": "⟩", ".": "",
           "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
           "vert": "|", "Vert": "‖", "lvert": "|", "rvert": "|", "lVert": "‖", "rVert": "‖", "backslash": "\\",
           "lbrace": "{", "rbrace": "}", "lbrack": "[", "rbrack": "]", "uparrow": "↑", "downarrow": "↓"}
_SIZES = _words("big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr bigm Bigm")
_MATRICES = {"matrix": ("", ""), "pmatrix": ("(", ")"), "bmatrix": ("[", "]"), "Bmatrix": ("{", "}"),
             "vmatrix": ("|", "|"), "Vmatrix": ("‖", "‖"), "smallmatrix": ("", ""), "array": ("", ""),
             "cases": ("{", ""), "rcases": ("", "}"), "dcases": ("{", "")}
_ALIGNED = _words("aligned align align* alignat alignat* gathered gather gather* split eqnarray eqnarray* multline "
                  "multline* flalign flalign*")
_SUP = dict(zip("0123456789+-−=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿⁱ"))
_SUP.update(zip("abcdefghjklmoprstuvwxyz", "ᵃᵇᶜᵈᵉᶠᵍʰʲᵏˡᵐᵒᵖʳˢᵗᵘᵛʷˣʸᶻ"))
_SUP.update(zip("ABDEGHIJKLMNOPRTUVW", "ᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ"))
_SUP.update({"β": "ᵝ", "γ": "ᵞ", "δ": "ᵟ", "θ": "ᶿ", "φ": "ᵠ", "χ": "ᵡ", "′": "′", "∗": "*", "*": "*", "∘": "°", "°": "°"})
_SUB = dict(zip("0123456789+-−=()", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎"))
_SUB.update(zip("aehijklmnoprstuvx", "ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))
_SUB.update({"β": "ᵦ", "γ": "ᵧ", "ρ": "ᵨ", "φ": "ᵩ", "χ": "ᵪ"})
_VULGAR = {("1", "2"): "½", ("1", "3"): "⅓", ("2", "3"): "⅔", ("1", "4"): "¼", ("3", "4"): "¾", ("1", "5"): "⅕",
           ("2", "5"): "⅖", ("3", "5"): "⅗", ("4", "5"): "⅘", ("1", "6"): "⅙", ("5", "6"): "⅚", ("1", "8"): "⅛",
           ("3", "8"): "⅜", ("5", "8"): "⅝", ("7", "8"): "⅞", ("1", "7"): "⅐", ("1", "9"): "⅑", ("1", "10"): "⅒"}


class _Box(object):
    """Rows of text of equal width; ``base`` is the row the line of text sits on."""

    __slots__ = ("lines", "base")

    def __init__(self, lines, base=0):
        width = max([len(line) for line in lines] + [0])
        self.lines = [line + " " * (width - len(line)) for line in lines]
        self.base = base

    @property
    def width(self):
        return len(self.lines[0]) if self.lines else 0

    @property
    def height(self):
        return len(self.lines)

    def text(self):
        return self.lines[0] if len(self.lines) == 1 else None


def _hcat(boxes):
    boxes = [b for b in boxes if b.width or b.height > 1]
    if not boxes:
        return _Box([""], 0)
    above = max(b.base for b in boxes)
    below = max(b.height - b.base - 1 for b in boxes)
    rows = [""] * (above + below + 1)
    for box in boxes:
        top = above - box.base
        for row in range(len(rows)):
            k = row - top
            rows[row] += box.lines[k] if 0 <= k < box.height else " " * box.width
    return _Box(rows, above)


def _pad(box, width, align="center"):
    extra = width - box.width
    if extra <= 0:
        return box
    left = extra // 2 if align == "center" else 0 if align == "left" else extra
    return _Box([" " * left + line + " " * (extra - left) for line in box.lines], box.base)


def _vstack(boxes, base_of=0, align="center"):
    width = max(b.width for b in boxes)
    rows, base = [], 0
    for index, box in enumerate(boxes):
        if index == base_of:
            base = len(rows) + box.base
        rows.extend(_pad(box, width, align).lines)
    return _Box(rows, base)


def _script_text(text, table):
    out = []
    for ch in text:
        if ch == " ":
            return None
        mapped = table.get(ch)
        if mapped is None:
            return None
        out.append(mapped)
    return "".join(out)


def _tall_delimiter(ch, height):
    """A delimiter ``height`` rows tall from the Unicode bracket pieces (or the plain one for one row)."""
    if height <= 1 or not ch:
        return [ch or ""]
    pieces = {"(": ("⎛", "⎜", "⎝"), ")": ("⎞", "⎟", "⎠"), "[": ("⎡", "⎢", "⎣"), "]": ("⎤", "⎥", "⎦"),
              "{": ("⎧", "⎪", "⎩"), "}": ("⎫", "⎪", "⎭"), "|": ("│", "│", "│"), "‖": ("║", "║", "║"),
              "⌊": ("⎢", "⎢", "⎣"), "⌋": ("⎥", "⎥", "⎦"), "⌈": ("⎡", "⎢", "⎢"), "⌉": ("⎤", "⎥", "⎥"),
              "⟨": ("╱", "│", "╲"), "⟩": ("╲", "│", "╱"), "/": ("╱", "╱", "╱"), "\\": ("╲", "╲", "╲")}
    top, mid, bottom = pieces.get(ch, (ch, ch, ch))
    if ch == "{" and height >= 3:
        rows = [top] + [mid] * (height - 2) + [bottom]
        rows[height // 2] = "⎨"
        return rows
    if ch == "}" and height >= 3:
        rows = [top] + [mid] * (height - 2) + [bottom]
        rows[height // 2] = "⎬"
        return rows
    return [top] + [mid] * (height - 2) + [bottom]


def _wrap_delimiters(box, left, right):
    height = box.height
    parts = []
    for ch in (left, None, right):
        if ch is None:
            parts.append(box)
            continue
        rows = _tall_delimiter(ch, height)
        if ch == "":
            continue
        parts.append(_Box(rows if height > 1 else [rows[0]], box.base if height > 1 else 0))
    return _hcat(parts)


def _combine(text, accent):
    return "".join(ch + accent if not ch.isspace() else ch for ch in text)


def _font_text(text, font):
    if font in ("mathbb", "mathcal", "mathscr", "mathfrak", "mathbf", "boldsymbol", "bm", "pmb", "bf"):
        out = []
        for ch in text:
            mapped = None
            if font == "mathbb":
                names = ("DOUBLE-STRUCK CAPITAL %s", "DOUBLE-STRUCK SMALL %s", "MATHEMATICAL DOUBLE-STRUCK CAPITAL %s",
                         "MATHEMATICAL DOUBLE-STRUCK SMALL %s", "MATHEMATICAL DOUBLE-STRUCK DIGIT %s")
            elif font in ("mathcal", "mathscr"):
                names = ("SCRIPT CAPITAL %s", "MATHEMATICAL SCRIPT CAPITAL %s", "SCRIPT SMALL %s", "MATHEMATICAL SCRIPT SMALL %s")
            elif font == "mathfrak":
                names = ("BLACK-LETTER CAPITAL %s", "MATHEMATICAL FRAKTUR CAPITAL %s", "MATHEMATICAL FRAKTUR SMALL %s")
            else:
                names = ("MATHEMATICAL BOLD CAPITAL %s", "MATHEMATICAL BOLD SMALL %s", "MATHEMATICAL BOLD DIGIT %s")
            if ch.isascii() and (ch.isalpha() or ch.isdigit()):
                label = ch.upper() if ch.isalpha() else {"0": "ZERO", "1": "ONE", "2": "TWO", "3": "THREE", "4": "FOUR",
                                                         "5": "FIVE", "6": "SIX", "7": "SEVEN", "8": "EIGHT", "9": "NINE"}[ch]
                for pattern in names:
                    if ("CAPITAL" in pattern and not ch.isupper() and ch.isalpha()) or ("SMALL" in pattern and not ch.islower()) \
                            or ("DIGIT" in pattern and not ch.isdigit()):
                        continue
                    try:
                        mapped = unicodedata.lookup(pattern % label)
                        break
                    except KeyError:
                        continue
            out.append(mapped or ch)
        return "".join(out)
    return text


class _TexParser(object):
    _TOKEN = re.compile(r"(\s+)|\\([A-Za-z]+\*?)|\\(.)|(.)", re.S)

    def __init__(self, src):
        self.toks = []
        for match in self._TOKEN.finditer(src):
            if match.group(1):
                self.toks.append(("sp", " "))
            elif match.group(2):
                self.toks.append(("cmd", match.group(2)))
            elif match.group(3) is not None:
                self.toks.append(("cmd", match.group(3)))
            else:
                self.toks.append(("ch", match.group(4)))
        self.pos = 0

    # -- token access ----------------------------------------------------------------------------
    def peek(self):
        while self.pos < len(self.toks) and self.toks[self.pos][0] == "sp":
            self.pos += 1
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def take(self):
        token = self.peek()
        if token is not None:
            self.pos += 1
        return token

    def expect(self, ch, message):
        token = self.take()
        if token != ("ch", ch):
            raise _MathError(message)

    # -- grammar ---------------------------------------------------------------------------------
    def parse(self):
        nodes = self.row(())
        if self.peek() is not None:
            token = self.peek()
            raise _MathError("Unexpected %s" % ("}" if token == ("ch", "}") else ("\\" + token[1] if token[0] == "cmd" else token[1])))
        return ("row", nodes)

    def row(self, stops):
        nodes = []
        while True:
            token = self.peek()
            if token is None:
                return nodes
            if token in stops or (token[0] == "cmd" and ("cmd", token[1]) in stops):
                return nodes
            if token == ("ch", "}"):
                if ("ch", "}") in stops:
                    return nodes
                raise _MathError("Extra }")
            if token == ("ch", "&") or token == ("cmd", "\\") or token == ("cmd", "end") or token == ("cmd", "right"):
                if token in stops:
                    return nodes
                if token == ("ch", "&"):
                    raise _MathError("Misplaced &")
                if token == ("cmd", "\\"):
                    self.take()
                    nodes.append(("nl",))
                    continue
                raise _MathError("Extra \\%s" % token[1])
            nodes.append(self.scripted())

    def scripted(self):
        base = self.primary()
        sub = sup = None
        while True:
            token = self.peek()
            if token == ("ch", "^"):
                self.take()
                if sup is not None:
                    raise _MathError("Double superscript")
                sup = self.argument("Missing superscript")
            elif token == ("ch", "_"):
                self.take()
                if sub is not None:
                    raise _MathError("Double subscript")
                sub = self.argument("Missing subscript")
            elif token == ("ch", "'"):
                self.take()
                prime = ("atom", "′", "ord")
                sup = ("row", [sup, prime]) if sup is not None else prime
            else:
                break
        if sub is None and sup is None:
            return base
        return ("script", base, sub, sup)

    def argument(self, message="Missing argument"):
        token = self.peek()
        if token is None or token == ("ch", "}"):
            raise _MathError(message)
        if token == ("ch", "{"):
            self.take()
            nodes = self.row((("ch", "}"),))
            self.expect("}", "Missing }")
            return ("row", nodes)
        return self.primary(True)

    def raw_group(self):
        """The text inside the next {...}, spaces kept (for \\text and environment names)."""
        token = self.peek()
        if token != ("ch", "{"):
            raise _MathError("Missing {")
        self.take()
        depth, out = 1, []
        while True:
            if self.pos >= len(self.toks):
                raise _MathError("Missing }")
            kind, value = self.toks[self.pos]
            self.pos += 1
            if kind == "ch" and value == "{":
                depth += 1
            elif kind == "ch" and value == "}":
                depth -= 1
                if depth == 0:
                    return "".join(out)
            if kind == "cmd":
                out.append(_SYMBOLS.get(value, ("\\" + value, ""))[0] if value in _SYMBOLS or len(value) == 1 else "")
            else:
                out.append(value)

    def delimiter(self):
        token = self.take()
        if token is None:
            raise _MathError("Missing delimiter")
        name = token[1]
        if name in _DELIMS:
            return _DELIMS[name]
        raise _MathError("Invalid delimiter")

    def primary(self, single=False):
        token = self.take()
        if token is None:
            raise _MathError("Unexpected end of formula")
        kind, value = token
        if kind == "ch":
            if value == "{":
                nodes = self.row((("ch", "}"),))
                self.expect("}", "Missing }")
                return ("row", nodes)
            if value in "^_":
                self.pos -= 1
                return ("atom", "", "ord")
            if value == "}":
                raise _MathError("Extra }")
            if value.isdigit():
                text = value
                while not single:
                    nxt = self.toks[self.pos] if self.pos < len(self.toks) else None
                    if nxt and nxt[0] == "ch" and (nxt[1].isdigit() or (nxt[1] == "." and self.pos + 1 < len(self.toks)
                                                                       and self.toks[self.pos + 1][0] == "ch"
                                                                       and self.toks[self.pos + 1][1].isdigit())):
                        text += nxt[1]
                        self.pos += 1
                    else:
                        break
                return ("atom", text, "ord")
            if value in "=<>":
                return ("atom", value, "rel")
            if value == ":":
                return ("atom", ":", "rel")
            if value in "+-*":
                return ("atom", {"-": "−", "*": "∗"}.get(value, value), "bin")
            if value in "([":
                return ("atom", value, "open")
            if value in ")]":
                return ("atom", value, "close")
            if value in ",;":
                return ("atom", value, "punct")
            if value == "~":
                return ("atom", " ", "ord")
            if value == "|":
                return ("atom", "|", "ord")
            return ("atom", value, "ord")
        return self.command(value)

    def command(self, name):
        if name in _NOOPS:
            return ("atom", "", "ord")
        if name in _SYMBOLS:
            text, kind = _SYMBOLS[name]
            if kind == "op":
                return ("atom", text, "bigop")
            return ("atom", text, kind)
        if name in _SPACES:
            return ("atom", _SPACES[name], "ord")
        if name in _NAMED_OPS:
            return ("atom", name, "named" if name not in _LIMIT_OPS else "namedlim")
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            return ("frac", self.argument("Missing numerator"), self.argument("Missing denominator"))
        if name in ("binom", "dbinom", "tbinom", "choose"):
            return ("binom", self.argument("Missing argument"), self.argument("Missing argument"))
        if name == "sqrt":
            index = None
            if self.peek() == ("ch", "["):
                self.take()
                nodes = self.row((("ch", "]"),))
                self.expect("]", "Missing ]")
                index = ("row", nodes)
            return ("sqrt", self.argument("Missing radicand"), index)
        if name in _ACCENTS:
            return ("accent", _ACCENTS[name], self.argument("Missing argument"), name in ("overline", "underline"))
        if name in ("underbrace", "overbrace", "underleftarrow", "underrightarrow", "xcancel", "cancel", "boxed",
                    "fbox", "colorbox", "phantom", "vphantom", "hphantom", "smash", "mathstrut", "lefteqn", "substack",
                    "displaylines", "stackrel", "overset", "underset", "not", "xrightarrow", "xleftarrow", "hspace",
                    "hspace*", "vspace", "vspace*", "color", "textcolor", "label", "tag", "href", "tag*", "mathllap",
                    "mathrlap", "mathclap", "rlap", "llap"):
            return self.special(name)
        if name in _FONTS:
            if name in ("text", "textrm", "textbf", "textit", "textsf", "texttt", "mbox", "hbox", "operatorname"):
                text = self.raw_group()
                return ("text", text, name)
            if name in ("bf", "it", "rm", "sf", "tt"):
                return ("atom", "", "ord")
            return ("font", name, self.argument("Missing argument"))
        if name in _SIZES:
            self.take()
            return ("atom", "", "ord")
        if name == "left":
            left = self.delimiter()
            nodes = self.row((("cmd", "right"),))
            if self.take() != ("cmd", "right"):
                raise _MathError("Missing \\right")
            right = self.delimiter()
            return ("delim", left, ("row", nodes), right)
        if name == "begin":
            return self.environment()
        raise _MathError("Undefined control sequence \\%s" % name)

    def special(self, name):
        if name in ("hspace", "hspace*", "vspace", "vspace*"):
            self.raw_group()
            return ("atom", " " if name.startswith("h") else "", "ord")
        if name in ("label", "tag", "tag*", "href"):
            self.raw_group()
            if name == "href":
                return ("row", [self.argument()])
            return ("atom", "", "ord")
        if name == "color":
            self.raw_group()
            return ("atom", "", "ord")
        if name in ("textcolor", "colorbox"):
            self.raw_group()
            return self.argument()
        if name in ("stackrel", "overset"):
            top = self.argument()
            return ("stack", top, self.argument(), "over")
        if name == "underset":
            under = self.argument()
            return ("stack", under, self.argument(), "under")
        if name in ("xrightarrow", "xleftarrow"):
            sub = None
            if self.peek() == ("ch", "["):
                self.take()
                sub = ("row", self.row((("ch", "]"),)))
                self.expect("]", "Missing ]")
            arrow = ("atom", "→" if name == "xrightarrow" else "←", "rel")
            return ("stack", self.argument(), arrow, "over", sub)
        if name == "not":
            target = self.argument()
            return ("not", target)
        if name in ("underbrace", "overbrace"):
            return ("brace", name == "overbrace", self.argument())
        if name in ("cancel", "xcancel"):
            return ("accent", "\u0338", self.argument(), False)
        if name == "boxed" or name == "fbox":
            return ("boxed", self.argument())
        if name == "substack":
            return ("row", [self.argument()])
        return ("row", [self.argument()])

    def environment(self):
        name = self.raw_group()
        if name == "array":
            if self.peek() == ("ch", "{"):
                self.raw_group()
        if name not in _MATRICES and name not in _ALIGNED:
            raise _MathError("Unknown environment '%s'" % name)
        rows, cells, stops = [], [], (("ch", "&"), ("cmd", "\\"), ("cmd", "end"))
        while True:
            nodes = self.row(stops)
            cells.append(("row", nodes))
            token = self.take()
            if token is None:
                raise _MathError("Missing \\end{%s}" % name)
            if token == ("ch", "&"):
                continue
            if token == ("cmd", "\\"):
                rows.append(cells)
                cells = []
                continue
            closing = self.raw_group()
            if closing != name:
                raise _MathError("\\end{%s} does not match \\begin{%s}" % (closing, name))
            if cells and not (len(cells) == 1 and not cells[0][1]):
                rows.append(cells)
            return ("env", name, rows)


def _kind_of(node):
    if node[0] == "atom":
        return node[2]
    if node[0] == "script":
        return _kind_of(node[1])
    if node[0] in ("frac", "binom"):
        return "ord"
    if node[0] == "nl":
        return "nl"
    return "ord"


class _Typesetter(object):
    def __init__(self, display):
        self.display = display
        self.tight = 0              # inside a script: no spaces around operators

    # -- rows --------------------------------------------------------------------------------------
    def row(self, nodes, inline):
        """A list of nodes as one box.  ``inline`` forces a single text row."""
        if any(node[0] == "nl" for node in nodes) and not inline:
            lines, current = [], []
            for node in nodes:
                if node[0] == "nl":
                    lines.append(current)
                    current = []
                else:
                    current.append(node)
            lines.append(current)
            return _vstack([self.row(line, inline) for line in lines])
        nodes = [n for n in nodes if n[0] != "nl"]
        pieces, kinds = [], []
        for node in nodes:
            pieces.append(self.node(node, inline))
            kinds.append(_kind_of(node))
        out = []
        for index, box in enumerate(pieces):
            kind = kinds[index]
            previous = kinds[index - 1] if index else None
            if kind == "bin" and (previous is None or previous in ("bin", "rel", "open", "punct", "bigop", "named", "namedlim")):
                kind = "ord"
            following = kinds[index + 1] if index + 1 < len(kinds) else None
            if kind in ("rel", "bin") and out and not self.tight:
                out.append(_Box([" "]))
            out.append(box)
            if kind in ("rel", "bin") and following is not None and not self.tight:
                out.append(_Box([" "]))
            elif kind == "punct" and following is not None and following != "close":
                out.append(_Box([" "]))
            elif kind in ("named", "namedlim", "bigop") and following not in (None, "open", "close", "punct"):
                out.append(_Box([" "]))
        return _hcat(out)

    # -- nodes -------------------------------------------------------------------------------------
    def node(self, node, inline):
        kind = node[0]
        if kind == "atom":
            return _Box([node[1]])
        if kind == "row":
            return self.row(node[1], inline)
        if kind == "text":
            text = node[1]
            return _Box([text])
        if kind == "font":
            inner = self.node(node[2], inline)
            if inner.height == 1:
                return _Box([_font_text(inner.lines[0], node[1])])
            return inner
        if kind == "accent":
            return self.accent(node, inline)
        if kind == "script":
            return self.script(node, inline)
        if kind == "frac":
            return self.fraction(node, inline)
        if kind == "binom":
            return self.binomial(node, inline)
        if kind == "sqrt":
            return self.root(node, inline)
        if kind == "delim":
            inner = self.node(node[2], inline)
            return _wrap_delimiters(inner, node[1], node[3]) if not inline else _hcat([_Box([node[1]]), inner, _Box([node[3]])])
        if kind == "env":
            return self.environment(node, inline)
        if kind == "stack":
            return self.stack(node, inline)
        if kind == "not":
            inner = self.node(node[1], inline)
            return _Box([_combine(inner.lines[0], "\u0338")]) if inner.height == 1 else inner
        if kind == "brace":
            inner = self.node(node[2], inline)
            if inline or inner.height > 1:
                return inner
            bar = _Box([("⏞" if node[1] else "⏟") * max(1, inner.width)])
            return _vstack([bar, inner] if node[1] else [inner, bar], 1 if node[1] else 0)
        if kind == "boxed":
            inner = self.node(node[1], inline)
            return _hcat([_Box(["["]), inner, _Box(["]"])])
        raise _MathError("Cannot typeset %s" % kind)

    def accent(self, node, inline):
        inner = self.node(node[2], inline)
        if inner.height != 1:
            return inner
        text = inner.lines[0]
        if node[3] and len(text) > 1 and node[1] == "\u0305" and not inline:
            return _vstack([_Box(["‾" * len(text)]), inner], 1)
        if node[1] == "\u20d7" and len(text) > 1:
            return _Box([_combine(text[:-1], "") + text[-1] + node[1]])
        if node[1] in ("\u0305", "\u0332"):
            return _Box([_combine(text, node[1])])
        # an accent sits on the last character of the argument
        return _Box([text[:-1] + text[-1] + node[1]]) if text else inner

    def stack(self, node, inline):
        top, bottom = self.node(node[1], True), self.node(node[2], True)
        position = node[3]
        extra = self.node(node[4], True) if len(node) > 4 and node[4] is not None else None
        if inline or top.height > 1 or bottom.height > 1:
            suffix = ("^" + top.lines[0]) if position == "over" else ("_" + top.lines[0])
            return _hcat([bottom, _Box([suffix if len(top.lines[0]) == 1 else suffix[0] + "(" + top.lines[0] + ")"])])
        rows = [top, bottom] if position == "over" else [bottom, top]
        if extra is not None:
            rows.append(extra)
        return _vstack(rows, 1 if position == "over" else 0)

    def fraction(self, node, inline):
        num, den = self.node(node[1], True), self.node(node[2], True)
        if inline or not self.display:
            a, b = num.lines[0], den.lines[0]
            if (a, b) in _VULGAR:
                return _Box([_VULGAR[(a, b)]])
            wrap_a = a if len(a) == 1 or re.match(r"^[\w.′]+$", a) else "(" + a + ")"
            wrap_b = b if len(b) == 1 or re.match(r"^[\w.′]+$", b) else "(" + b + ")"
            return _Box([wrap_a + "/" + wrap_b])
        num, den = self.node(node[1], False), self.node(node[2], False)
        width = max(num.width, den.width) + 2
        bar = _Box(["─" * width])
        return _vstack([num, bar, den], 1)

    def binomial(self, node, inline):
        top, bottom = self.node(node[1], True), self.node(node[2], True)
        if inline or not self.display:
            return _Box(["C(" + top.lines[0] + "," + bottom.lines[0] + ")"])
        top, bottom = self.node(node[1], False), self.node(node[2], False)
        stacked = _vstack([top, bottom], 0)
        stacked.base = top.height - 1 if top.height else 0
        return _wrap_delimiters(stacked, "(", ")")

    def root(self, node, inline):
        body = self.node(node[1], inline)
        index = self.node(node[2], True).lines[0] if node[2] is not None else None
        if inline or not self.display or body.height > 3:
            text = body.lines[0] if body.height == 1 else " ".join(l.strip() for l in body.lines)
            sign = "√"
            if index:
                sign = {"3": "∛", "4": "∜"}.get(index) or (_script_text(index, _SUP) or "(%s)" % index) + "√"
            wrapped = text if len(text) == 1 or re.match(r"^[\w.]+$", text) else "(" + text + ")"
            return _Box([sign + wrapped])
        width = body.width
        prefix = (_script_text(index, _SUP) or "") if index else ""
        pre = max(1, len(prefix) + 1)
        rows = [" " * pre + "‾" * width]
        for k, line in enumerate(body.lines):
            last = k == body.height - 1
            rows.append((prefix.rjust(pre - 1) + "√") if last else (" " * (pre - 1) + "│"))
            rows[-1] += line
        return _Box(rows, 1 + body.base)

    def script(self, node, inline):
        _, base_node, sub_node, sup_node = node
        base = self.node(base_node, inline)
        base_kind = _kind_of(base_node)
        self.tight += 1
        try:
            sub = self.node(sub_node, True) if sub_node is not None else None
            sup = self.node(sup_node, True) if sup_node is not None else None
        finally:
            self.tight -= 1
        limits = (base_kind == "namedlim") or (base_kind == "bigop" and base_node[1] in _LARGE_LIMITS)
        if limits and self.display and not inline:
            parts = [b for b in (sup, base, sub) if b is not None]
            index = 1 if sup is not None else 0
            return _vstack(parts, index)
        sub_text = _script_text(sub.lines[0], _SUB) if sub is not None else ""
        sup_text = _script_text(sup.lines[0], _SUP) if sup is not None else ""
        if (sub is None or sub_text is not None) and (sup is None or sup_text is not None) and base.height == 1:
            return _Box([base.lines[0] + (sub_text or "") + (sup_text or "")])
        if inline or not self.display:
            tail = ""
            if sub is not None:
                tail += "_" + (sub.lines[0] if len(sub.lines[0]) == 1 else "(" + sub.lines[0] + ")")
            if sup is not None:
                tail += "^" + (sup.lines[0] if len(sup.lines[0]) == 1 else "(" + sup.lines[0] + ")")
            return _hcat([base, _Box([tail])])
        self.tight += 1
        try:
            sub = self.node(sub_node, False) if sub_node is not None else None
            sup = self.node(sup_node, False) if sup_node is not None else None
        finally:
            self.tight -= 1
        width = max(sup.width if sup else 0, sub.width if sub else 0)
        rows, base_row = [], 0
        if sup is not None:
            rows.extend(_pad(sup, width, "left").lines)
        rows.extend(base.lines)
        base_row = (sup.height if sup is not None else 0) + base.base
        if sub is not None:
            rows.extend(_pad(sub, width, "left").lines)
        sup_rows = sup.height if sup is not None else 0
        # place the script columns to the right of the base: build row by row
        final = []
        for index in range(len(rows)):
            if index < sup_rows:
                final.append(" " * base.width + rows[index])
            elif index < sup_rows + base.height:
                final.append(rows[index] + " " * width)
            else:
                final.append(" " * base.width + rows[index])
        return _Box(final, base_row)

    def environment(self, node, inline):
        name, rows = node[1], node[2]
        if name in _ALIGNED:
            if inline:
                texts = [" ".join(self.node(cell, True).lines[0] for cell in cells) for cells in rows]
                return _Box(["; ".join(texts)])
            columns = max(len(cells) for cells in rows) if rows else 0
            grid = [[self.node(cell, False) for cell in cells] for cells in rows]
            widths = [max((r[k].width for r in grid if k < len(r)), default=0) for k in range(columns)]
            out_rows = []
            for r in grid:
                parts = []
                for k in range(columns):
                    box = r[k] if k < len(r) else _Box([""])
                    parts.append(_pad(box, widths[k], "right" if k % 2 == 0 else "left"))
                    if k < columns - 1:
                        parts.append(_Box([" " if k % 2 == 0 else "  "]))
                out_rows.append(_hcat(parts))
            return _vstack(out_rows, 0, "left")
        left, right = _MATRICES[name]
        if inline:
            texts = [" ".join(self.node(cell, True).lines[0] for cell in cells) for cells in rows]
            return _Box([left + "; ".join(texts) + right])
        grid = [[self.node(cell, False) for cell in cells] for cells in rows]
        if not grid:
            return _Box([left + right])
        columns = max(len(r) for r in grid)
        widths = [max((r[k].width for r in grid if k < len(r)), default=0) for k in range(columns)]
        align = "left" if name in ("cases", "rcases", "dcases") else "center"
        line_boxes = []
        for r in grid:
            parts = []
            for k in range(columns):
                box = r[k] if k < len(r) else _Box([""])
                parts.append(_pad(box, widths[k], align))
                if k < columns - 1:
                    parts.append(_Box(["  "]))
            line_boxes.append(_hcat(parts))
        body = _vstack(line_boxes, 0, "left")
        body.base = body.height // 2
        return _wrap_delimiters(body, left, right)


def _typeset(src, display):
    tree = _TexParser(src).parse()
    box = _Typesetter(display).row(tree[1], not display)
    lines = [line.rstrip() for line in box.lines]
    width = max([len(line) for line in lines] + [0])
    return [line + " " * (width - len(line)) for line in lines]


def math_html(tex, ctx, display=False):
    """A TeX formula: display style (stacked, centred) or one text row.  A formula that does not typeset shows its source."""
    source = tex if isinstance(tex, str) else ""
    if display:
        source = "\n".join(line for line in source.split("\n") if line.strip())
    else:
        source = re.sub(r"\s*\n\s*", " ", source)
    source = source.strip()
    try:
        lines = _typeset(source, display)
    except _MathError as error:
        return ('<span style="color: %s" title="%s"><tt>%s</tt></span>'
                % (_col(ctx, "error"), _attr(str(error)), _nbsp(_e(source))))
    except RecursionError:
        return '<span style="color: %s" title="too deep"><tt>%s</tt></span>' % (_col(ctx, "error"), _nbsp(_e(source)))
    color = _text_color(ctx)
    body = "<br>".join(_nbsp(_e(line)) for line in lines)
    if display:
        return '<div style="text-align: center; color: %s; margin: 4px 0" title="%s"><tt>%s</tt></div>' % (color, _attr(source), body)
    return '<span style="color: %s" title="%s"><tt>%s</tt></span>' % (color, _attr(source), body)


def _draw_math(node, ctx):
    props = node.get("p") or {}
    text = props.get("text")
    display = bool(props.get("display"))
    result = math_html(text if isinstance(text, str) else "", ctx, display)
    return result if display else "<div>%s</div>" % result


# ---------------------------------------------------------------------------------------------

DRAWERS.update({
    "text": _draw_text,
    "md": _draw_md,
    "code": _draw_code,
    "diff": _draw_diff,
    "ansi": _draw_ansi,
    "rows": _draw_rows,
    "math": _draw_math,
})
