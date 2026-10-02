"""TSP HTML renderer: turns TSP nodes into Sublime "minihtml" for phantoms.

This is the OUTPUT (SINK) block of GhostShell's native TSP view.  It replaces
tsp_text.py's plain lines with rounded cards, coloured chips, diffs, meters and
so on, drawn in the program's own theme (omp sends its palette as a `t`
message; ``Theme`` reads it).

minihtml is a small subset of HTML/CSS (https://www.sublimetext.com/docs/minihtml.html):
no tables, no flexbox, no CSS widths, no SVG.  Bars are scaled 1x1 PNG images,
columns are monospace-aligned text, side-by-side blocks are inline-blocks.

Pure Python: no Sublime import, so it can be read and run on its own.
Each ``_draw_<kind>(node, ctx)`` returns an HTML string.
"""

import base64
import html
import json
import re
import struct
import urllib.parse
import zlib

# Symbols for omp's icon names (plain Unicode that ordinary fonts have).
ICONS = {
    "lightbulb": "◆", "model": "◈", "chev": "▾", "check": "✓",
    "error": "✗", "warning": "▲", "info": "●", "folder": "▤",
    "branch": "⑂", "search": "⌕", "bell": "◎", "copy": "⧉",
}

DEFAULT_TOKENS = {
    "pageBg": "#0d1117", "cardBg": "#12171d", "border": "#2a3038", "borderMuted": "#1f252d",
    "borderAccent": "#00b4ff", "accent": "#00b4ff", "success": "#00ff88", "error": "#ff4757",
    "warning": "#ffb347", "muted": "#9ca3b0", "dim": "#6b7280", "text": "#e6eaf0",
    "toolPendingBg": "#14202b", "toolSuccessBg": "#0f2a1d", "toolErrorBg": "#2d1418",
    "userMessageBg": "#1b2733", "selectedBg": "#1b2733", "toolDiffAdded": "#00ff88",
    "toolDiffRemoved": "#ff4757", "toolDiffContext": "#6b7280", "mdHeading": "#00b4ff",
    "mdCode": "#ffb347", "mdCodeBlock": "#e6eaf0", "mdCodeBlockBorder": "#2a3038",
    "mdLink": "#00b4ff", "mdListBullet": "#00b4ff", "mdQuote": "#9ca3b0", "mdQuoteBorder": "#2a3038",
    "infoBg": "#14202b",
}

_PNG_CACHE = {}


def _png_pixel(hex_color):
    """A 1x1 PNG data URL of ``hex_color`` ("#rrggbb"); scaled with width/height it is a coloured bar."""
    key = hex_color.lower()
    if key not in _PNG_CACHE:
        red, green, blue = (int(key[i:i + 2], 16) for i in (1, 3, 5))

        def chunk(kind, data):
            body = kind + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(b"\x00" + bytes((red, green, blue, 255))))
               + chunk(b"IEND", b""))
        _PNG_CACHE[key] = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return _PNG_CACHE[key]


class Theme:
    """Colours by name, from the program's palette message, with sensible defaults."""

    def __init__(self, palette_message=None, dark=True):
        tokens = dict(DEFAULT_TOKENS)
        message = palette_message or {}
        variant = message.get("dark" if dark else "light") or message.get("dark") or {}
        tokens.update({name: value for name, value in variant.items() if isinstance(value, str)})
        self.tokens = tokens

    def color(self, name, default=None):
        return self.tokens.get(name) or default or self.tokens["muted"]


class StaticClock:
    """No animation: elapsed timers show the age they were sent with and spinners hold still."""

    @staticmethod
    def elapsed_ms(node_id, age_ms):
        return age_ms or 0

    @staticmethod
    def visible_ms(node_id):
        return 0

    @staticmethod
    def spinner_index(frame_count):
        return 0


class Context:
    """What a drawer needs: the theme and the available width in characters."""

    def __init__(self, theme, cols=100, surface_id=None, clock=None, blobs=None, local=None):
        self.theme = theme
        self.local = local if local is not None else {}    # view-only state, e.g. an expanded section
        self.cols = cols
        self.blobs = blobs or {}        # content hash -> base64 text of images the program sent
        self.surface_id = surface_id    # goes into the events that clicks send back
        self.clock = clock or StaticClock()
        self.animated = False           # set by drawers whose output changes with time


# omp uses symbols from icon fonts (private-use characters); without that font they show as empty
# boxes.  Known ones get a plain stand-in, the rest are left out.
_PRIVATE_USE = re.compile("[-󰀀-󿿿]")
_SYMBOL_FALLBACK = {"󰙺": "$", "󰌑": "↵"}


def _plain_symbols(text):
    return _PRIVATE_USE.sub(lambda match: _SYMBOL_FALLBACK.get(match.group(0), ""), text)


def esc(text):
    return html.escape(_plain_symbols(text if isinstance(text, str) else str(text)), quote=False)


def _hex_with_alpha(color, alpha):
    """"#rrggbb" + alpha 0..1 -> "#rrggbbaa" for tinted backgrounds."""
    return "%s%02x" % (color, int(max(0.0, min(1.0, alpha)) * 255))


# ---- text ------------------------------------------------------------------

_SPAN_COLORS = {"muted": "muted", "dim": "dim", "accent": "accent", "success": "success",
                "warning": "warning", "error": "error", "info": "accent", "path": "muted",
                "key": "accent", "link": "mdLink", "num": "warning", "ins": "toolDiffAdded",
                "del": "toolDiffRemoved", "code": "mdCode"}


def _span_html(text, tokens, ctx):
    """One styled run.  ``tokens`` is the space-separated style string from the wire."""
    style = []
    for token in (tokens or "").split():
        if token == "hide":
            return ""
        if token in ("strong",):
            style.append("font-weight: bold")
        elif token == "em":
            style.append("font-style: italic")
        elif token in ("mark",):
            style.append("background-color: %s" % ctx.theme.color("selectedBg"))
        elif token == "typo":
            style.append("text-decoration: underline; color: %s" % ctx.theme.color("error"))
        elif token in _SPAN_COLORS:
            style.append("color: %s" % ctx.theme.color(_SPAN_COLORS[token]))
        elif token in ctx.theme.tokens:
            style.append("color: %s" % ctx.theme.color(token))
    body = esc(text).replace("\n", "<br>")
    return '<span style="%s">%s</span>' % ("; ".join(style), body) if style else body


def text_html(value, ctx, base_tokens=""):
    """A str or a list of {"t","s"} spans as inline HTML."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _span_html(value, base_tokens, ctx)
    return "".join(_span_html(span.get("t", ""), ("%s %s" % (base_tokens, span.get("s") or "")).strip(), ctx)
                   for span in value if isinstance(span, dict))


# ---- small inline pieces -----------------------------------------------------

def chip(text, ctx, tone="accent"):
    color = ctx.theme.color(tone)
    return ('<span style="background-color: %s; color: %s; border-radius: 9px; padding: 1px 8px; '
            'font-size: 0.85rem">%s</span>' % (_hex_with_alpha(color, 0.16), color, esc(text)))


def keycap(keys, ctx):
    label = "+".join(keys) if isinstance(keys, (list, tuple)) else str(keys)
    return ('<span style="background-color: %s; color: %s; border: 1px solid %s; border-radius: 4px; '
            'padding: 0 5px; font-size: 0.85rem"><tt>%s</tt></span>'
            % (ctx.theme.color("selectedBg"), ctx.theme.color("muted"), ctx.theme.color("border"), esc(label)))


def bar(value, ctx, color_name="accent", width=160, height=6):
    """A progress bar made of two scaled 1x1 images; ``value`` 0..1 or None (unknown)."""
    track = _png_pixel(ctx.theme.color("border"))
    if value is None:
        return '<img src="%s" width="%d" height="%d">' % (track, width, height)
    filled = int(round(max(0.0, min(1.0, value)) * width))
    fill = _png_pixel(ctx.theme.color(color_name))
    parts = []
    if filled:
        parts.append('<img src="%s" width="%d" height="%d">' % (fill, filled, height))
    if width - filled:
        parts.append('<img src="%s" width="%d" height="%d">' % (track, width - filled, height))
    return "".join(parts)


def _format_age(milliseconds):
    seconds = (milliseconds or 0) / 1000.0
    return "%.1fs" % seconds if seconds < 60 else "%dm%02ds" % (seconds // 60, seconds % 60)


# ---- block helpers ---------------------------------------------------------------

def _children(node, ctx):
    return "".join(draw(child, ctx) for child in node.get("c") or [])


# ---- clicks ----------------------------------------------------------------------
#
# A click target is an <a href="tsp:..."> whose href carries the event to send back.
# minihtml drops a link that wraps a block (<div>), so links only ever wrap inline
# content.  The href is a pure function of the event, so unchanged panels keep
# identical HTML and Sublime does not redraw them.

def event_href(event):
    """The href that stands for ``event`` (a dict)."""
    return "tsp:" + urllib.parse.quote(json.dumps(event, separators=(",", ":"), ensure_ascii=False), safe="")


def href_event(href):
    """The event dict inside an href made by event_href, or None."""
    if not href.startswith("tsp:"):
        return None
    try:
        value = json.loads(urllib.parse.unquote(href[4:]))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def node_text(node):
    """All the plain text under ``node`` (for a copy action)."""
    props = node.get("p") or {}
    head = props.get("head")
    if isinstance(head, str) and any(child.get("id") == head for child in node.get("c") or []):
        head = None             # the head names a child node (its text comes with the children), not text
    own = props.get("text") or plain_text(props.get("spans")) or plain_text(head)
    return "\n".join(part for part in [own] + [node_text(child) for child in node.get("c") or []] if part)


def _click_event(node, ctx):
    """The event a click on ``node`` should send, or None when it is not clickable."""
    props = node.get("p") or {}
    if node.get("k") in ("editor", "input") and ctx.surface_id is not None:
        return {"ev": "focus", "sf": ctx.surface_id, "id": node.get("id")}
    actions = props.get("actions")
    click = actions.get("click") if isinstance(actions, dict) else None   # a picker's own "actions" is a list
    if not click:
        return None
    surface, node_id = ctx.surface_id, node.get("id")
    if click == "toggle":
        return {"ev": "toggle", "sf": surface, "id": node_id, "collapsed": not props.get("collapsed", False)}
    if click == "copy":
        return {"local": "copy", "text": node_text(node)}
    if click == "open":
        return {"local": "open", "href": props.get("href") or ""}
    if click in ("select", "activate"):
        return {"ev": click, "sf": surface, "id": node_id, "item": node_id}
    if click == "zoom":
        return None
    return {"ev": "action", "sf": surface, "id": node_id, "act": click}


def _link_wrap(html_text, href, ctx, title=None):
    """Make ``html_text`` clickable: a link around inline HTML, or inside a single block's content.
    ``title`` is the program's own explanation of the click (shown when the pointer rests on it)."""
    tip = ' title="%s"' % html.escape(title, quote=True) if title else ""
    open_tag = '<a href="%s"%s style="text-decoration: none; color: %s">' % (href, tip, ctx.theme.color("text"))
    if html_text.startswith("<div") and html_text.endswith("</div>") and html_text.count("<div") == 1:
        end_of_tag = html_text.index(">") + 1
        return html_text[:end_of_tag] + open_tag + html_text[end_of_tag:-len("</div>")] + "</a></div>"
    if "<div" in html_text:
        return html_text            # holds nested blocks: a link around them would vanish
    return open_tag + html_text + "</a>"


def _toggle_link(node, ctx, inner_html):
    """The head of a collapsible node, clickable to expand or collapse it."""
    props = node.get("p") or {}
    if not props.get("collapsible") or ctx.surface_id is None:
        return inner_html
    event = {"ev": "toggle", "sf": ctx.surface_id, "id": node.get("id"), "collapsed": not props.get("collapsed", False)}
    if props.get("key"):
        event["key"] = props["key"]
    return '<a href="%s" style="text-decoration: none; color: %s">%s</a>' % (
        event_href(event), ctx.theme.color("text"), inner_html)


def _item_link(ctx, owner, item_id, event_name, inner_html):
    """A list/picker/tab entry, clickable (a click does what Enter does)."""
    if ctx.surface_id is None:
        return inner_html
    event = {"ev": event_name, "sf": ctx.surface_id, "id": owner.get("id"), "item": item_id}
    return '<a href="%s" style="text-decoration: none; color: %s">%s</a>' % (
        event_href(event), ctx.theme.color("text"), inner_html)


def draw(node, ctx):
    """HTML for ``node`` and its children."""
    if (node.get("p") or {}).get("hidden"):
        return ""
    drawer = _DRAWERS.get(node.get("k"), _draw_default)
    try:
        html_text = drawer(node, ctx)
    except Exception as error:      # show the fault in place, keep drawing everything else
        DRAW_ERRORS.append("%s %s: %s: %s" % (node.get("k"), node.get("id"), type(error).__name__, error))
        del DRAW_ERRORS[:-50]       # keep the last 50, for diagnosis
        return ('<div style="background-color: %s; color: %s; border-radius: 6px; padding: 2px 8px; margin: 2px 0">'
                '&#9888; could not draw a "%s" element: %s</div>'
                % (_hex_with_alpha(ctx.theme.color("error"), 0.2), ctx.theme.color("error"),
                   esc(node.get("k", "?")), esc("%s: %s" % (type(error).__name__, error))))
    event = _click_event(node, ctx)
    if event is not None and html_text:
        html_text = _link_wrap(html_text, event_href(event), ctx, (node.get("p") or {}).get("title"))
    return html_text


def _draw_default(node, ctx):
    return _children(node, ctx)


def _draw_col(node, ctx):
    if (node.get("p") or {}).get("role") == "omp.editor":
        return _draw_composer_box(node, ctx)
    return "<div>%s</div>" % _children(node, ctx)


# Kinds that flow inline inside a row (the rest are blocks and become inline-blocks).
_INLINE_KINDS = ("text", "icon", "kbd", "badge", "elapsed", "rate", "meter", "progress", "effort",
                 "shimmer", "spinner", "image", "seg")


def _inline_text(node, ctx):
    """A text node as inline HTML (no wrapping div) so it can sit beside an icon or keycap."""
    props = node.get("p") or {}
    if props.get("spans"):
        return text_html(props["spans"], ctx)
    return _md_inline(props.get("text") or "", ctx)


def _draw_row(node, ctx):
    pieces = []     # (html, is_block)
    for child in node.get("c") or []:
        if child.get("k") == "text":
            inner, is_block = _inline_text(child, ctx), False
            click = _click_event(child, ctx)         # omp can make a small label clickable (Copy, Rewind)
            if click is not None and inner:
                inner = _link_wrap(inner, event_href(click), ctx, (child.get("p") or {}).get("title"))
        else:
            inner, is_block = draw(child, ctx), child.get("k") not in _INLINE_KINDS
            if child.get("k") == "row" and inner.startswith("<div>") and inner.endswith("</div>") and inner.count("<div") == 1:
                inner, is_block = inner[len("<div>"):-len("</div>")], False     # a one-line row sits inline
        if inner:
            pieces.append((inner, is_block))
    if not pieces:
        return ""
    # minihtml cannot size inline-blocks to their content, so side-by-side columns are
    # unreliable: block children are stacked, inline children flow on one line.
    out, line = [], []
    for inner, is_block in pieces:
        if is_block:
            if line:
                out.append("<div>%s</div>" % "".join(line))
                line = []
            out.append(inner)
        else:
            line.append(inner + "&nbsp;&nbsp;")
    if line:
        out.append("<div>%s</div>" % "".join(line))
    return "".join(out)


def _draw_spacer(node, ctx):
    return '<div style="font-size: 0.4rem">&nbsp;</div>'


def _draw_rule(node, ctx):
    label = text_html((node.get("p") or {}).get("label"), ctx)
    line = '<div style="border-top: 1px solid %s; margin: 6px 0"></div>' % ctx.theme.color("border")
    return line if not label else '<div style="color: %s">%s</div>%s' % (ctx.theme.color("dim"), label, line)


DRAW_ERRORS = []        # the last elements that failed to draw ("kind id: error"), newest last

STATUS_MARKS = {"running": ("●", "accent"), "done": ("✓", "success"), "error": ("✗", "error"),
                "cancelled": ("○", "dim"), "pending": ("◌", "dim")}


def _card_shell(inner, ctx, background="cardBg", border="border", accent_edge=None):
    edge = "border-left: 3px solid %s; " % ctx.theme.color(accent_edge) if accent_edge else ""
    return ('<div style="background-color: %s; border: 1px solid %s; %sborder-radius: 8px; '
            'padding: 8px 12px; margin: 5px 0">%s</div>'
            % (ctx.theme.color(background), ctx.theme.color(border), edge, inner))


def _draw_card(node, ctx):
    props = node.get("p") or {}
    role = props.get("role") or ""
    # omp may name a child node as the head ("head": "<child id>") instead of giving text.
    head_value = props.get("head")
    head_child = None
    if isinstance(head_value, str):
        head_child = next((child for child in node.get("c") or [] if child.get("id") == head_value), None)
    if head_child is not None:
        head = draw(head_child, ctx)
        if head.startswith("<div>") and head.endswith("</div>"):
            head = head[len("<div>"):-len("</div>")]
    else:
        head = text_html(head_value, ctx)
    mark, tone = STATUS_MARKS.get(props.get("status"), ("", "dim"))
    head_html = ""
    if head or mark:
        head_inner = '%s %s' % ('<span style="color: %s">%s</span>' % (ctx.theme.color(tone), mark) if mark else "", head)
        head_html = '<div style="font-weight: bold">%s</div>' % _toggle_link(node, ctx, head_inner)
    body = ""
    if not props.get("collapsed"):
        body = "".join(draw(child, ctx) for child in node.get("c") or [] if child is not head_child)
    if role == "omp.welcome":
        return _card_shell(body, ctx, "cardBg", "borderMuted", "accent")
    tone_name = props.get("tone")
    if role == "omp.user" or tone_name == "user":
        return _card_shell(head_html + body, ctx, "userMessageBg", "border")      # omp's own user-message colour
    edge = tone_name if tone_name in ("error", "warning", "success", "accent") else None
    return _card_shell(head_html + body, ctx, "cardBg", edge or "border", edge)


def _head_parts(node, ctx):
    """(head HTML, the child that holds it or None).  omp may give a head as text, or name a child
    node as the head ("head": "<child id>"); that child is drawn inline and left out of the body."""
    value = (node.get("p") or {}).get("head")
    child = None
    if isinstance(value, str):
        child = next((c for c in node.get("c") or [] if c.get("id") == value), None)
    if child is None:
        return text_html(value, ctx), None
    html_text = draw(child, ctx)
    if html_text.startswith("<div>") and html_text.endswith("</div>"):
        html_text = html_text[len("<div>"):-len("</div>")]
    return html_text, child


def _draw_section(node, ctx):
    props = node.get("p") or {}
    arrow = chr(9656) if props.get("collapsed") else chr(9662)
    head_html, head_child = _head_parts(node, ctx)
    head = '<div style="color: %s">%s</div>' % (
        ctx.theme.color("muted"), _toggle_link(node, ctx, "%s %s" % (arrow, head_html)))
    if props.get("collapsed"):
        return head
    body = "".join(draw(child, ctx) for child in node.get("c") or [] if child is not head_child)
    return head + '<div style="padding-left: 14px">%s</div>' % body


def _draw_text(node, ctx):
    props = node.get("p") or {}
    role = props.get("role") or ""
    if props.get("spans"):
        body = text_html(props["spans"], ctx)
    else:
        body = _md_inline(props.get("text") or "", ctx).replace("\n", "<br>")
    if role.endswith("heading"):
        return '<div style="color: %s; font-weight: bold; margin-top: 4px">%s</div>' % (ctx.theme.color("mdHeading"), body)
    if role.endswith(("version", "greeting")):
        return '<div style="color: %s">%s</div>' % (ctx.theme.color("muted"), body)
    return "<div>%s</div>" % body


# Inline markdown: `code`, **bold**, *italic*, [text](url)
_MD_INLINE = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*|\*([^*]+)\*|\[([^\]]+)\]\(([^)]+)\)")


def _md_inline(text, ctx):
    out, at = [], 0
    for match in _MD_INLINE.finditer(text):
        out.append(esc(text[at:match.start()]))
        code, bold, italic, label, url = match.groups()
        if code is not None:
            out.append('<span style="color: %s; background-color: %s; border-radius: 3px; padding: 0 3px"><tt>%s</tt></span>'
                       % (ctx.theme.color("mdCode"), "#ffffff14", esc(code)))
        elif bold is not None:
            out.append("<b>%s</b>" % esc(bold))
        elif italic is not None:
            out.append("<i>%s</i>" % esc(italic))
        else:
            out.append('<a href="%s" style="color: %s">%s</a>' % (esc(url), ctx.theme.color("mdLink"), esc(label)))
        at = match.end()
    out.append(esc(text[at:]))
    return "".join(out)


def markdown_html(text, ctx):
    """Small markdown to HTML: headings, lists, quotes, fenced code, paragraphs."""
    blocks, lines, i = [], (text or "").split("\n"), 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            blocks.append(_code_block("\n".join(code), ctx))
        elif line.strip().startswith("|") and line.strip().endswith("|"):
            table = []
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                table.append(lines[i].strip())
                i += 1
            i -= 1
            blocks.append(_md_table(table, ctx))
        elif re.match(r"^#{1,6}\s", line):
            blocks.append('<div style="color: %s; font-weight: bold; margin-top: 6px">%s</div>'
                          % (ctx.theme.color("mdHeading"), _md_inline(line.lstrip("# ").strip(), ctx)))
        elif re.match(r"^\s*([-*+]|\d+\.)\s", line):
            bullet = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", line)
            blocks.append('<div style="padding-left: 14px"><span style="color: %s">•</span> %s</div>'
                          % (ctx.theme.color("mdListBullet"), _md_inline(bullet, ctx)))
        elif line.startswith(">"):
            blocks.append('<div style="border-left: 3px solid %s; padding-left: 8px; color: %s">%s</div>'
                          % (ctx.theme.color("mdQuoteBorder"), ctx.theme.color("mdQuote"), _md_inline(line.lstrip("> "), ctx)))
        elif line.strip():
            blocks.append("<div>%s</div>" % _md_inline(line, ctx))
        else:
            blocks.append('<div style="font-size: 0.4rem">&nbsp;</div>')
        i += 1
    return "".join(blocks)


def _md_table(rows, ctx):
    """A markdown table (lines like | a | b |) as aligned monospace columns, the header coloured."""
    cells = []
    for row in rows:
        parts = [part.strip() for part in row.strip("|").split("|")]
        if all(re.match(r"^:?-{2,}:?$", part) for part in parts):
            continue                      # the |---|---| divider line
        cells.append([re.sub(r"[`*]", "", part) for part in parts])
    if not cells:
        return ""
    width = max(len(r) for r in cells)
    cells = [r + [""] * (width - len(r)) for r in cells]
    sizes = [max(len(r[i]) for r in cells) for i in range(width)]
    out = []
    for n, row in enumerate(cells):
        line = "&nbsp;&nbsp;&nbsp;".join(esc(c.ljust(sizes[i])).replace(" ", "&nbsp;") for i, c in enumerate(row))
        color = ctx.theme.color("accent") if n == 0 else ctx.theme.color("text")
        weight = "font-weight: bold; " if n == 0 else ""
        out.append('<div style="%scolor: %s"><tt>%s</tt></div>' % (weight, color, line))
    return '<div style="margin: 4px 0; padding: 4px 8px; background-color: %s; border-radius: 6px">%s</div>' % (
        ctx.theme.color("pageBg"), "".join(out))


def _draw_md(node, ctx):
    return markdown_html((node.get("p") or {}).get("text"), ctx)


def _code_block(code, ctx, numbers=False, start=1):
    lines = code.split("\n")
    if numbers:
        width = len(str(start + len(lines)))
        lines = ['<span style="color: %s">%s</span>  %s' % (ctx.theme.color("dim"), str(start + n).rjust(width).replace(" ", "&nbsp;"), esc(l))
                 for n, l in enumerate(lines)]
    else:
        lines = [esc(l) for l in lines]
    body = "<br>".join(line.replace(" ", "&nbsp;") if "<" not in line else line for line in lines)
    return ('<div style="background-color: %s; border: 1px solid %s; border-radius: 6px; padding: 6px 10px; margin: 4px 0; color: %s">'
            '<tt>%s</tt></div>' % (ctx.theme.color("pageBg"), ctx.theme.color("mdCodeBlockBorder"), ctx.theme.color("mdCodeBlock"), body))


def _draw_code(node, ctx):
    props = node.get("p") or {}
    return _code_block(props.get("text") or "", ctx, bool(props.get("numbers")), props.get("start") or 1)


def _draw_diff(node, ctx):
    text = (node.get("p") or {}).get("text") or ""
    rows = []
    for line in text.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            style = "background-color: %s; color: %s" % (_hex_with_alpha(ctx.theme.color("toolDiffAdded"), 0.14), ctx.theme.color("toolDiffAdded"))
        elif line.startswith("-") and not line.startswith("---"):
            style = "background-color: %s; color: %s" % (_hex_with_alpha(ctx.theme.color("toolDiffRemoved"), 0.14), ctx.theme.color("toolDiffRemoved"))
        else:
            style = "color: %s" % ctx.theme.color("toolDiffContext")
        rows.append('<div style="%s; padding: 0 8px"><tt>%s</tt></div>' % (style, esc(line).replace(" ", "&nbsp;")))
    return '<div style="margin: 4px 0">%s</div>' % "".join(rows)


# ANSI colour support for `ansi` nodes and `rows` fallback.
_ANSI16 = ["#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
           "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff"]
_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_ANSI_STRIP = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def ansi_html(text, ctx):
    """SGR colours/bold -> spans; other escapes are dropped.  \\r\\n -> line breaks."""
    text = _ANSI_STRIP.sub("", (text or "").replace("\r\n", "\n"))
    out, color, bold, at = [], None, False, 0

    def emit(segment):
        if not segment:
            return
        body = esc(segment).replace(" ", "&nbsp;").replace("\n", "<br>")
        style = ("color: %s; " % color if color else "") + ("font-weight: bold" if bold else "")
        out.append('<span style="%s">%s</span>' % (style, body) if style else body)

    for match in _SGR.finditer(text):
        emit(text[at:match.start()])
        at = match.end()
        codes = [int(c) if c else 0 for c in match.group(1).split(";")]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code == 0:
                color, bold = None, False
            elif code == 1:
                bold = True
            elif 30 <= code <= 37:
                color = _ANSI16[code - 30]
            elif 90 <= code <= 97:
                color = _ANSI16[code - 90 + 8]
            elif code == 39:
                color = None
            elif code == 38 and i + 4 < len(codes) + 0 and codes[i + 1] == 2:
                color = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5])
                i += 4
            elif code == 38 and i + 2 < len(codes) + 1 and codes[i + 1] == 5:
                index = codes[i + 2]
                color = _ANSI16[index] if index < 16 else "#c0c0c0"
                i += 2
            i += 1
    emit(text[at:])
    return "".join(out)


def _draw_ansi(node, ctx):
    body = ansi_html((node.get("p") or {}).get("text"), ctx)
    return '<div style="color: %s"><tt>%s</tt></div>' % (ctx.theme.color("toolOutput", ctx.theme.color("muted")), body)


def _draw_rows(node, ctx):
    lines = (node.get("p") or {}).get("lines") or []
    return '<div><tt>%s</tt></div>' % "<br>".join(ansi_html(line, ctx) for line in lines)


def _draw_math(node, ctx):
    return '<div style="color: %s"><tt>%s</tt></div>' % (ctx.theme.color("warning"), esc((node.get("p") or {}).get("text") or ""))


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


def _image_info(base64_text):
    """(mime type, width, height) of a PNG, GIF or JPEG given as base64, or None."""
    try:
        data = base64.b64decode(base64_text[:4096] + "=" * (-len(base64_text[:4096]) % 4))
    except ValueError:
        return None
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
    return None


def _draw_image(node, ctx):
    props = node.get("p") or {}
    if props.get("builtin") == "omp":
        return '<img src="%s" width="60" height="25">' % _omp_logo_url()
    blob = ctx.blobs.get(props.get("blob") or "")
    info = _image_info(blob) if blob else None
    if info and info[1] > 0 and info[2] > 0:
        mime, width, height = info
        scale = min(1.0, 480.0 / width, 320.0 / height)
        return '<span><img src="data:%s;base64,%s" width="%d" height="%d"></span>' % (
            mime, blob, max(1, int(width * scale)), max(1, int(height * scale)))
    label = props.get("alt") or props.get("builtin") or "image"
    return '<span style="color: %s">image: %s</span>' % (ctx.theme.color("accent"), esc(label))


def _draw_kv(node, ctx):
    rows = []
    for item in (node.get("p") or {}).get("items") or []:
        rows.append('<div><span style="color: %s">%s</span>&nbsp;&nbsp;%s</div>'
                    % (ctx.theme.color("muted"), text_html(item.get("k"), ctx), text_html(item.get("v"), ctx)))
    return "".join(rows)


def _draw_table(node, ctx):
    props = node.get("p") or {}
    columns = props.get("cols") or []
    cells = [[plain_text(col.get("head")) or col.get("id", "") for col in columns]]
    for row in props.get("rows") or []:
        cells.append([plain_text((row.get("cells") or {}).get(col.get("id"))) for col in columns])
    widths = [max(len(r[i]) for r in cells) for i in range(len(columns))]
    lines = []
    for n, row in enumerate(cells):
        line = "&nbsp;&nbsp;".join(esc(c.ljust(widths[i])).replace(" ", "&nbsp;") for i, c in enumerate(row))
        color = ctx.theme.color("accent") if n == 0 else ctx.theme.color("text")
        lines.append('<div style="color: %s"><tt>%s</tt></div>' % (color, line))
    return '<div style="margin: 4px 0">%s</div>' % "".join(lines)


def plain_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return "".join(span.get("t", "") for span in value if isinstance(span, dict))


def _draw_tree_nodes(nodes, depth, ctx):
    out = []
    for item in nodes:
        out.append('<div style="padding-left: %dpx">%s</div>' % (depth * 14, text_html(item.get("label"), ctx)))
        out.append(_draw_tree_nodes(item.get("children") or [], depth + 1, ctx))
    return "".join(out)


def _draw_tree(node, ctx):
    return _draw_tree_nodes((node.get("p") or {}).get("nodes") or [], 0, ctx)


def _draw_badge(node, ctx):
    return chip((node.get("p") or {}).get("text", ""), ctx)


def _draw_kbd(node, ctx):
    return keycap((node.get("p") or {}).get("keys") or [], ctx)


def _draw_icon(node, ctx):
    name = (node.get("p") or {}).get("name", "")
    return '<span style="color: %s">%s</span>' % (ctx.theme.color("accent"), ICONS.get(name, "•"))


SPINNER_FRAMES = {
    "braille": "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f",
    "dots": "\u2022\u25cf\u2022\u00b7",
    "starburst": "\u2736\u2737\u2738\u2739\u2738\u2737",
    "orbit": "\u25dc\u25dd\u25de\u25df",
}


def _draw_spinner(node, ctx):
    props = node.get("p") or {}
    frames = SPINNER_FRAMES.get(props.get("style") or "braille", SPINNER_FRAMES["braille"])
    ctx.animated = True
    return '<span style="color: %s">%s</span> %s' % (
        ctx.theme.color("accent"), frames[ctx.clock.spinner_index(len(frames))], text_html(props.get("label"), ctx))


def _draw_shimmer(node, ctx):
    props = node.get("p") or {}
    return '<span style="color: %s">%s</span>' % (ctx.theme.color("accent"), esc(props.get("text") or plain_text(props.get("spans"))))


def _draw_elapsed(node, ctx):
    props = node.get("p") or {}
    if props.get("stopped") is not None:
        value = props["stopped"]
    else:
        value = ctx.clock.elapsed_ms(node.get("id"), props.get("age"))
        ctx.animated = True
    return '<span style="color: %s">%s</span>' % (ctx.theme.color("dim"), _format_age(value))


def _draw_progress(node, ctx):
    props = node.get("p") or {}
    value = props.get("value")
    percent = "" if value is None else " %d%%" % round(value * 100)
    return '<div>%s <span style="color: %s">%s</span> %s</div>' % (
        bar(value, ctx), ctx.theme.color("muted"), percent, text_html(props.get("label"), ctx))


def _draw_meter(node, ctx):
    props = node.get("p") or {}
    value = props.get("value")
    tone = "accent"
    thresholds = props.get("thresholds") or {}
    if value is not None and thresholds.get("bad") is not None and value >= thresholds["bad"]:
        tone = "error"
    elif value is not None and thresholds.get("warn") is not None and value >= thresholds["warn"]:
        tone = "warning"
    return '<span>%s <span style="color: %s">%s</span></span>' % (
        bar(value, ctx, tone, 90), ctx.theme.color("muted"), text_html(props.get("label"), ctx))


def _draw_rate(node, ctx):
    props = node.get("p") or {}
    return '<span style="color: %s">%s %s</span>' % (ctx.theme.color("dim"), props.get("value"), esc(props.get("unit", "")))


def _draw_list(node, ctx):
    selected = (node.get("p") or {}).get("selected")
    rows = []
    for child in node.get("c") or []:
        item = child.get("p") or {}
        on = child.get("id") == selected
        style = "background-color: %s; border-radius: 4px; " % ctx.theme.color("selectedBg") if on else ""
        line = '<span style="color: %s">%s</span> %s <span style="color: %s">%s</span>' % (
            ctx.theme.color("accent"), "\u203a" if on else "&nbsp;", text_html(item.get("label"), ctx),
            ctx.theme.color("dim"), text_html(item.get("detail"), ctx))
        rows.append('<div style="%spadding: 1px 8px">%s</div>' % (style, _item_link(ctx, node, child.get("id"), "activate", line)))
    return "".join(rows)


def _draw_item(node, ctx):
    item = node.get("p") or {}
    return '<div style="padding: 1px 8px">%s <span style="color: %s">%s</span></div>' % (
        text_html(item.get("label"), ctx), ctx.theme.color("dim"), text_html(item.get("detail"), ctx))


def _draw_tabs(node, ctx):
    props = node.get("p") or {}
    parts = []
    for item in props.get("items") or []:
        on = item.get("id") == props.get("active")
        chip_html = ('<span style="background-color: %s; color: %s; border-radius: 6px; padding: 2px 10px">%s</span>'
                     % (ctx.theme.color("selectedBg") if on else "transparent", ctx.theme.color("accent" if on else "muted"),
                        text_html(item.get("label"), ctx)))
        parts.append(_item_link(ctx, node, item.get("id"), "select", chip_html) + "&nbsp;")
    return "<div>%s</div>" % "".join(parts)


def _draw_editor(node, ctx):
    """omp's text editor: the text with omp's own decorations, selection and caret, then its inline completion.

    Everything comes from the node: ``decor`` (ranges with a style: ``hide`` leaves the characters out, ``mark``
    highlights, ``typo`` underlines, a colour name colours), ``anchor`` (selection from there to the caret),
    ``ghost`` (completion suffix after the caret), ``lang`` (code: mono face), ``prompt`` and ``mode`` (labels).
    Offsets are UTF-16 offsets in omp; they are used here as string positions, which is the same for the
    ordinary text typed here (an emoji counts two in omp and one here, shifting later decorations by one).
    """
    props = node.get("p") or {}
    theme = ctx.theme
    text = props.get("text") or ""
    cursor = props.get("cursor")
    caret = '<span style="color: %s">%s</span>' % (theme.color("accent"), "&#9608;" if not text else "&#9615;")
    if not text:
        body = caret + '<span style="color: %s">%s</span>' % (theme.color("dim"), esc(props.get("placeholder") or ""))
    else:
        if not (isinstance(cursor, int) and 0 <= cursor <= len(text)):
            cursor = None
        anchor = props.get("anchor")
        selected = None
        if cursor is not None and isinstance(anchor, int) and 0 <= anchor <= len(text) and anchor != cursor:
            selected = (min(anchor, cursor), max(anchor, cursor))
        decor = [d for d in props.get("decor") or [] if isinstance(d, dict) and isinstance(d.get("from"), int) and isinstance(d.get("to"), int)]
        cuts = {0, len(text)}
        for d in decor:
            cuts.update((max(0, d["from"]), min(len(text), d["to"])))
        if selected:
            cuts.update(selected)
        if cursor is not None:
            cuts.add(cursor)
        points = sorted(cuts)
        pieces = []
        for start, end in zip(points, points[1:]):
            if start == cursor:
                pieces.append(caret)
            covering = [d for d in decor if d["from"] <= start and end <= d["to"]]
            tokens = " ".join(str(d.get("s") or "") for d in covering)
            if "hide" in tokens.split():
                continue                        # omp asked for these characters not to be shown
            if selected and selected[0] <= start and end <= selected[1]:
                tokens += " mark"
            pieces.append(_span_html(text[start:end], tokens.strip(), ctx))
        if cursor == len(text):
            pieces.append(caret)
        body = "".join(pieces)
        if props.get("lang"):
            body = "<tt>%s</tt>" % body
        body += '<span style="color: %s">%s</span>' % (theme.color("dim"), esc(props.get("ghost") or ""))
    label = ""
    if props.get("prompt"):
        label += '<span style="color: %s">%s</span> ' % (theme.color("accent"), text_html(props["prompt"], ctx))
    if props.get("mode"):
        label += chip(props["mode"], ctx, "accent") + " "
    return "<div>%s%s</div>" % (label, body.replace("\n", "<br>"))


def _draw_composer_box(node, ctx):
    """The omp editor region as omp described it: a bordered box holding its own rows."""
    return ('<div style="background-color: %s; border: 1px solid %s; border-radius: 10px; padding: 8px 12px; margin-top: 8px">%s</div>'
            % (ctx.theme.color("pageBg"), ctx.theme.color("borderAccent"), _children(node, ctx)))


def _draw_status(node, ctx):
    segs = [draw(child, ctx) for child in node.get("c") or [] if child.get("k") == "seg"]
    segs = [s for s in segs if s]
    return '<div style="color: %s">%s</div>' % (ctx.theme.color("muted"), " &middot; ".join(segs)) if segs else ""


def _draw_seg(node, ctx):
    return text_html((node.get("p") or {}).get("spans"), ctx)


def _draw_toast(node, ctx):
    props = node.get("p") or {}
    ttl = props.get("ttl")
    if isinstance(ttl, (int, float)) and ttl > 0:
        if ctx.clock.visible_ms(node.get("id")) > ttl:
            return ""               # its time is up
        ctx.animated = True         # keep redrawing until it expires
    return ('<div style="background-color: %s; color: %s; border: 1px solid %s; border-radius: 8px; padding: 4px 10px; margin: 4px 0">%s</div>'
            % (ctx.theme.color("infoBg"), ctx.theme.color("muted"), ctx.theme.color("borderMuted"), esc(props.get("text", ""))))


def _draw_overlay(node, ctx):
    head = text_html((node.get("p") or {}).get("head"), ctx)
    inner = ('<div style="font-weight: bold">%s</div>' % head if head else "") + _children(node, ctx)
    return _card_shell(inner, ctx, "cardBg", "borderAccent")


def _action_link(ctx, owner, act, value, inner_html):
    """Clickable inline content that sends an `action` event (scope, tab, strip, button)."""
    if ctx.surface_id is None:
        return inner_html
    event = {"ev": "action", "sf": ctx.surface_id, "id": owner.get("id"), "act": act}
    if value is not None:
        event["value"] = value
    return '<a href="%s" style="text-decoration: none; color: %s">%s</a>' % (
        event_href(event), ctx.theme.color("text"), inner_html)


def _mark_chip(mark, ctx):
    """A small coloured tile with initials (provider or avatar mark); its colour comes from the seed."""
    text = (mark.get("text") or "")[:2]
    seed = mark.get("seed") or text
    hue = sum(ord(char) * (n + 1) for n, char in enumerate(seed)) % 360
    return ('<span style="background-color: hsl(%d, 45%%, 32%%); color: #ffffff; border-radius: 4px; '
            'padding: 0 5px; font-size: 0.8rem">%s</span>' % (hue, esc(text)))


def _hit_html(text, ranges, ctx):
    """``text`` with the search-hit ranges highlighted."""
    out, at = [], 0
    for start, end in sorted((r[0], r[1]) for r in ranges or [] if len(r) >= 2):
        if start < at:
            continue
        out.append(esc(text[at:start]))
        out.append('<span style="color: %s; font-weight: bold">%s</span>' % (ctx.theme.color("accent"), esc(text[start:end])))
        at = end
    out.append(esc(text[at:]))
    return "".join(out)


def _fact_html(value, fmt, ctx):
    if fmt == "bar" and isinstance(value, (int, float)):
        return bar(value, ctx, "accent", 40, 5)
    if fmt == "elapsed" and isinstance(value, (int, float)):
        return _format_age(value)
    if isinstance(value, (int, float)):
        if abs(value) >= 1000000:
            return "%gM" % round(value / 1000000.0, 1)
        if abs(value) >= 1000:
            return "%dK" % round(value / 1000.0)
        return "%g" % value
    return esc(plain_text(value))


def _role_chip(chip_data, ctx):
    """A role chip after a model's name (default / smol / ...): lit when it is the current assignment."""
    dot = chip_data.get("dot")
    color = ctx.theme.color("accent" if chip_data.get("on") else "dim")
    lead = '<span style="color: %s">&#9679;</span> ' % ctx.theme.color(dot) if dot else ""
    style = "font-style: italic; " if chip_data.get("auto") else ""
    return ('<span style="%sbackground-color: %s; color: %s; border-radius: 9px; padding: 1px 8px; font-size: 0.85rem">%s%s</span>'
            % (style, _hex_with_alpha(color, 0.16), color, lead, esc(chip_data.get("text", ""))))


def _soft_selection(ctx):
    """A calm highlight for the selected row: the accent colour at low strength, readable under any text."""
    return _hex_with_alpha(ctx.theme.color("accent"), 0.20)


def _pad_to_width(inner_html, ctx):
    """Add spaces after a row's content so the link under it covers the whole row, not only its text."""
    visible = len(re.sub(r"<[^>]+>|&[#a-zA-Z0-9]+;", "x", inner_html))
    return inner_html + "&nbsp;" * max(0, ctx.cols - 6 - visible)


def _local_link(ctx, key, value, inner_html):
    """Inline content that changes only what this view shows (nothing is sent to the program)."""
    event = {"local": "set", "key": key, "value": value}
    return '<a href="%s" style="text-decoration: none; color: %s">%s</a>' % (
        event_href(event), ctx.theme.color("accent"), inner_html)


def _model_name(label, ctx):
    """A machine name 'provider/model': the provider dim, the model itself strong."""
    if "/" not in label:
        return "<b>%s</b>" % esc(label)
    prefix, _, tail = label.rpartition("/")
    return '<span style="color: %s">%s/</span><b>%s</b>' % (ctx.theme.color("dim"), esc(prefix), esc(tail))


def _picker_row(node, ctx, item, columns, selected, current, hits):
    """One choice: a marker, its name, the roles it has, and the two facts people look at."""
    item_id = item.get("id")
    on = item_id == selected
    label = plain_text(item.get("label"))
    ranges = (hits or {}).get(item_id) or item.get("hits")
    if ranges:
        name = _hit_html(label, ranges, ctx)
    elif item.get("mono"):
        name = _model_name(label, ctx)
    else:
        name = text_html(item.get("label"), ctx)
    marker = '<span style="color: %s">%s</span>' % (ctx.theme.color("accent"), "&#9656;" if on else "&nbsp;")
    check = ' <span style="color: %s">&#10003; in use</span>' % ctx.theme.color("success") if item_id in current else ""
    extras = []
    for role_chip in item.get("chips") or []:
        if role_chip.get("on"):              # only the roles this model really has
            extras.append(_role_chip(role_chip, ctx))
    for badge in item.get("badges") or []:
        extras.append(chip(badge.get("text", ""), ctx, badge.get("tone") or "accent"))
    facts = item.get("facts") or {}
    shown = []
    has_free_badge = any(str(b.get("text", "")).lower() == "free" for b in item.get("badges") or [])
    for wanted in ("ctx", "price"):
        if wanted == "price" and has_free_badge and str(facts.get("price", "")).lower() == "free":
            continue
        if wanted in facts:
            fmt = next((c.get("format") for c in columns if c.get("id") == wanted), None)
            shown.append(_fact_html(facts[wanted], fmt, ctx) + (" context" if wanted == "ctx" else ""))
    facts_html = '<span style="color: %s">%s</span>' % (ctx.theme.color("dim"), " &middot; ".join(shown)) if shown else ""
    detail = '<span style="color: %s">%s</span>' % (ctx.theme.color("dim"), text_html(item.get("detail"), ctx)) if item.get("detail") else ""
    inner = marker + " " + name + check + ("&nbsp; " + "&nbsp;".join(extras) if extras else "") + ("&nbsp;&nbsp;" + facts_html if facts_html else "") + ("&nbsp;&nbsp;" + detail if detail else "")
    if item.get("disabled"):
        inner = '<span style="color: %s">%s</span>' % (ctx.theme.color("dim"), inner)
    else:
        inner = _item_link(ctx, node, item_id, "activate" if on else "select", _pad_to_width(inner, ctx))
    background = "background-color: %s; border-radius: 4px; " % _soft_selection(ctx) if on else ""
    return '<div style="%spadding: 2px 8px">%s</div>' % (background, inner)


def _scope_chip(node, ctx, scope, chosen):
    on = scope.get("id") == chosen
    dot = '<span style="color: %s">&#9679;</span> ' % ctx.theme.color(scope["dot"]) if scope.get("dot") else ""
    count = ' <span style="color: %s">%s</span>' % (ctx.theme.color("dim"), scope["count"]) if scope.get("count") is not None else ""
    body = ('<span style="background-color: %s; color: %s; border-radius: 6px; padding: 1px 8px">%s%s%s</span>&nbsp; '
            % (_soft_selection(ctx) if on else "transparent", ctx.theme.color("accent" if on else "muted"),
               dot, text_html(scope.get("label"), ctx), count))
    return body if scope.get("disabled") else _action_link(ctx, node, "scope", scope.get("id"), body)


def _picker_scopes(node, ctx, scopes, chosen):
    """Provider choices: the ones you can use, and the rest behind one small link."""
    usable = [s for s in scopes if s.get("group") != "Not signed in"]
    unsigned = [s for s in scopes if s.get("group") == "Not signed in"]
    out = ["".join(_scope_chip(node, ctx, s, chosen) for s in usable)]
    if unsigned:
        # The group's own name from omp (scope.group); only the fold-away of the group is GhostShell's.
        group = esc(unsigned[0].get("group") or "")
        key = "unsigned:%s" % node.get("id")
        if ctx.local.get(key):
            out.append(_local_link(ctx, key, False, "&#8722; %s" % group))
            out.append('<div style="margin-top: 2px">%s</div>' % "".join(_scope_chip(node, ctx, s, chosen) for s in unsigned))
        else:
            out.append(_local_link(ctx, key, True, "&#43; %s (%d)" % (group, len(unsigned))))
    return '<div style="margin: 2px 0 6px 0">%s</div>' % "".join(out)


def _picker_button(node, ctx, action):
    keys = " " + "".join(keycap([key], ctx) for key in action.get("keys") or [])
    if action.get("primary"):
        look = "background-color: %s; color: %s" % (ctx.theme.color("accent"), ctx.theme.color("pageBg"))
    elif action.get("danger"):
        look = "background-color: %s; color: %s" % (_hex_with_alpha(ctx.theme.color("error"), 0.2), ctx.theme.color("error"))
    else:
        look = "background-color: %s; color: %s" % (_soft_selection(ctx), ctx.theme.color("text"))
    body = '<span style="%s; border-radius: 6px; padding: 3px 12px">%s</span>%s&nbsp;&nbsp;' % (look, esc(action.get("label", "")), keys)
    return body if action.get("disabled") else _action_link(ctx, node, action.get("id"), None, body)


PICKER_ROWS = 10
PICKER_MORE = 20


def _picker_entries(props):
    items = [item for item in props.get("items") or [] if isinstance(item, dict)]
    by_id = {item.get("id"): item for item in items}
    order = props.get("order")
    entries = []
    for entry in (order if order is not None else [item.get("id") for item in items]):
        if isinstance(entry, dict) and "group" in entry:
            entries.append(("group", entry))
        elif entry in by_id:
            entries.append(("item", by_id[entry]))
    return entries


def _draw_picker(node, ctx):
    """A choice list a person can follow: a question, a few rows near the selection, and clear buttons."""
    props = node.get("p") or {}
    theme = ctx.theme
    parts = []
    entries = _picker_entries(props)
    shown_total = sum(1 for kind, _ in entries if kind == "item")
    total = props.get("total")
    # 1. what this is
    noun = props.get("noun") or "options"
    counts = "%s %s" % ("{:,}".format(shown_total), noun)
    if isinstance(total, int) and total != shown_total:
        counts += " (of {:,})".format(total)
    parts.append('<div><b style="color: %s">%s</b> &nbsp;<span style="color: %s">%s</span></div>'
                 % (theme.color("accent"), esc(props.get("title") or "Choose"), theme.color("dim"), esc(counts)))
    if props.get("subtitle"):
        parts.append('<div style="color: %s">%s</div>' % (theme.color("muted"), text_html(props["subtitle"], ctx)))
    # 2. search
    query = props.get("query")
    if query is not None:
        cursor = props.get("cursor")
        if query:
            if isinstance(cursor, int) and 0 <= cursor <= len(query):
                field = esc(query[:cursor]) + '<span style="color: %s">&#9615;</span>' % theme.color("accent") + esc(query[cursor:])
            else:
                field = esc(query)
        else:
            field = '<span style="color: %s">Type to search %s&#8230;</span>' % (theme.color("dim"), esc(noun))
        parts.append('<div style="background-color: %s; border-radius: 6px; padding: 3px 10px; margin: 4px 0">&#8981; %s</div>' % (theme.color("pageBg"), field))
    # 3. narrowing choices
    if props.get("scopes"):
        parts.append(_picker_scopes(node, ctx, props["scopes"], props.get("scope")))
    if props.get("tabs"):
        tabs = []
        for tab in props["tabs"]:
            on = tab.get("id") == props.get("tab")
            label = text_html(tab.get("label"), ctx)
            body = ('<span style="background-color: %s; color: %s; border-radius: 6px; padding: 1px 8px">%s</span>&nbsp;'
                    % (_soft_selection(ctx) if on else "transparent", theme.color("accent" if on else "muted"), label))
            tabs.append(_action_link(ctx, node, "tab", tab.get("id"), body))
        parts.append('<div style="margin: 2px 0 6px 0">%s</div>' % "".join(tabs))
    # 4. state and the rows
    state = props.get("state")
    if state == "loading":
        parts.append('<div style="color: %s">Loading&#8230;</div>' % theme.color("muted"))
    elif state == "error":
        parts.append('<div style="color: %s">%s</div>' % (theme.color("error"), text_html(props.get("message"), ctx)))
    if not entries and state != "loading":
        parts.append('<div style="color: %s">%s</div>' % (theme.color("dim"), text_html(props.get("empty") or "Nothing matches.", ctx)))
    selected, current = props.get("selected"), set(props.get("current") or [])
    columns = props.get("columns") or []
    limit = ctx.local.get("rows:%s" % node.get("id")) or PICKER_ROWS
    position_of_selected = next((n for n, (kind, entry) in enumerate(entries) if kind == "item" and entry.get("id") == selected), 0)
    start = max(0, min(position_of_selected - limit // 2, len(entries) - limit))
    window = entries[start:start + limit]
    if start:
        parts.append('<div style="color: %s">&#8593; %d more above</div>' % (theme.color("dim"), start))
    for kind, entry in window:
        if kind == "group":
            parts.append('<div style="color: %s; font-weight: bold; margin-top: 4px">%s</div>' % (theme.color("muted"), text_html(entry.get("label"), ctx)))
        else:
            parts.append(_picker_row(node, ctx, entry, columns, selected, current, props.get("hits")))
    below = len(entries) - start - len(window)
    if below > 0:
        more = _local_link(ctx, "rows:%s" % node.get("id"), limit + PICKER_MORE, "show %d more rows" % PICKER_MORE)
        parts.append('<div style="color: %s">&#8595; %d more below &nbsp;%s</div>' % (theme.color("dim"), below, more))
    # 5. details of the selected row, only when short
    if props.get("preview") != "none" and node.get("c"):
        preview = _children(node, ctx)
        if preview.count("<div") <= 8:
            parts.append('<div style="border-top: 1px solid %s; margin-top: 6px; padding-top: 4px">%s</div>' % (theme.color("border"), preview))
    # 6. role strip, confirmation
    strip = props.get("strip")
    if strip:
        chips = "".join(_action_link(ctx, node, "strip", s.get("id"), chip(plain_text(s.get("label")), ctx, "accent" if s.get("on") else "dim") + "&nbsp;")
                        for s in strip.get("items") or [])
        parts.append('<div style="margin-top: 4px">%s %s</div>' % (text_html(strip.get("label"), ctx), chips))
    confirm = props.get("confirm")
    if confirm:
        parts.append('<div style="background-color: %s; border-radius: 6px; padding: 4px 8px; margin-top: 4px">%s &nbsp;%s</div>' % (
            _hex_with_alpha(theme.color("warning"), 0.16), text_html(confirm.get("text"), ctx),
            _action_link(ctx, node, confirm.get("act"), None, chip(confirm.get("label") or "Confirm", ctx, "warning"))))
    # 7. buttons and how to use it
    actions = props.get("actions") or []
    if actions:
        parts.append('<div style="margin-top: 8px">%s</div>' % "".join(_picker_button(node, ctx, action) for action in actions))
    hints = [(chr(8593) + chr(8595), "move"), ("Enter", "choose")] + ([("type", "search")] if query is not None else []) + [("Esc", "close")]
    parts.append('<div style="color: %s; margin-top: 6px">%s</div>' % (
        theme.color("dim"), " &nbsp; ".join(keycap([key], ctx) + " " + label for key, label in hints)))
    return _card_shell("".join(parts), ctx, "cardBg", "borderAccent")


def _prefs_value_label(options, value):
    """The label of the option whose value is ``value`` (or the value itself)."""
    for option in options or []:
        if option.get("value") == value:
            return option.get("label") or str(value)
    return str(value)


def _prefs_control_html(node, row, ctx, editing):
    """The control at the right of a settings row, drawn from the control's own data.

    A click sends omp what the pointer did: ``change`` for a switch (the opposite value),
    ``select`` of the row for anything omp edits itself (choice, number, text, list), and the
    ``action`` omp named for an action button.  Which of these omp acts on is UNVERIFIED for
    each control kind; omp decides, GhostShell only reports the click.
    """
    control = row.get("control") or {}
    kind = control.get("k")
    theme = ctx.theme
    surface, node_id, row_id = ctx.surface_id, node.get("id"), row.get("id")
    if kind == "switch":
        on = bool(control.get("on"))
        pill = chip("on" if on else "off", ctx, "success" if on else "dim")
        if surface is None:
            return pill
        event = {"ev": "change", "sf": surface, "id": node_id, "item": row_id, "value": not on}
        return '<a href="%s" style="text-decoration: none">%s</a>' % (event_href(event), pill)
    if kind == "action":
        return _action_link(ctx, node, control.get("act"), row_id, chip(control.get("label") or "Run", ctx, "accent"))
    if kind == "choice":
        label = _prefs_value_label(control.get("options"), control.get("value"))
        shown = "<tt>%s</tt>" % esc(label) if control.get("mono") else esc(label)
        text = '<span style="color: %s">%s</span> <span style="color: %s">&#9662;</span>' % (theme.color("accent"), shown, theme.color("dim"))
    elif kind == "number":
        value = control.get("value")
        labels = control.get("labels") or {}
        shown = labels.get(str(value)) if isinstance(labels, dict) else None
        if shown is None:
            shown = ("%g" % value if isinstance(value, (int, float)) else str(value)) + (" " + control["unit"] if control.get("unit") else "")
        text = '<span style="color: %s">%s</span>' % (theme.color("accent"), esc(shown))
    elif kind == "text":
        value = control.get("value") or ""
        if control.get("secret") and value:
            value = "•" * min(len(value), 12)
        shown = value or control.get("placeholder") or ""
        color = theme.color("accent" if value else "dim")
        text = '<span style="color: %s">%s</span>' % (color, "<tt>%s</tt>" % esc(shown) if control.get("mono") else esc(shown))
    elif kind == "keys":
        text = " ".join(keycap(keys, ctx) for keys in control.get("keys") or [])
    elif kind == "multi":
        labels = [_prefs_value_label(control.get("options"), value) for value in control.get("values") or []]
        text = " ".join(chip(label, ctx, "accent") for label in labels) or '<span style="color: %s">none</span>' % theme.color("dim")
    else:
        text = ""
    if editing and editing.get("row") == row_id:
        draft = editing.get("draft")
        text = '<span style="color: %s">%s&#9608;</span>' % (theme.color("accent"), esc(draft if draft is not None else "")) if draft is not None else text
    return text


def _draw_prefs(node, ctx):
    """omp's settings window: title, page tabs, the page's lead, sections of rows with their controls.

    Everything comes from the ``prefs`` node: ``pages`` (tabs, with changed counts), ``page`` (the open
    one), ``lead``, ``query`` (search text), ``sections`` (rows with label, hint, warning, changed dot and
    typed control), ``focus`` (the row omp's keys act on, drawn highlighted) and ``editing`` (the row
    being edited, with its draft).  Child nodes are previews and follow the rows.
    """
    props = node.get("p") or {}
    theme = ctx.theme
    surface, node_id = ctx.surface_id, node.get("id")
    parts = ['<div style="font-weight: bold; color: %s">%s</div>' % (theme.color("accent"), esc(props.get("title", "Settings")))]
    query = props.get("query")
    if query:
        parts.append('<div style="color: %s">search: <b>%s</b></div>' % (theme.color("muted"), esc(query)))
    tabs = []
    for page in props.get("pages") or []:
        label = esc(page.get("label") or page.get("id") or "")
        if page.get("changed"):
            label += ' <span style="color: %s">%d</span>' % (theme.color("accent"), page["changed"])
        if page.get("id") == props.get("page") and not query:
            tab = '<span style="background-color: %s; border-radius: 4px; padding: 1px 6px">%s</span>' % (_soft_selection(ctx), label)
        else:
            tab = '<span style="color: %s">%s</span>' % (theme.color("dim" if page.get("disabled") else "muted"), label)
        if surface is not None and not page.get("disabled"):
            tab = _item_link(ctx, node, page.get("id"), "select", tab)
        tabs.append(tab)
    if tabs:
        parts.append('<div style="margin: 4px 0">%s</div>' % " &nbsp; ".join(tabs))
    if props.get("lead") and not query:
        parts.append('<div style="color: %s">%s</div>' % (theme.color("muted"), esc(props["lead"])))
    focus, editing = props.get("focus"), props.get("editing")
    for section in props.get("sections") or []:
        parts.append('<div style="color: %s; margin-top: 8px; font-weight: bold">%s</div>' % (theme.color("muted"), esc(section.get("title", ""))))
        for row in section.get("rows") or []:
            on_row = row.get("id") == focus
            label = '<b>%s</b>' % esc(row.get("label", "")) if on_row else esc(row.get("label", ""))
            if row.get("changed"):
                label += ' <span style="color: %s" title="%s">&#9679;</span>' % (
                    theme.color("accent"), html.escape("default: %s" % (row.get("defaultLabel") or ""), quote=True))
            if row.get("disabled"):
                label = '<span style="color: %s">%s</span>' % (theme.color("dim"), label)
            marker = '<span style="color: %s">&#9656;</span>' % theme.color("accent") if on_row else "&nbsp;"
            left = marker + " " + label
            if surface is not None and not row.get("disabled"):
                left = _item_link(ctx, node, row.get("id"), "select", left)
            line = '%s &nbsp; %s' % (left, _prefs_control_html(node, row, ctx, editing))
            background = "background-color: %s; border-radius: 4px; " % _soft_selection(ctx) if on_row else ""
            parts.append('<div style="%spadding: 2px 8px">%s</div>' % (background, line))
            for note, tone in ((row.get("hint"), "dim"), (row.get("warning"), "warning"), (row.get("disabled"), "dim")):
                if note:
                    parts.append('<div style="color: %s; padding-left: 28px; font-size: 0.85rem">%s</div>' % (theme.color(tone), esc(note)))
    body = "".join(parts)
    if node.get("c"):
        body += _children(node, ctx)
    return _card_shell(body, ctx, "cardBg", "borderAccent" if focus else "border")


def _draw_tool(node, ctx):
    props = node.get("p") or {}
    mark, tone = STATUS_MARKS.get(props.get("status"), ("●", "dim"))
    status = props.get("status")
    background = {"done": "toolSuccessBg", "error": "toolErrorBg"}.get(status, "toolPendingBg")
    head = ['<span style="color: %s">%s</span>' % (ctx.theme.color(tone), mark),
            '<b>%s</b>' % text_html(props.get("title"), ctx)]
    if props.get("target"):
        head.append('<span style="color: %s"><tt>%s</tt></span>' % (ctx.theme.color("muted"), text_html(props["target"], ctx)))
    for meta in props.get("meta") or []:
        head.append('<span style="color: %s">%s</span>' % (ctx.theme.color("dim"), text_html(meta, ctx)))
    for badge in props.get("badges") or []:
        head.append(chip(badge.get("text", ""), ctx, badge.get("tone") or "accent"))
    if props.get("exit"):
        head.append(chip("exit %s" % props["exit"], ctx, "error"))
    took = props.get("took")
    if took is None and props.get("age") is not None:
        if status in ("running", "pending"):
            took = ctx.clock.elapsed_ms(node.get("id"), props["age"])
            ctx.animated = True
        else:
            took = props["age"]
    if took is not None:
        head.append('<span style="color: %s">%s</span>' % (ctx.theme.color("dim"), _format_age(took)))
    body = "" if props.get("collapsed") else _children(node, ctx)
    inner = '<div>%s</div>' % _toggle_link(node, ctx, " &nbsp;".join(head)) + (('<div style="margin-top: 4px">%s</div>' % body) if body else "")
    return _card_shell(inner, ctx, background, "border")


def _draw_checklist(node, ctx):
    marks = {"done": ("☑", "success"), "active": ("▶", "accent"), "blocked": ("⚠", "warning"),
             "dropped": ("☒", "dim"), "pending": ("☐", "muted")}
    rows = []
    for phase in (node.get("p") or {}).get("phases") or []:
        rows.append('<div style="font-weight: bold">%s</div>' % text_html(phase.get("title"), ctx))
        for item in phase.get("items") or []:
            mark, tone = marks.get(item.get("status"), marks["pending"])
            rows.append('<div style="padding-left: 12px"><span style="color: %s">%s</span> %s</div>'
                        % (ctx.theme.color(tone), mark, text_html(item.get("text"), ctx)))
    return "".join(rows)


def _draw_agent(node, ctx):
    props = node.get("p") or {}
    head = "<b>%s</b> %s %s" % (esc(props.get("name", "agent")), chip(props.get("status", ""), ctx), text_html(props.get("task"), ctx))
    return _card_shell("<div>%s</div>%s" % (head, _children(node, ctx)), ctx)


def _heat_color(value, ctx):
    if value is None:
        return ctx.theme.color("borderMuted")
    low, high = ctx.theme.color("borderMuted"), ctx.theme.color("success")
    mix = max(0.0, min(1.0, value))
    return "#" + "".join("%02x" % int(int(low[i:i + 2], 16) * (1 - mix) + int(high[i:i + 2], 16) * mix) for i in (1, 3, 5))


def _draw_chart(node, ctx):
    props = node.get("p") or {}
    if props.get("kind") == "heatmap" and props.get("cells"):
        rows = ["<div>%s</div>" % "".join('<img src="%s" width="9" height="9">&#8201;' % _png_pixel(_heat_color(v, ctx)) for v in row)
                for row in props["cells"]]
        return '<div style="margin: 4px 0">%s</div><div style="color: %s">%s</div>' % ("".join(rows), ctx.theme.color("muted"), text_html(props.get("summary"), ctx))
    series = props.get("series") or []
    peak = max([s.get("value", 0) for s in series] or [1]) or 1
    return "".join('<div>%s <span style="color: %s">%s</span></div>' % (bar(s.get("value", 0) / peak, ctx, "accent", 120), ctx.theme.color("muted"), esc(s.get("label", "")))
                   for s in series)


def _draw_effort(node, ctx):
    level = (node.get("p") or {}).get("level", "")
    rungs = {"off": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}.get(level, 0)
    return '<span style="color: %s">%s%s</span>' % (ctx.theme.color("accent"), "●" * rungs, "○" * (6 - rungs))


_DRAWERS = {
    "col": _draw_col, "row": _draw_row, "card": _draw_card, "section": _draw_section, "rule": _draw_rule,
    "spacer": _draw_spacer, "text": _draw_text, "md": _draw_md, "code": _draw_code, "diff": _draw_diff,
    "ansi": _draw_ansi, "math": _draw_math, "image": _draw_image, "kv": _draw_kv, "table": _draw_table,
    "tree": _draw_tree, "badge": _draw_badge, "kbd": _draw_kbd, "icon": _draw_icon, "spinner": _draw_spinner,
    "shimmer": _draw_shimmer, "elapsed": _draw_elapsed, "progress": _draw_progress, "rate": _draw_rate,
    "list": _draw_list, "item": _draw_item, "tabs": _draw_tabs, "editor": _draw_editor, "input": _draw_editor,
    "status": _draw_status, "seg": _draw_seg, "overlay": _draw_overlay, "toast": _draw_toast, "rows": _draw_rows,
    "picker": _draw_picker, "prefs": _draw_prefs, "tool": _draw_tool, "checklist": _draw_checklist,
    "agent": _draw_agent, "chart": _draw_chart, "meter": _draw_meter, "effort": _draw_effort,
}


def _draw_role_override(node, ctx):
    """Roles that need a different shape than their kind's default (omp.editor = the composer box)."""
    return None


def _has_role(node, role):
    """True when ``node`` or anything under it has the given role."""
    if (node.get("p") or {}).get("role") == role:
        return True
    return any(_has_role(child, role) for child in node.get("c") or [])


CAPTION_LENGTH = 90         # a caption is the first line of an entry's text, cut to fit one row


def entry_outline(node):
    """(kind, caption) for one top-level entry, for the tab's text line and its minimap mark.

    ``kind`` says what the entry is (user, error, tool, assistant, note, other; "" for spacing) so the
    minimap can colour it; ``caption`` is the first line of the entry's own text, cut short.
    """
    props = node.get("p") or {}
    role = str(props.get("role") or "")
    kind_name = node.get("k")
    if kind_name == "spacer":
        return "", ""
    if role.startswith("omp.user"):
        kind = "user"
    elif role.startswith("omp.error") or props.get("tone") == "error":
        kind = "error"
    elif kind_name == "tool" or role.startswith("omp.tool"):
        kind = "tool"
    elif role.startswith("omp.assistant"):
        kind = "assistant"
    elif kind_name == "toast" or role.startswith(("omp.welcome", "omp.recap", "omp.turn")):
        kind = "note"
    else:
        kind = "other"
    text = node_text(node)
    if not text.strip() and kind_name == "tool":
        text = "%s %s" % (plain_text(props.get("title")), plain_text(props.get("target")))
    caption = next((" ".join(line.split()) for line in text.splitlines() if line.strip()), "")
    return kind, caption[:CAPTION_LENGTH]


def render_regions(document_snapshot, palette_message, cols=100, clock=None, blobs=None, local=None):
    """HTML for each top-level region of a surface: {"main": [html per entry], "layer": [html per overlay], "dock": html}.

    ``main`` is a list so a viewer can update only the entries that changed; ``main_outline`` holds the
    ``entry_outline`` of each, in the same order.
    """
    ctx = Context(Theme(palette_message), cols, document_snapshot.get("id"), clock, blobs, local)
    regions = {"main": [], "main_outline": [], "layer": [], "under": 0, "dock": "", "animated": False}
    for region in document_snapshot.get("c") or []:
        if region.get("id") == "main":
            children = region.get("c") or []
            regions["main"] = [draw(child, ctx) for child in children]
            regions["main_outline"] = [entry_outline(child) for child in children]
        elif region.get("id") == "layer":
            overlays = region.get("c") or []
            # The layer is a stack: the last overlay is the one the program is showing.
            regions["layer"] = [draw(overlays[-1], ctx)] if overlays else []
            regions["under"] = max(0, len(overlays) - 1)
        elif region.get("id") == "dock":
            # The input box (omp.editor) always comes last, below everything else in the dock.
            dock_children = region.get("c") or []
            composer = [c for c in dock_children if _has_role(c, "omp.editor")]
            others = [c for c in dock_children if not _has_role(c, "omp.editor")]
            regions["dock"] = "".join(draw(child, ctx) for child in others + composer)
    regions["animated"] = ctx.animated
    return regions


def overlay_note(count, palette_message=None):
    """A line saying more overlays are open underneath the one being shown."""
    theme = Theme(palette_message)
    return ('<div style="color: %s; margin: 4px 0">%d more window%s open underneath this one. '
            'Esc closes this one first.</div>' % (theme.color("dim"), count, "" if count == 1 else "s"))


def page(parts, palette_message=None):
    """Wrap region HTML in a body with a base style, for one phantom."""
    theme = Theme(palette_message)
    return ('<body id="tsp-native"><style>body { color: %s; } tt { font-family: monospace }</style>'
            '<div style="padding: 0 12px">%s</div></body>' % (theme.color("text"), parts))
