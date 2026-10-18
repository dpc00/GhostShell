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
import functools
import html
import json
import re
import struct
import urllib.parse
import zlib

from . import tsp_style

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
        self.rules = ()                 # parsed program stylesheets (tsp_style), applied to el nodes
        self.ancestors = []             # the el nodes around the one being drawn, outermost first
        self.stack = []                 # every ancestor of the node being drawn (not itself), outermost first
        self.current = None             # the node whose drawer is running (its parent while a child draws)
        self.avail = max(12, cols - 4)  # cells a line may use here; a drawer that indents shrinks it for its children
        self.cell_px = 8                # width of one character cell in pixels, for sizing images and paddings
        self.reduce_motion = False      # the program asked for no motion
        self.focus = self.local.get("focus")    # id of the focused node
        self.surface_role = None        # the `o` role of the surface (data-surface of the regions)
        self.surface_app = None         # the hello `app` (data-app)
        self.cwd = None                 # the pane's working directory


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
    """A str or a list of {"t","s"} spans as inline HTML (the full span path lives in tsp_draw_text)."""
    from . import tsp_draw_text
    return tsp_draw_text.spans_html(value, ctx, base_tokens)


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


# ---- block helpers ---------------------------------------------------------------

def _children(node, ctx):
    return "".join(draw(child, ctx) for child in node.get("c") or [])


# ---- measuring and shaping HTML --------------------------------------------------------
#
# minihtml has no layout we can query and no flexbox, so rows, rules, clamps and status strips work from
# character cells: ``vis_len`` counts the cells an HTML run takes, ``measure_clip`` counts (and cuts to) lines,
# ``fit_width`` truncates a run with an ellipsis.  The font of a phantom is the view's own, normally monospace,
# so one cell is one character; images and horizontal paddings are converted with ``ctx.cell_px``.

_BLOCK_TAGS = ("div", "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "pre", "blockquote")
_VOID_TAGS = ("br", "img", "hr")
_BLOCK_OPEN = re.compile(r"<(?:div|p|h[1-6]|ul|ol|li|pre|blockquote)\b")
_TOKEN = re.compile(r"<[^>]*>|&#?\w+;|[^<&]+|&")
_TAG = re.compile(r"<[^>]*>")
_ENTITY = re.compile(r"&#?\w+;")
_TAG_NAME = re.compile(r"<(/?)([a-zA-Z0-9]+)")
_IMG_WIDTH = re.compile(r'<img\b[^>]*?\bwidth="(\d+)"')
_SPAN_STYLE = re.compile(r'<span\b[^>]*?\bstyle="([^"]*)"')
_PADDING = re.compile(r"padding(-left|-right)?\s*:\s*([^;]+)")
_PLAIN_DIV = re.compile(r"^<div([^>]*)>(.*)</div>$", re.S)
_ROOT_OPEN = re.compile(r"<(div|span)\b([^>]*)>")
_EXTENT = re.compile(r"(\d+(?:\.\d+)?)(ch|lines)")


def _padding_cells(style, cell_px):
    px = 0.0
    for match in _PADDING.finditer(style):
        numbers = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)px", match.group(2))]
        if match.group(1):
            px += numbers[0] if numbers else 0
        elif len(numbers) == 1:
            px += 2 * numbers[0]
        elif len(numbers) in (2, 3):
            px += 2 * numbers[1]
        elif len(numbers) == 4:
            px += numbers[1] + numbers[3]
    return int(px // cell_px)


def vis_len(html_text, cell_px=8):
    """Cells a run of inline HTML takes: its characters, its images and the horizontal padding of its spans."""
    cells = len(_ENTITY.sub("x", _TAG.sub("", html_text)))
    if "<img" in html_text:
        cells += sum(int(match.group(1)) // cell_px for match in _IMG_WIDTH.finditer(html_text))
    if "padding" in html_text:
        cells += sum(_padding_cells(match.group(1), cell_px) for match in _SPAN_STYLE.finditer(html_text))
    return cells


def _advance(lines, cur, chars, avail):
    """The (completed lines, cells on the open line) after ``chars`` more cells flow into a line ``avail`` wide."""
    full, rest = divmod(cur + chars, avail)
    if rest == 0 and full > 0:
        return lines + full - 1, avail
    return lines + full, rest


def measure_clip(html_text, avail, limit=None, cell_px=8):
    """(``html_text`` cut to ``limit`` lines when it is longer, the number of lines it takes in all).

    A line is a ``<br>``, a block boundary or ``avail`` cells of flowing text.  The cut keeps the tags
    balanced.  Without a ``limit`` the text comes back unchanged."""
    avail = max(1, int(avail))
    out, open_tags = [], []
    lines, cur, stopped = 0, 0, False
    for match in _TOKEN.finditer(html_text):
        token = match.group(0)
        if token[0] == "<":
            tag = _TAG_NAME.match(token)
            name = tag.group(2).lower() if tag else ""
            closing = bool(tag and tag.group(1))
            if name == "br":
                if limit is not None and lines >= limit:
                    stopped = True
                if not stopped:
                    out.append(token)
                lines, cur = lines + 1, 0
                continue
            if name == "img":
                width = re.search(r'width="(\d+)"', token)
                cells = max(1, int(width.group(1)) // cell_px) if width else 1
                if limit is not None and lines * avail + cur >= limit * avail:
                    stopped = True
                if not stopped:
                    out.append(token)
                lines, cur = _advance(lines, cur, cells, avail)
                continue
            if name in _BLOCK_TAGS and cur > 0:
                lines, cur = lines + 1, 0
            if not stopped:
                out.append(token)
                if name and name not in _VOID_TAGS and not token.endswith("/>"):
                    if not closing:
                        open_tags.append(name)
                    elif name in open_tags:
                        del open_tags[len(open_tags) - 1 - open_tags[::-1].index(name):]
            continue
        chars = 1 if token[0] == "&" else len(token)
        if not stopped and limit is not None:
            capacity = limit * avail - (lines * avail + cur)
            if capacity <= 0:
                stopped = True
            elif chars > capacity:
                out.append(token[:capacity])
                stopped = True
            else:
                out.append(token)
        elif not stopped:
            out.append(token)
        lines, cur = _advance(lines, cur, chars, avail)
    total = lines + (1 if cur > 0 else 0)
    if not stopped:
        return html_text, total
    while out and out[-1].lower().startswith("<br"):
        out.pop()       # a break that ends the cut adds no line
    out.extend("</%s>" % name for name in reversed(open_tags))
    return "".join(out), total


def fit_width(html_text, width, cell_px=8):
    """``html_text`` cut to ``width`` cells with an ellipsis (unchanged when it fits); tags stay balanced."""
    if vis_len(html_text, cell_px) <= width:
        return html_text
    keep = max(0, width - 1)
    out, open_tags, used = [], [], 0
    for match in _TOKEN.finditer(html_text):
        token = match.group(0)
        if token[0] == "<":
            tag = _TAG_NAME.match(token)
            name = tag.group(2).lower() if tag else ""
            if name == "img":
                size = re.search(r'width="(\d+)"', token)
                cells = max(1, int(size.group(1)) // cell_px) if size else 1
                if used + cells > keep:
                    break
                used += cells
                out.append(token)
                continue
            out.append(token)
            if name and name not in _VOID_TAGS and not token.endswith("/>"):
                if not tag.group(1):
                    open_tags.append(name)
                elif name in open_tags:
                    del open_tags[len(open_tags) - 1 - open_tags[::-1].index(name):]
            continue
        chars = 1 if token[0] == "&" else len(token)
        if used + chars > keep:
            if token[0] != "&" and keep - used > 0:
                out.append(token[:keep - used])
            break
        out.append(token)
        used += chars
    out.append("&#8230;")
    out.extend("</%s>" % name for name in reversed(open_tags))
    return "".join(out)


def has_block(html_text):
    """True when the HTML holds a block element anywhere (it cannot sit inside a line of text)."""
    return _BLOCK_OPEN.search(html_text) is not None


def starts_block(html_text):
    return _BLOCK_OPEN.match(html_text) is not None


def spanify(html_text):
    """One plain ``<div>`` of inline content as a ``<span>`` (it can then sit in a line), or None."""
    match = _PLAIN_DIV.match(html_text)
    if match is None or _BLOCK_OPEN.search(match.group(2)):
        return None
    return "<span%s>%s</span>" % (match.group(1), match.group(2))


def inline_of(html_text):
    """``html_text`` as inline content: unchanged when it already is, a span for a one-div block, else None."""
    if not has_block(html_text):
        return html_text
    return spanify(html_text)


def stack_blocks(parts, gap_px=0):
    """Block pieces one under another, ``gap_px`` apart; a piece that is inline gets a block of its own."""
    out = []
    for part in parts:
        if not part:
            continue
        if not starts_block(part.lstrip()):
            part = "<div>%s</div>" % part
        if out and gap_px:
            part = '<div style="margin-top: %dpx">%s</div>' % (gap_px, part)
        out.append(part)
    return "".join(out)


def extent(value):
    """A size prop (min/max w/h) as ("ch", n), ("lines", n), ("frac", f), or None when it is to be ignored."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return ("frac", float(value)) if value > 0 else None
    if isinstance(value, str):
        match = _EXTENT.fullmatch(value.strip())
        if match and float(match.group(1)) > 0:
            return (match.group(2), float(match.group(1)))
    return None


def extent_cells(value, parent_cells):
    """A width extent in cells (``parent_cells`` is the full width a fraction refers to), or None."""
    found = extent(value)
    if found is None or found[0] == "lines":
        return None
    return int(found[1] * parent_cells) if found[0] == "frac" else int(found[1])


def extent_lines(value):
    found = extent(value)
    return int(found[1]) if found is not None and found[0] == "lines" else None


# ---- folding (card, section and the kinds that fold like them) ----------------------------
#
# A fold flipped by the user is remembered here, in the view, by the node's key among its siblings (its id
# without a key); it wins until the program sends a different ``collapsed`` value.  A click both records the
# flip (a ``local`` ``set`` with ``value`` = {"collapsed", "seen"}) and tells the program (``send``).

def flippable(props):
    actions = props.get("actions")
    return bool(props.get("collapsible")) or (isinstance(actions, dict) and actions.get("click") == "toggle")


def fold_key(node, parent_id):
    key = (node.get("p") or {}).get("key")
    return "fold:%s/%s" % (parent_id or "", key) if key else "fold:%s" % node.get("id")


def _fold_state_under(node, parent_id, ctx):
    props = node.get("p") or {}
    if not flippable(props):
        return False
    base = bool(props.get("collapsed"))
    state = ctx.local.get(fold_key(node, parent_id))
    if isinstance(state, dict) and state.get("seen") == base:
        return bool(state.get("collapsed"))
    return base


def fold_state(node, ctx):
    """True when ``node`` (being drawn now) is folded: its ``collapsed`` prop, or the user's flip of it."""
    return _fold_state_under(node, ctx.stack[-1].get("id") if ctx.stack else "", ctx)


def fold_event(node, ctx):
    """The event of a click that flips ``node``: record the new state here and send ``toggle`` to the program."""
    props = node.get("p") or {}
    parent_id = ctx.stack[-1].get("id") if ctx.stack else ""
    base = bool(props.get("collapsed"))
    new = not _fold_state_under(node, parent_id, ctx)
    sent = {"ev": "toggle", "sf": ctx.surface_id, "id": node.get("id"), "collapsed": new}
    if props.get("key"):
        sent["key"] = props["key"]
    return {"local": "set", "key": fold_key(node, parent_id), "value": {"collapsed": new, "seen": base}, "send": sent}


def preview_gives_way(ctx):
    """True when the nearest folding ancestor of the node being drawn is open: its ``preview`` then does not apply."""
    for index in range(len(ctx.stack) - 1, -1, -1):
        ancestor = ctx.stack[index]
        if flippable(ancestor.get("p") or {}):
            parent_id = ctx.stack[index - 1].get("id") if index else ""
            return not _fold_state_under(ancestor, parent_id, ctx)
    return False


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
    """The event a click on ``node`` should send, or None when it is not clickable.

    editor and input draw their own focus link (a field that is read-only or already focused sends none)."""
    props = node.get("p") or {}
    actions = props.get("actions")
    click = actions.get("click") if isinstance(actions, dict) else None   # a picker's own "actions" is a list
    if not click:
        return None
    surface, node_id = ctx.surface_id, node.get("id")
    if click == "toggle":
        return fold_event(node, ctx)
    if click == "copy":
        return {"local": "copy", "text": node_text(node)}
    if click == "open":
        return {"local": "open", "href": props.get("href") or ""}
    if click in ("select", "activate"):
        return {"ev": click, "sf": surface, "id": node_id, "item": node_id}
    if click == "zoom":
        return None
    return {"ev": "action", "sf": surface, "id": node_id, "act": click}


_LEAF_DIV = re.compile(r"(<div\b[^>]*>)((?:(?!<div\b|</div>).)+)(</div>)", re.S)


def _link_wrap(html_text, href, ctx, title=None):
    """Make ``html_text`` clickable: a link around inline HTML, or inside a single block's content; a node
    made of nested blocks gets the link inside each innermost block (minihtml drops a link around a block).
    ``title`` is the program's own explanation of the click (shown when the pointer rests on it)."""
    tip = ' title="%s"' % html.escape(title, quote=True) if title else ""
    open_tag = '<a href="%s"%s style="text-decoration: none; color: %s">' % (href, tip, ctx.theme.color("text"))
    if html_text.startswith("<div") and html_text.endswith("</div>") and html_text.count("<div") == 1:
        end_of_tag = html_text.index(">") + 1
        return html_text[:end_of_tag] + open_tag + html_text[end_of_tag:-len("</div>")] + "</a></div>"
    if "<div" in html_text:
        def link_leaf(match):
            content = match.group(2)
            if "<a " in content or has_block(content):
                return match.group(0)       # it has a link of its own (the closer one wins), or a block inside
            return match.group(1) + open_tag + content + "</a>" + match.group(3)
        return _LEAF_DIV.sub(link_leaf, html_text)
    return open_tag + html_text + "</a>"


def _toggle_link(node, ctx, inner_html):
    """The head of a collapsible node, clickable to expand or collapse it."""
    props = node.get("p") or {}
    if not flippable(props) or ctx.surface_id is None:
        return inner_html
    return '<a href="%s" style="text-decoration: none; color: %s">%s</a>' % (
        event_href(fold_event(node, ctx)), ctx.theme.color("text"), inner_html)


def _item_link(ctx, owner, item_id, event_name, inner_html):
    """A list/picker/tab entry, clickable (a click does what Enter does)."""
    if ctx.surface_id is None:
        return inner_html
    event = {"ev": event_name, "sf": ctx.surface_id, "id": owner.get("id"), "item": item_id}
    return '<a href="%s" style="text-decoration: none; color: %s">%s</a>' % (
        event_href(event), ctx.theme.color("text"), inner_html)


_TITLE_IS_OWN = ("tool", "picker", "prefs")     # these kinds use `title` for something other than a tooltip
_TONES = ("neutral", "accent", "info", "success", "warning", "error", "pending", "muted", "user")


def _tooltip(node):
    title = (node.get("p") or {}).get("title")
    return title if isinstance(title, str) and title and node.get("k") not in _TITLE_IS_OWN else None


def _single_root(html_text):
    """The match of the opening tag of ``html_text`` when one div or span holds all of it, else None."""
    match = _ROOT_OPEN.match(html_text)
    if match is None:
        return None
    name = match.group(1)
    closing = "</%s>" % name
    if not html_text.endswith(closing):
        return None
    depth = 0
    last = len(html_text) - len(closing)
    for tag in re.finditer(r"<(/?)%s\b" % name, html_text):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return match if tag.start() == last else None
    return None


def _decorate(html_text, node, ctx, clip):
    """Give the root element of ``html_text`` the hooks every node has: classes sf, sf-<kind>, sf-act, sf-clip
    and the attributes data-id, data-role, data-tone, data-mark, data-href, title and aria-label."""
    props = node.get("p") or {}
    match = _single_root(html_text)
    if match is None:
        return html_text
    classes = ["sf", "sf-%s" % node.get("k")]
    actions = props.get("actions")
    if isinstance(actions, dict) and (actions.get("click") or actions.get("dblclick")):
        classes.append("sf-act")
    if clip:
        classes.append("sf-clip")
    rest = match.group(2)
    found = re.search(r'\bclass="([^"]*)"', rest)
    if found:
        rest = rest[:found.start()] + 'class="%s %s"' % (" ".join(classes), found.group(1)) + rest[found.end():]
    else:
        rest = ' class="%s"' % " ".join(classes) + rest
    extra = ['data-id="%s"' % html.escape(str(node.get("id")), quote=True)]
    for attribute, value in (("data-role", props.get("role")),
                             ("data-tone", props.get("tone") if props.get("tone") in _TONES else None),
                             ("data-mark", props.get("mark")),
                             ("data-href", props.get("href")),
                             ("title", _tooltip(node)),
                             ("aria-label", props.get("aria"))):
        if isinstance(value, str) and value and (" %s=" % attribute) not in rest:
            extra.append('%s="%s"' % (attribute, html.escape(value, quote=True)))
    mark = props.get("mark")
    parts = []
    if ctx.rules and node.get("k") not in ("el", "media"):
        try:
            from . import tsp_draw_el
            sheet = tsp_draw_el.style_for_node(node, ctx)
        except Exception as error:
            DRAW_ERRORS.append("style %s: %s: %s" % (node.get("id"), type(error).__name__, error))
            sheet = ""
        if sheet:
            parts.append(sheet.strip().rstrip(";"))
    if mark == "pick":
        parts.append("border-left: 3px solid %s; padding-left: 6px" % ctx.theme.color("accent"))
    elif mark == "drop":
        parts.append("color: %s" % ctx.theme.color("dim"))
    css = "; ".join(parts)
    if css:
        style = re.search(r'\bstyle="([^"]*)"', rest)
        if style:
            rest = rest[:style.start()] + 'style="%s; %s"' % (style.group(1), css) + rest[style.end():]
        else:
            extra.append('style="%s"' % css)
    return "<%s%s %s>%s" % (match.group(1), rest, " ".join(extra), html_text[match.end():])


def _apply_common(html_text, node, ctx):
    """The size and hook props every node takes, applied to what its drawer drew."""
    props = node.get("p") or {}
    biggest, smallest = props.get("max"), props.get("min")
    clip = False
    if isinstance(biggest, dict):
        lines = extent_lines(biggest.get("h"))
        if lines:
            clip = True
            if ("scrollinfo:%s" % node.get("id")) not in ctx.local:     # a scroller does its own windowing
                html_text = measure_clip(html_text, ctx.avail, lines, ctx.cell_px)[0]
    if isinstance(smallest, dict):
        lines = extent_lines(smallest.get("h"))
        if lines and starts_block(html_text) and html_text.endswith("</div>"):
            missing = lines - measure_clip(html_text, ctx.avail, None, ctx.cell_px)[1]
            if missing > 0:
                html_text = html_text[:-len("</div>")] + "<div>&nbsp;</div>" * missing + "</div>"
    return _decorate(html_text, node, ctx, clip)


def draw(node, ctx):
    """HTML for ``node`` and its children."""
    props = node.get("p") or {}
    if props.get("hidden"):
        return ""
    drawer = _DRAWERS.get(node.get("k"), _draw_default)
    parent = ctx.current
    if parent is not None:
        ctx.stack.append(parent)        # while a drawer runs, ctx.stack holds the ancestors of its node
    ctx.current = node
    try:
        html_text = drawer(node, ctx)
    except Exception as error:      # show the fault in place, keep drawing everything else
        DRAW_ERRORS.append("%s %s: %s: %s" % (node.get("k"), node.get("id"), type(error).__name__, error))
        del DRAW_ERRORS[:-50]       # keep the last 50, for diagnosis
        return ('<div style="background-color: %s; color: %s; border-radius: 6px; padding: 2px 8px; margin: 2px 0">'
                '&#9888; could not draw a "%s" element: %s</div>'
                % (_hex_with_alpha(ctx.theme.color("error"), 0.2), ctx.theme.color("error"),
                   esc(node.get("k", "?")), esc("%s: %s" % (type(error).__name__, error))))
    finally:
        ctx.current = parent
        if parent is not None:
            ctx.stack.pop()
    if html_text:
        html_text = _apply_common(html_text, node, ctx)
    event = None if node.get("k") in ("el", "media") else _click_event(node, ctx)     # the el drawer links its own leaves
    if event is not None and html_text:
        html_text = _link_wrap(html_text, event_href(event), ctx, _tooltip(node))
    return html_text


def _draw_default(node, ctx):
    return _children(node, ctx)


# Kinds that flow inline inside a row (the rest are blocks and become inline-blocks).
_INLINE_KINDS = ("text", "icon", "kbd", "badge", "elapsed", "rate", "meter", "progress", "effort",
                 "shimmer", "spinner", "image", "seg")


def _draw_rule(node, ctx):
    label = text_html((node.get("p") or {}).get("label"), ctx)
    line = '<div style="border-top: 1px solid %s; margin: 6px 0"></div>' % ctx.theme.color("border")
    return line if not label else '<div style="color: %s">%s</div>%s' % (ctx.theme.color("dim"), label, line)


DRAW_ERRORS = []        # the last elements that failed to draw ("kind id: error"), newest last

def _card_shell(inner, ctx, background="cardBg", border="border", accent_edge=None):
    edge = "border-left: 3px solid %s; " % ctx.theme.color(accent_edge) if accent_edge else ""
    return ('<div style="background-color: %s; border: 1px solid %s; %sborder-radius: 8px; '
            'padding: 8px 12px; margin: 5px 0">%s</div>'
            % (ctx.theme.color(background), ctx.theme.color(border), edge, inner))


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


# ANSI colour support for `ansi` nodes and `rows` fallback.
_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_ANSI_STRIP = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


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


def plain_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return "".join(span.get("t", "") for span in value if isinstance(span, dict))


def _draw_composer_box(node, ctx):
    """The omp editor region as omp described it: a bordered box holding its own rows."""
    return ('<div style="background-color: %s; border: 1px solid %s; border-radius: 10px; padding: 8px 12px; margin-top: 8px">%s</div>'
            % (ctx.theme.color("pageBg"), ctx.theme.color("borderAccent"), _children(node, ctx)))


def _action_link(ctx, owner, act, value, inner_html):
    """Clickable inline content that sends an `action` event (scope, tab, strip, button)."""
    if ctx.surface_id is None:
        return inner_html
    event = {"ev": "action", "sf": ctx.surface_id, "id": owner.get("id"), "act": act}
    if value is not None:
        event["value"] = value
    return '<a href="%s" style="text-decoration: none; color: %s">%s</a>' % (
        event_href(event), ctx.theme.color("text"), inner_html)


def _soft_selection(ctx):
    """A calm highlight for the selected row: the accent colour at low strength, readable under any text."""
    return _hex_with_alpha(ctx.theme.color("accent"), 0.20)


_EL_INLINE_TAGS = ("span", "label", "a", "b", "i", "em", "strong", "code", "small", "kbd", "u", "s", "mark", "td", "th")


@functools.lru_cache(maxsize=16)
def _parsed_rules(sheets):
    rules = []
    for css in sheets:
        rules.extend(tsp_style.parse(css))      # later sheets come later in the cascade
    return tuple((parts, weight, index, declarations)
                 for index, (parts, weight, _order, declarations) in enumerate(rules))


def _styled(html_text, node, ctx):
    """``html_text`` (one <span> or <div> from _draw_el_inner) with the program's rules as a style attribute."""
    style = tsp_style.style_for(ctx.rules, node, ctx.ancestors[:-1], ctx.theme)
    if not style:
        return html_text
    return html_text.replace(">", ' style="%s">' % style, 1) if html_text.startswith(("<span", "<div")) else html_text


def _draw_el_inner(node, ctx):
    """An HTML element node (the Tern SDK's `el`), drawn with the few tags minihtml handles:
    inline tags as spans, controls as glyphs and chips, every other tag as a block.  The
    program's stylesheet (the `s` message) is applied only as far as tsp_style supports."""
    props = node.get("p") or {}
    tag = str(props.get("tag") or "div").lower()
    own = text_html(props["spans"] if props.get("spans") else props.get("text"), ctx)
    if tag == "input":
        kind = str(props.get("type") or "text").lower()
        if kind in ("radio", "checkbox"):
            if kind == "radio":
                glyph = "&#9673;" if props.get("checked") else "&#9675;"
            else:
                glyph = "&#9745;" if props.get("checked") else "&#9744;"
            if ctx.surface_id is None:
                return glyph
            return '<a href="%s" style="text-decoration: none; color: %s">%s</a>' % (
                event_href({"ev": "change", "sf": ctx.surface_id, "id": node.get("id")}),
                ctx.theme.color("text"), glyph)
        return chip(plain_text(props.get("value")) or plain_text(props.get("placeholder")) or "    ", ctx, "muted")
    if tag == "button":
        return chip(plain_text(props.get("text")) or node_text(node), ctx)
    if tag == "br":
        return "<br>"
    if tag == "hr":
        return _draw_rule(node, ctx)
    inner = own + _children(node, ctx)
    if tag in _EL_INLINE_TAGS:
        return _styled("<span>%s</span>%s" % (inner, "&nbsp;&nbsp;" if tag in ("label", "td", "th") else ""), node, ctx)
    return _styled("<div>%s</div>" % inner, node, ctx)


_DRAWERS = {}      # filled from the tsp_draw_* modules below


from . import tsp_draw_data, tsp_draw_el, tsp_draw_layout, tsp_draw_text, tsp_draw_work  # noqa: E402  (they import this module)

for _module in (tsp_draw_layout, tsp_draw_text, tsp_draw_data, tsp_draw_work, tsp_draw_el):
    _DRAWERS.update(_module.DRAWERS)


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


def _region_role(region):
    role = (region.get("p") or {}).get("role")
    return role if isinstance(role, str) and role else None


def _draw_in_region(region, children, ctx):
    """Draw ``children`` as the content of ``region`` (a region root is drawn as its children, not as an element)."""
    ctx.current = region
    try:
        return [draw(child, ctx) for child in children]
    finally:
        ctx.current = None


def _draw_main_entries(region, children, ctx, cache, env):
    """Draw the entries of ``main``, reusing the html of an entry that has not changed and does not move with time.

    Drawing every entry on every spinner tick took about 220 ms for a 650-panel transcript, all on Sublime's
    main thread, so keys were handled late.  An entry is reused only when its whole subtree is equal to the one
    drawn last time (compared by repr) and the environment it was drawn in (width, palette, stylesheets, local
    state, images, toast) is unchanged.  An entry that drew anything animated (ctx.animated, set by every drawer
    whose output depends on time) is never kept, so spinners and running-time counters still move.

    Returns (html list, outline list, indices of the entries that move with time), in the order of ``children``.
    ``cache`` is a dict the caller keeps.
    """
    if cache.get("env") != env:
        cache["env"] = env
        cache["entries"] = {}
    old = cache["entries"]
    kept = {}
    drawn, outlines, moving = [], [], []
    ctx.current = region
    try:
        for index, child in enumerate(children):
            key = child.get("id") or index
            signature = repr(child)
            hit = old.get(key)
            if hit is not None and hit[0] == signature:
                kept[key] = hit
                drawn.append(hit[1])
                outlines.append(hit[2])
                continue
            outer_animated = ctx.animated
            ctx.animated = False
            html_text = draw(child, ctx)
            outline = entry_outline(child)
            moves = ctx.animated
            ctx.animated = outer_animated or moves
            drawn.append(html_text)
            outlines.append(outline)
            if moves:
                moving.append(index)
            else:
                kept[key] = (signature, html_text, outline)
    finally:
        ctx.current = None
    cache["entries"] = kept         # an entry that left the document is dropped here
    return drawn, outlines, moving


def render_regions(document_snapshot, palette_message, cols=100, clock=None, blobs=None, local=None, aside=None,
                   reduce_motion=False, toast_region=False, entry_cache=None):
    """HTML for each top-level region of a surface.

    Returns a dict:

    * ``main`` -- a list, one html string per entry, so a viewer can update only the entries that changed;
      ``main_outline`` holds the ``entry_outline`` of each, in the same order; ``main_moving`` lists the indices of
      the entries that change with time (only filled when an ``entry_cache`` is given).
    * ``dock`` -- html of the dock (an ``omp.editor`` composer always comes last), wrapped in ``div.sf-region.sf-dock``.
    * ``layer`` -- a list of html strings, the overlays to show.  omp's layer is a stack of whole screens: only the
      topmost is listed.  A layer made only of ``overlay`` nodes that are not omp's lists all of them, in order.
    * ``under`` -- how many overlays of the layer are not listed (open underneath).
    * ``overlays`` -- one ``{"id", "k", "prefs", "covers"}`` per node of the layer, in order, whatever is listed:
      ``prefs`` says it is a ``prefs`` node (the aside), ``covers`` that it is a modal or full overlay or a picker.
    * ``covered`` -- True while a modal/full overlay or a picker covers ``main`` and ``dock`` (class sf-covered).
    * ``aside``, ``aside_id`` -- with ``aside`` = N (cells; the program supports the aside feature) the last
      ``prefs`` node of the layer is drawn N cells wide into ``aside`` (html, "" when none) and every ``prefs`` node
      leaves ``layer``; without ``aside`` they stay overlays.  ``cols`` is the width of main and dock.
    * ``toast`` -- with ``toast_region`` the toast to show at the bottom of the window (html, "" when none); the
      toast node itself then draws nothing.  Without it a toast is drawn where its node is.
    * ``entry_cache`` -- a dict the caller keeps between calls; with it, an unchanged, non-animated entry of ``main``
      is not drawn again (see ``_draw_main_entries``).  None draws everything every time.
    * ``roles`` -- the ``role`` prop of each region root ({"main": ..., "dock": ..., "layer": ...}), for data-role.
    * ``animated`` -- True while something on screen changes with time (the viewer keeps redrawing).
    """
    ctx = Context(Theme(palette_message), cols, document_snapshot.get("id"), clock, blobs, local)
    ctx.reduce_motion = bool(reduce_motion)
    ctx.rules = _parsed_rules(tuple(document_snapshot.get("sheets") or ()))
    ctx.surface_role = document_snapshot.get("role")    # the `o` role, hello app and pane cwd, when the host gives them
    ctx.surface_app = document_snapshot.get("app")
    ctx.cwd = document_snapshot.get("cwd")
    ctx.toast_sink = [] if toast_region else None
    ctx.toast_winner = None
    tsp_draw_layout.scan_toasts(document_snapshot.get("c") or [], ctx)
    regions = {"main": [], "main_outline": [], "main_moving": [], "layer": [], "under": 0, "dock": "", "animated": False,
               "overlays": [], "covered": False, "aside": "", "aside_id": None, "toast": "", "roles": {}}
    for region in document_snapshot.get("c") or []:
        region_id = region.get("id")
        if region_id not in ("main", "dock", "layer"):
            continue
        regions["roles"][region_id] = _region_role(region)
        children = region.get("c") or []
        if region_id == "main":
            if entry_cache is None:
                regions["main"] = _draw_in_region(region, children, ctx)
                regions["main_outline"] = [entry_outline(child) for child in children]
            else:
                env = (cols, repr(palette_message), ctx.reduce_motion, repr(document_snapshot.get("sheets")),
                       ctx.surface_role, ctx.surface_app, ctx.cwd, repr(local), len(blobs) if blobs else 0,
                       ctx.toast_winner, bool(toast_region))
                regions["main"], regions["main_outline"], regions["main_moving"] = _draw_main_entries(
                    region, children, ctx, entry_cache, env)
        elif region_id == "layer":
            regions["overlays"] = [{"id": o.get("id"), "k": o.get("k"), "prefs": o.get("k") == "prefs",
                                    "covers": tsp_draw_layout.overlay_covers(o)} for o in children]
            shown = children
            if isinstance(aside, int) and not isinstance(aside, bool):
                prefs = [o for o in children if o.get("k") == "prefs"]
                shown = [o for o in children if o.get("k") != "prefs"]
                if prefs:
                    saved = (ctx.cols, ctx.avail)
                    ctx.cols, ctx.avail = aside, max(12, aside - 4)
                    try:
                        regions["aside"] = _draw_in_region(region, [prefs[-1]], ctx)[0]
                    finally:
                        ctx.cols, ctx.avail = saved
                    regions["aside_id"] = prefs[-1].get("id")
            regions["covered"] = any(m["covers"] and not (m["prefs"] and regions["aside_id"] is not None)
                                     for m in regions["overlays"])
            # omp's layer is a stack of screens: the last overlay is the one the program is showing.  Any other
            # program's layer holds overlays that are all open at once.
            stacked = any(o.get("k") != "overlay" or str((o.get("p") or {}).get("role") or "").startswith("omp.")
                          for o in shown)
            chosen = shown[-1:] if stacked else shown
            drawn = _draw_in_region(region, chosen, ctx)
            regions["layer"] = drawn if stacked else [html_text for html_text in drawn if html_text]
            regions["under"] = len(shown) - len(chosen)
        else:
            # The input box (omp.editor) always comes last, below everything else in the dock.
            composer = [c for c in children if _has_role(c, "omp.editor")]
            others = [c for c in children if not _has_role(c, "omp.editor")]
            body = "".join(_draw_in_region(region, others + composer, ctx))
            if body:
                role = _region_role(region)
                attributes = "".join(' %s="%s"' % (name, html.escape(value, quote=True)) for name, value in (
                    ("data-surface", ctx.surface_role), ("data-app", ctx.surface_app), ("data-role", role))
                    if isinstance(value, str) and value)
                regions["dock"] = '<div class="sf-region sf-dock"%s>%s</div>' % (attributes, body)
    if ctx.toast_sink:
        regions["toast"] = ctx.toast_sink[0]
    regions["animated"] = ctx.animated
    return regions


def overlay_note(count, palette_message=None):
    """A line saying more overlays are open underneath the one being shown."""
    theme = Theme(palette_message)
    return ('<div style="color: %s; margin: 4px 0">%d more window%s open underneath this one. '
            'Esc closes this one first.</div>' % (theme.color("dim"), count, "" if count == 1 else "s"))


_PAGE_THEME = [None, None]     # [palette message, its Theme]: page() runs once per panel on every drawing


def page(parts, palette_message=None):
    """Wrap region HTML in a body with a base style, for one phantom."""
    if _PAGE_THEME[1] is None or _PAGE_THEME[0] is not palette_message:
        _PAGE_THEME[:] = [palette_message, Theme(palette_message)]
    theme = _PAGE_THEME[1]
    return ('<body id="tsp-native"><style>body { color: %s; } tt { font-family: monospace }</style>'
            '<div style="padding: 0 12px">%s</div></body>' % (theme.color("text"), parts))
