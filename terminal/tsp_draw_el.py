"""TSP drawers for: el, media.

DRAWERS maps a kind to a drawer function(node, ctx) -> HTML; tsp_html registers it over its own.

``el`` draws HTML elements the way the Tern spec describes them (el_el.md): the tag table with its
user-agent defaults, attributes, text before children, checkbox and radio controls with the
``change`` event, actions on any element, the default look of labels and controls, and the program's
stylesheets (the ``s`` verb).  minihtml has no flex, grid, tables, ``:checked`` or ``var()``, so every
element is matched against the sheets as a real DOM element (tsp_style) and then laid out with what
minihtml has: blocks, spans, ``&nbsp;`` gaps for flex and grid rows, monospace columns for tables.

``media`` draws the file or blob a ``media`` node names.
"""

import base64
import html as _html
import os
import re
import subprocess
import time

from . import tsp_html
from . import tsp_style

# ---------------------------------------------------------------------------------------------
# the tag table (el_el.md "Tags")
# ---------------------------------------------------------------------------------------------

ALLOWED_TAGS = frozenset((
    "div", "span", "p", "section", "header", "footer", "nav", "aside", "main", "article", "figure", "blockquote",
    "ul", "ol", "li", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "pre", "code", "kbd", "strong", "b", "em", "i", "del",
    "mark", "hr", "table", "thead", "tbody", "tr", "th", "td", "label", "button", "form", "input"))

CONTROL_TYPES = ("checkbox", "radio")

_KNOWN_TONES = ("neutral", "accent", "info", "success", "warning", "error", "pending", "muted", "user")

_OWNED_ATTRS = frozenset(("data-id", "data-role", "data-tone", "data-mark", "data-href", "data-scope",
                          "aria-label", "aria-checked"))
_ATTR_SUFFIX = re.compile(r"^[a-z0-9_-]+$")

CW = tsp_style.CELL_WIDTH_PX
LH = tsp_style.LINE_HEIGHT_PX

# user-agent defaults: Chrome's, as Tern's sheet keeps them (declarations in the same syntax as a sheet)
_UA = {
    "div": {"display": "block"},
    "span": {"display": "inline"},
    "p": {"display": "block", "margin": "1em 0"},
    "section": {"display": "block"}, "header": {"display": "block"}, "footer": {"display": "block"},
    "nav": {"display": "block"}, "aside": {"display": "block"}, "main": {"display": "block"},
    "article": {"display": "block"},
    "figure": {"display": "block", "margin": "1em 40px"},
    "blockquote": {"display": "block", "margin": "1em 40px"},
    "ul": {"display": "block", "margin": "1em 0", "padding-left": "40px", "list-style-type": "disc"},
    "ol": {"display": "block", "margin": "1em 0", "padding-left": "40px", "list-style-type": "decimal"},
    "li": {"display": "list-item"},
    "dl": {"display": "block", "margin": "1em 0"},
    "dt": {"display": "block"},
    "dd": {"display": "block", "margin-left": "40px"},
    "h1": {"display": "block", "font-size": "2em", "font-weight": "bold", "margin": "0.67em 0"},
    "h2": {"display": "block", "font-size": "1.5em", "font-weight": "bold", "margin": "0.83em 0"},
    "h3": {"display": "block", "font-size": "1.17em", "font-weight": "bold", "margin": "1em 0"},
    "h4": {"display": "block", "font-weight": "bold", "margin": "1.33em 0"},
    "pre": {"display": "block", "white-space": "pre", "margin": "1em 0", "font-family": "monospace"},
    "code": {"display": "inline", "font-family": "monospace"},
    "kbd": {"display": "inline", "font-family": "monospace"},
    "strong": {"display": "inline", "font-weight": "bolder"},
    "b": {"display": "inline", "font-weight": "bolder"},
    "em": {"display": "inline", "font-style": "italic"},
    "i": {"display": "inline", "font-style": "italic"},
    "del": {"display": "inline", "text-decoration-line": "line-through"},
    "mark": {"display": "inline", "background-color": "#ffff00", "color": "#000000"},
    "hr": {"display": "block", "margin": "0.5em auto", "border-top": "1px solid #808080"},
    "table": {"display": "table"},
    "thead": {"display": "table-header-group"},
    "tbody": {"display": "table-row-group"},
    "tr": {"display": "table-row"},
    "th": {"display": "table-cell", "padding": "1px", "font-weight": "bold", "text-align": "center"},
    "td": {"display": "table-cell", "padding": "1px"},
    "label": {"display": "inline-flex", "gap": "6px", "align-items": "center"},
    "button": {"display": "inline-block", "background-color": "transparent", "border": "none", "padding": "0",
               "text-align": "inherit"},
    "form": {"display": "block"},
}

_BLOCK_DISPLAYS = frozenset(("block", "flex", "grid", "table", "list-item", "flow-root", "table-row-group",
                             "table-header-group", "table-footer-group", "table-row", "-webkit-box"))


def _tag_of(props):
    tag = props.get("tag")
    return "div" if tag is None or tag == "" else str(tag)


def unknown_tag_message(props):
    """The ``error`` text Tern sends for an ``el`` with an unknown tag, or None when the tag is known."""
    props = props or {}
    tag = _tag_of(props)
    low = tag.lower()
    if low == "input":
        kind = props.get("type")
        if kind in CONTROL_TYPES:
            return None
        if kind is None or kind == "":
            return "unknown el tag input"
        return "unknown el tag input type %s" % kind
    if low in ALLOWED_TAGS:
        return None
    return "unknown el tag %s" % tag


def _attr_text(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return "%g" % value
    if isinstance(value, str):
        return value
    return None


def clean_attrs(props, control=False):
    """The ``attrs`` an el may set (data-*, aria-*, role, colspan, rowspan) as {name: text}."""
    raw = (props or {}).get("attrs")
    out = {}
    if not isinstance(raw, dict):
        return out
    for name, value in raw.items():
        if not isinstance(name, str):
            continue
        text = _attr_text(value)
        if text is None or name in _OWNED_ATTRS:
            continue
        if name.startswith("data-") or name.startswith("aria-"):
            if not _ATTR_SUFFIX.match(name[5:]):
                continue
            if control and name == "aria-labelledby":
                continue
        elif name == "role":
            if control:
                continue
        elif name not in ("colspan", "rowspan"):
            continue
        out[name] = text
    return out


def _control_type(props):
    if str(_tag_of(props)).lower() != "input":
        return None
    kind = props.get("type")
    return kind if kind in CONTROL_TYPES else None


# ---------------------------------------------------------------------------------------------
# state, elements
# ---------------------------------------------------------------------------------------------

class _State:
    """Per-drawing state kept on the Context: the cascade environment, the element chain, the link chain."""

    def __init__(self, ctx):
        self.rules = ctx.rules
        columns = getattr(ctx, "cols", 100) or 100
        self.env = tsp_style.Env(ctx.theme, ctx.rules, (max(320.0, columns * CW), 1000.0))
        attrs = {"data-scope": "sf1"}
        role = getattr(ctx, "surface_role", None)
        if role:
            attrs["data-surface"] = str(role)
        app = getattr(ctx, "surface_app", None)
        if app:
            attrs["data-app"] = str(app)
        self.root = tsp_style.Elem("div", ("sf-region", "sf-main"), attrs, None, None, None, 0, "div",
                                   {"display": "block"})
        self.stack = []             # the Elems being drawn, outermost first
        self.links = []             # (href, title) of the enclosing click targets
        self.ancestors = {}         # id(node) -> Elem of ancestors that other drawers drew around an el


def _state(ctx):
    state = getattr(ctx, "_el_state", None)
    if state is None or state.rules is not ctx.rules:
        state = _State(ctx)
        ctx._el_state = state
    return state


def _list_depth(parent):
    depth = 0
    while parent is not None:
        if parent.logical in ("ul", "ol"):
            depth += 1
        parent = parent.parent
    return depth


def _ua_for(logical, control, props, parent):
    ua = dict(_UA.get(logical, {"display": "block"}))
    if control:
        radio = control == "radio"
        ua = {"display": "inline-block", "width": "15px", "height": "15px", "border-radius": "8px" if radio else "4px",
              "background-color": "var(--card)", "border": "1px solid var(--l4)"}
        if props.get("checked"):
            ua["background-color"] = "var(--accent-fill)"
            ua["color"] = "#ffffff"
        if props.get("disabled"):
            ua["opacity"] = "0.45"
        return ua
    if logical in ("ul", "ol"):
        depth = _list_depth(parent)
        if depth:
            ua["margin"] = "0"
            if logical == "ul":
                ua["list-style-type"] = "circle" if depth == 1 else "square"
    mark = props.get("mark")
    if mark == "pick":
        ua["border-left"] = "3px solid var(--accent)"
        ua["padding-left"] = "6px"
    elif mark == "drop":
        ua["opacity"] = "0.5"
    return ua


def _describe(node, parent, siblings, index):
    """The Elem of a wire node (see tsp_style.Elem)."""
    props = node.get("p") or {}
    kind = node.get("k")
    node_id = node.get("id")
    attrs = {}
    if node_id is not None:
        attrs["data-id"] = str(node_id)
    if props.get("role"):
        attrs["data-role"] = str(props["role"])
    if props.get("tone") in _KNOWN_TONES:
        attrs["data-tone"] = props["tone"]
    if props.get("mark"):
        attrs["data-mark"] = str(props["mark"])
    if props.get("href"):
        attrs["data-href"] = str(props["href"])
    if props.get("title"):
        attrs["title"] = str(props["title"])
    if props.get("aria"):
        attrs["aria-label"] = str(props["aria"])
    children = node.get("c") or []
    if kind != "el":
        classes = ["sf", "sf-%s" % kind]
        display = "inline" if kind in tsp_html._INLINE_KINDS else "block"
        elem = tsp_style.Elem("", classes, attrs, node, parent, siblings, index, kind, {"display": display},
                              empty=not children)
        return elem
    raw_tag = _tag_of(props)
    logical = raw_tag.lower()
    control = _control_type(props)
    if control:
        dom_tag = "span"
    elif logical == "input" or logical not in ALLOWED_TAGS:
        dom_tag, logical = "div", "div"
    elif logical == "form":
        dom_tag = "div"
    else:
        dom_tag = logical
    attrs.update(clean_attrs(props, bool(control)))
    if control:
        attrs["role"] = control
        attrs["aria-checked"] = "true" if props.get("checked") else "false"
        attrs["aria-disabled"] = "true" if props.get("disabled") else "false"
        if props.get("checked"):
            attrs["checked"] = ""
        if props.get("disabled"):
            attrs["disabled"] = ""
    classes = ["sf", "sf-el"]
    class_prop = props.get("class")
    if isinstance(class_prop, str):
        classes.extend(name for name in class_prop.split() if name != "sf" and not name.startswith("sf-"))
    actions = props.get("actions")
    if isinstance(actions, dict) and (actions.get("click") or actions.get("dblclick")):
        classes.append("sf-act")
    if props.get("hidden"):
        classes.append("sf-hidden")
    empty = not control and not props.get("text") and not props.get("spans") and not children
    if logical == "input" or logical == "hr":
        empty = True
    elem = tsp_style.Elem(dom_tag, classes, attrs, node, parent, siblings, index, logical,
                          _ua_for(logical, control, props, parent), empty)
    elem.meta["control"] = control
    elem.meta["void"] = raw_tag.lower() == "input" and not control
    return elem


def _kids(elem):
    """The child Elems of ``elem`` (built once)."""
    if elem.kids is None:
        children = (elem.node.get("c") or []) if elem.node is not None else []
        kids = []
        for index, child in enumerate(children):
            kids.append(_describe(child, elem, kids, index))
        elem.kids = kids
    return elem.kids


def _ancestor_chain(nodes, st):
    """The Elem of the innermost of ``nodes`` (ancestors, outermost first, that this module did not draw)."""
    parent = st.stack[-1] if st.stack else st.root
    for ancestor in nodes:
        elem = st.ancestors.get(id(ancestor))
        if elem is None:
            elem = _describe(ancestor, parent, None, 0)
            elem.siblings = [elem]
            st.ancestors[id(ancestor)] = elem
        parent = elem
    return parent


def _elem_for(node, st, ctx):
    """The Elem of ``node`` under its real parent (so sibling and child selectors see the true structure)."""
    outer = list(getattr(ctx, "stack", None) or [])
    current = getattr(ctx, "current", None)
    if current is not None and current is not node:
        outer.append(current)       # called after the node's own draw: the current node is its parent
    parent = None
    if outer:
        wanted = outer[-1]
        for candidate in reversed(st.stack):
            if candidate.node is wanted:
                parent = candidate
                break
        if parent is None:
            parent = _ancestor_chain(outer, st)
    if parent is None:
        parent = st.stack[-1] if st.stack else st.root
    if parent.node is not None:
        for kid in _kids(parent):
            if kid.node is node:
                return kid
    elem = _describe(node, parent, None, 0)
    elem.siblings = [elem]
    return elem


# ---------------------------------------------------------------------------------------------
# small html helpers
# ---------------------------------------------------------------------------------------------

class _Frag:
    """One drawn piece: its html, whether it is a block, whether it was one before being forced inline."""
    __slots__ = ("html", "block", "was_block", "comp", "node", "elem", "order")

    def __init__(self, html, block=False, was_block=None, comp=None, node=None, elem=None):
        self.html = html
        self.block = block
        self.was_block = block if was_block is None else was_block
        self.comp = comp
        self.node = node
        self.elem = elem
        self.order = 0


def _attr(text):
    return _html.escape(str(text), quote=True)


def _gap_chars(pixels):
    return 0 if pixels is None or pixels < 1 else max(1, int(round(pixels / CW)))


def _visible_len(markup):
    return len(_html.unescape(re.sub(r"<[^>]*>", "", markup)))


def _color_css(comp):
    return tsp_style.to_hex(tuple(comp.color[:3]) + (1.0,))


def _link_open(st, comp, ctx):
    href, title = st.links[-1]
    tip = ' title="%s"' % _attr(title) if title else ""
    return '<a href="%s"%s style="text-decoration: none; color: %s">' % (href, tip, _color_css(comp))


def _linked(markup, st, comp, ctx):
    """``markup`` (inline content) inside the innermost click target, when there is one."""
    if not markup or not st.links:
        return markup
    return _link_open(st, comp, ctx) + markup + "</a>"


def _text_html(text, comp):
    """A plain text node as html: white-space, text-transform and escaping applied."""
    space = (comp.inherited("white-space") or "normal").lower()
    transform = (comp.inherited("text-transform") or "none").lower()
    if transform == "uppercase":
        text = text.upper()
    elif transform == "lowercase":
        text = text.lower()
    elif transform == "capitalize":
        text = re.sub(r"(^|\s)(\S)", lambda m: m.group(1) + m.group(2).upper(), text)
    escaped = tsp_html.esc(text)
    if space in ("pre", "pre-wrap", "break-spaces"):
        escaped = escaped.replace("\t", "    ").replace(" ", "&nbsp;")
        return escaped.replace("\r\n", "\n").replace("\n", "<br>")
    if space == "pre-line":
        lines = [re.sub(r"[ \t]+", " ", line) for line in escaped.split("\n")]
        return "<br>".join(lines)
    collapsed = re.sub(r"\s+", " ", escaped)
    if space == "nowrap":
        collapsed = collapsed.replace(" ", "&nbsp;")
    return collapsed


def _wrap(comp, inner, block, skip=(), extra=""):
    style, flags = comp.box_style(inline=not block, skip=skip)
    if flags["mono"]:
        inner = "<tt>%s</tt>" % inner
    if extra:
        style = "; ".join(part for part in (style, extra) if part)
    tag = "div" if block else "span"
    return "<%s%s>%s</%s>" % (tag, ' style="%s"' % _attr(style) if style else "", inner, tag)


def _unwrap_block(markup):
    """A single ``<div ...>..</div>`` as a ``<span>`` (a block drawn where only inline content may go)."""
    stripped = markup.strip()
    if stripped.startswith("<div") and stripped.endswith("</div>") and stripped.count("<div") == 1:
        return "<span" + stripped[4:-6] + "</span>"
    return markup


def _is_block_markup(markup):
    return markup.lstrip().startswith(("<div", "<h1", "<h2", "<h3", "<pre", "<ul", "<ol"))


# ---------------------------------------------------------------------------------------------
# clicks
# ---------------------------------------------------------------------------------------------

def _first_control(node):
    """The first checkbox or radio el drawn inside ``node`` (depth first), or None."""
    for child in node.get("c") or []:
        if child.get("k") != "el":
            continue
        if _control_type(child.get("p") or {}):
            return child
        found = _first_control(child)
        if found is not None:
            return found
    return None


def _change_href(control, ctx):
    return tsp_html.event_href({"ev": "change", "sf": ctx.surface_id, "id": control.get("id")})


def _own_link(node, props, elem, ctx):
    """The href a click on this element sends, or None."""
    if ctx.surface_id is None:
        return None
    event = tsp_html._click_event(node, ctx)
    if event is not None:
        return tsp_html.event_href(event)
    if elem.logical == "label":
        control = _first_control(node)
        if control is not None and not (control.get("p") or {}).get("disabled"):
            return _change_href(control, ctx)
        return None
    if props.get("href"):
        return tsp_html.event_href({"local": "open", "href": str(props["href"])})
    return None


# ---------------------------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------------------------

def _own_html(props, comp, ctx):
    spans = props.get("spans")
    if spans:
        return tsp_html.text_html(spans, ctx)
    text = props.get("text")
    if isinstance(text, str) and text:
        return _text_html(text, comp)
    return ""


def _pseudo(elem, comp, name, ctx, st):
    generated = tsp_style.compute_pseudo(elem, st.env, name)
    if generated is None:
        return None
    text = generated.props.get("content") or ""
    if not text:
        return None
    block = generated.display in _BLOCK_DISPLAYS
    markup = _wrap(generated, _text_html(text, generated), block)
    return _Frag(markup, block, comp=generated)


def _draw_other(child, child_elem, ctx, st):
    st.stack.append(child_elem)
    try:
        markup = tsp_html.draw(child, ctx)
    finally:
        st.stack.pop()
    if not markup:
        return None
    block = _is_block_markup(markup)
    return _Frag(markup, block, node=child, elem=child_elem)


def _draw_children(node, elem, ctx, st, comp, force_inline):
    """[_Frag] for the children, in document order (hidden and display:none children left out)."""
    frags = []
    for child, child_elem in zip(node.get("c") or [], _kids(elem)):
        if child.get("k") == "el":
            frag = _render_child(child, ctx, st, child_elem, "inline" if force_inline else "auto")
        else:
            frag = _draw_other(child, child_elem, ctx, st)
            if frag is not None:
                if st.links and tsp_html._click_event(child, ctx) is None and not frag.block:
                    frag.html = _linked(frag.html, st, comp, ctx)
                elif st.links and tsp_html._click_event(child, ctx) is None and frag.block:
                    frag.html = tsp_html._link_wrap(frag.html, st.links[-1][0], ctx, st.links[-1][1])
                if force_inline and frag.block:
                    frag.html = _unwrap_block(frag.html)
                    frag.block = _is_block_markup(frag.html)
        if frag is not None:
            frags.append(frag)
    return frags


def _flow_html(frags, force_inline):
    out = []
    for position, frag in enumerate(frags):
        out.append(frag.html)
        if force_inline and frag.was_block and position < len(frags) - 1:
            out.append("<br>")
    return "".join(out)


def _marker(elem, comp):
    kind = (comp.inherited("list-style-type") or "disc").strip().lower()
    if kind == "none":
        return ""
    if kind == "decimal":
        position = sum(1 for sibling in elem.siblings[:elem.index + 1] if sibling.logical == "li")
        return "%d.&nbsp;" % position
    if kind[:1] in "\"'":
        return tsp_html.esc(kind[1:-1]) + "&nbsp;"
    return {"circle": "&#9702;", "square": "&#9642;"}.get(kind, "&#8226;") + "&nbsp;"


def _flex_direction(comp):
    return (comp.get("flex-direction") or "row").lower()


def _flex_layout(frags, comp, force_inline):
    """Flex children laid out with what minihtml has: a row is inline segments separated by gap spaces
    (``order`` and ``row-reverse`` honoured); a column is stacked blocks with the gap as a margin."""
    frags = sorted(frags, key=lambda frag: frag.order)
    direction = _flex_direction(comp)
    gap_value = comp.length("column-gap" if direction.startswith("row") else "row-gap")
    gap = gap_value if isinstance(gap_value, float) else 0.0
    if direction.endswith("reverse"):
        frags = list(reversed(frags))
    if direction.startswith("row") or force_inline:
        separator = "&nbsp;" * _gap_chars(gap)
        pieces = []
        for frag in frags:
            pieces.append(frag.html if not frag.block else _unwrap_block(frag.html))
        return separator.join(pieces)
    out = []
    for position, frag in enumerate(frags):
        markup = frag.html if frag.block else "<div>%s</div>" % frag.html
        if position and gap >= 1:
            markup = '<div style="margin-top: %s">%s</div>' % (tsp_style._px(gap), markup)
        out.append(markup)
    return "".join(out)


def _parse_tracks(text):
    """[("fixed", px) | ("fr", n) | ("auto", None)] of a grid-template-columns value."""
    tracks = []
    for token in tsp_style.tokens(text or ""):
        low = token.lower()
        match = re.match(r"^repeat\(\s*(\d+)\s*,(.*)\)$", low)
        if match:
            inner = _parse_tracks(match.group(2))
            tracks.extend(inner * int(match.group(1)))
            continue
        tracks.append(_track(low))
    return tracks


def _track(low):
    if low.endswith("fr"):
        try:
            return ("fr", float(low[:-2]))
        except ValueError:
            return ("auto", None)
    if low.startswith("minmax("):
        pieces = tsp_style.split_top(low[7:-1], ",")
        if len(pieces) == 2:
            high = pieces[1].strip()
            if high.endswith("fr"):
                return ("fr", 1.0)
            return ("auto", None)
    value = tsp_style.length(low)
    if isinstance(value, float):
        return ("fixed", value)
    return ("auto", None)


def _grid_line(text):
    """(start line or None, span) of a grid-column value."""
    if not text:
        return None, 1
    low = text.lower().replace(" ", "")
    match = re.match(r"^span(\d+)$", low)
    if match:
        return None, int(match.group(1))
    match = re.match(r"^(\d+)/span(\d+)$", low)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.match(r"^(\d+)/(-?\d+)$", low)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        return start, max(1, (end - start) if end > 0 else 1)
    match = re.match(r"^(\d+)$", low)
    if match:
        return int(match.group(1)), 1
    return None, 1


def _columns(rows, widths_hint, gap_chars):
    """Lay out rows of cells as aligned monospace columns.

    ``rows`` is [[(frag, start column, span)]]; ``widths_hint`` the fixed minimum widths per column.
    Returns [html per row]."""
    count = max([len(widths_hint)] + [cell[1] + cell[2] for row in rows for cell in row])
    widths = [0] * count
    for index, hint in enumerate(widths_hint):
        widths[index] = hint
    lengths = {}
    for row in rows:
        for frag, start, span in row:
            lengths[id(frag)] = _visible_len(frag.html)
            if span == 1:
                widths[start] = max(widths[start], lengths[id(frag)])
    for row in rows:
        for frag, start, span in row:
            if span > 1:
                room = sum(widths[start:start + span]) + gap_chars * (span - 1)
                if lengths[id(frag)] > room:
                    widths[start + span - 1] += lengths[id(frag)] - room
    out = []
    for row in rows:
        by_start = {cell[1]: cell for cell in row}
        pieces = []
        column = 0
        last_used = max((cell[1] + cell[2] for cell in row), default=0)
        while column < last_used:
            cell = by_start.get(column)
            if cell is None:
                pieces.append("&nbsp;" * widths[column])
                column += 1
                continue
            frag, start, span = cell
            room = sum(widths[start:start + span]) + gap_chars * (span - 1)
            pad = max(0, room - lengths[id(frag)])
            align = ((frag.comp.inherited("text-align") if frag.comp is not None else None) or "left").lower()
            end = start + span
            if end >= last_used and align not in ("right", "center"):
                pad = 0
            if align == "right":
                pieces.append("&nbsp;" * pad + frag.html)
            elif align == "center":
                left = pad // 2
                pieces.append("&nbsp;" * left + frag.html + ("&nbsp;" * (pad - left) if end < last_used else ""))
            else:
                pieces.append(frag.html + "&nbsp;" * pad)
            column = end
        out.append(("&nbsp;" * gap_chars).join(pieces))
    return out


def _grid_layout(frags, comp):
    tracks = _parse_tracks(comp.get("grid-template-columns"))
    count = max(1, len(tracks))
    hints = []
    for kind, value in tracks or [("auto", None)]:
        hints.append(int(round(value / CW)) if kind == "fixed" else 0)
    gap_value = comp.length("column-gap")
    gap_chars = _gap_chars(gap_value if isinstance(gap_value, float) else 0.0) if gap_value is not None else 0
    row_gap_value = comp.length("row-gap")
    row_gap = row_gap_value if isinstance(row_gap_value, float) else 0.0
    rows = {}
    row, col = 0, 0
    for frag in sorted(frags, key=lambda f: f.order):
        raw = frag.comp.get("grid-column") if frag.comp is not None else None
        start, span = _grid_line(raw)
        span = min(span, count)
        if start is not None:
            column = max(0, min(start - 1, count - 1))
            if column < col:
                row += 1
            col = column
        else:
            if col + span > count and col > 0:
                row += 1
                col = 0
        rows.setdefault(row, []).append((frag, col, span))
        col += span
        if col >= count:
            row += 1
            col = 0
    ordered = [rows[key] for key in sorted(rows)]
    lines = _columns(ordered, hints, gap_chars)
    out = []
    for position, line in enumerate(lines):
        margin = ' style="margin-top: %s"' % tsp_style._px(row_gap) if position and row_gap >= 8 else ""
        out.append("<div%s>%s</div>" % (margin, line))
    return "<tt>%s</tt>" % "".join(out)


def _table_layout(node, elem, ctx, st, comp):
    """A table as aligned columns of its cells.  Returns the html of its rows (and stray children)."""
    rows = []
    others = []

    def collect(container, container_elem):
        for child, child_elem in zip(container.get("c") or [], _kids(container_elem)):
            if child.get("k") != "el":
                others.append((child, child_elem))
                continue
            child_comp = tsp_style.compute(child_elem, st.env)
            if child_comp.display in ("table-row-group", "table-header-group", "table-footer-group"):
                collect(child, child_elem)
            elif child_comp.display == "table-row":
                rows.append((child, child_elem, child_comp))
            elif child_comp.display != "none" and not (child.get("p") or {}).get("hidden"):
                others.append((child, child_elem))

    collect(node, elem)
    st.stack.append(elem)
    try:
        header = []
        for child, child_elem in others:
            if child.get("k") == "el":
                frag = _render_child(child, ctx, st, child_elem, "auto")
            else:
                frag = _draw_other(child, child_elem, ctx, st)
            if frag is not None:
                header.append(frag.html if frag.block else "<div>%s</div>" % frag.html)
        grid_rows = []
        row_wrappers = []
        for row_node, row_elem, row_comp in rows:
            st.stack.append(row_elem)
            try:
                cells = []
                column = 0
                for cell_node, cell_elem in zip(row_node.get("c") or [], _kids(row_elem)):
                    if cell_node.get("k") == "el":
                        frag = _render_child(cell_node, ctx, st, cell_elem, "cell")
                    else:
                        frag = _draw_other(cell_node, cell_elem, ctx, st)
                    if frag is None:
                        continue
                    try:
                        span = max(1, int(cell_elem.attrs.get("colspan", "1")))
                    except ValueError:
                        span = 1
                    cells.append((frag, column, span))
                    column += span
            finally:
                st.stack.pop()
            grid_rows.append(cells)
            row_wrappers.append((row_comp, row_node))
    finally:
        st.stack.pop()
    lines = _columns(grid_rows, [], 1)
    body = []
    for (row_comp, _row_node), line in zip(row_wrappers, lines):
        body.append(_wrap(row_comp, line, True, skip=("padding-h", "margin", "size")))
    return "".join(header) + ("<tt>%s</tt>" % "".join(body) if body else "")


# ---- scrolling el containers -----------------------------------------------------------------

def _line_limit(props, comp):
    """Number of lines a scrolling container shows, or None when it does not scroll."""
    maximum = props.get("max")
    if isinstance(maximum, dict):
        raw = maximum.get("h")
        if isinstance(raw, str):
            match = re.match(r"^(\d+(?:\.\d+)?)lines$", raw.strip())
            if match:
                return max(1, int(float(match.group(1))))
    overflow = (comp.get("overflow-y") or comp.get("overflow-x") or "visible").lower()
    if overflow in ("auto", "scroll", "hidden", "clip"):
        for prop in ("max-height", "height"):
            value = comp.length(prop)
            if isinstance(value, float) and value > 0:
                return max(1, int(value // LH))
    return None


def _window(node, frags, limit, ctx):
    """Show only the children that fit in ``limit`` lines from the scroll position; publish the geometry."""
    heights = [max(1, f.html.count("<br>") + f.html.count("</div>")) if f.block else 1 for f in frags]
    total = len(frags)
    local = getattr(ctx, "local", None)
    first = 0
    if local is not None:
        stored = local.get("scroll:%s" % node.get("id"))
        if isinstance(stored, int):
            first = max(0, stored)
    max_first = total
    used = 0
    while max_first > 0 and used + heights[max_first - 1] <= limit:
        max_first -= 1
        used += heights[max_first]
    first = min(first, max_first)
    shown, used = 0, 0
    while first + shown < total and used + heights[first + shown] <= limit:
        used += heights[first + shown]
        shown += 1
    shown = max(shown, 1) if total else 0
    if local is not None:
        local["scrollinfo:%s" % node.get("id")] = {
            "total": total, "shown": shown, "first": first, "follow": False,
            "ids": [f.node.get("id") if f.node is not None else None for f in frags]}
    return frags[first:first + shown], first, total - first - shown


def _scroll_marker(count, up, ctx):
    color = ctx.theme.color("dim")
    return '<div style="color: %s">%s %d more</div>' % (color, "&#9650;" if up else "&#9660;", count)


# ---- el -----------------------------------------------------------------------------------

def _render_child(child, ctx, st, elem, mode):
    """_render for a child el, keeping ``ctx.current`` / ``ctx.stack`` (the ancestors other drawers read) true."""
    parent = getattr(ctx, "current", None)
    stack = getattr(ctx, "stack", None)
    if stack is not None and parent is not None:
        stack.append(parent)
    if hasattr(ctx, "current"):
        ctx.current = child
    try:
        return _render(child, ctx, st, elem, mode)
    finally:
        if hasattr(ctx, "current"):
            ctx.current = parent
        if stack is not None and parent is not None:
            stack.pop()


def _render(node, ctx, st, elem, mode="auto"):
    """The _Frag of an el node (None when it draws nothing).  ``mode``: "auto", "inline" (forced into a
    line of inline content) or "cell" (a grid or table cell: inline, with horizontal padding as spaces)."""
    props = node.get("p") or {}
    if props.get("hidden"):
        return None
    comp = tsp_style.compute(elem, st.env)
    display = comp.display
    if display == "none":
        return None
    if elem.meta.get("control"):
        return _render_control(node, props, elem, comp, ctx, st)
    logical = elem.logical
    block = display in _BLOCK_DISPLAYS and mode == "auto"
    if elem.meta.get("void"):
        return _finish(node, elem, comp, "", block, mode, ctx, "")
    if logical == "hr":
        frag = _Frag(_wrap(comp, "", True) if mode == "auto" else "<br>", mode == "auto", True, comp, node, elem)
        frag.order = int(comp.number("order", 0) or 0)
        return frag
    # click targets: this element's own, else the enclosing one (with this element's tooltip)
    pushed = False
    href = _own_link(node, props, elem, ctx)
    title = props.get("title")
    if href:
        st.links.append((href, title))
        pushed = True
    elif title and ctx.surface_id is not None:
        inherited_href = st.links[-1][0] if st.links else tsp_html.event_href({"local": "none"})
        st.links.append((inherited_href, title))
        pushed = True
    st.stack.append(elem)
    try:
        layout = display
        if layout in ("flex", "inline-flex"):
            force = True if _flex_direction(comp).startswith("row") else mode != "auto"
        elif layout in ("grid", "inline-grid"):
            force = True
        else:
            force = mode != "auto"
        if layout == "table":
            content = _table_layout(node, elem, ctx, st, comp)
            frag_block = mode == "auto"
        else:
            items = []
            before = _pseudo(elem, comp, "before", ctx, st)
            if before is not None:
                items.append(before)
            own = _own_html(props, comp, ctx)
            if own:
                items.append(_Frag(_linked(own, st, comp, ctx), False, comp=comp))
            kids = _draw_children(node, elem, ctx, st, comp, force or mode != "auto")
            for frag in kids:
                frag.order = int(frag.comp.number("order", 0) or 0) if frag.comp is not None else 0
            items.extend(kids)
            after = _pseudo(elem, comp, "after", ctx, st)
            if after is not None:
                items.append(after)
            if not items and not own:
                aria = props.get("aria")
                if aria and logical in ("button", "label"):
                    items.append(_Frag(_linked(tsp_html.esc(aria), st, comp, ctx), False, comp=comp))
            frag_block = block
            if layout in ("flex", "inline-flex"):
                extra_align = None
                justify = (comp.get("justify-content") or "").lower()
                if "text-align" not in comp.props and justify in ("center", "flex-end", "end", "right"):
                    extra_align = "text-align: %s" % ("center" if justify == "center" else "right")
                content = _flex_layout(items, comp, force and not block)
                if extra_align:
                    comp_extra = extra_align
                else:
                    comp_extra = ""
                return _finish(node, elem, comp, content, block, mode, ctx, comp_extra)
            if layout in ("grid", "inline-grid"):
                content = _grid_layout(items, comp)
            elif layout == "contents":
                return _Frag(_flow_html(items, force), False, False, comp, node, elem)
            else:
                limit = _line_limit(props, comp) if block else None
                if limit is not None:
                    shown, above, below = _window(node, items, limit, ctx)
                    pieces = []
                    if above:
                        pieces.append(_scroll_marker(above, True, ctx))
                    pieces.append(_flow_html(shown, force))
                    if below:
                        pieces.append(_scroll_marker(below, False, ctx))
                    content = "".join(pieces)
                else:
                    content = _flow_html(items, force)
                if layout == "list-item" or logical == "li":
                    content = '<span>%s</span>' % _marker(elem, comp) + content
        return _finish(node, elem, comp, content, block if layout != "table" else frag_block, mode, ctx, "")
    finally:
        st.stack.pop()
        if pushed:
            st.links.pop()


def _extent_css(value):
    """CSS for a min/max extent: "<n>ch", "<n>lines" or a fraction of the parent (else None)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return "%g%%" % round(value * 100, 2) if 0 < value <= 1 else None
    if isinstance(value, str):
        match = re.match(r"^(\d+(?:\.\d+)?)(ch|lines)$", value.strip())
        if match:
            return tsp_style._px(float(match.group(1)) * (CW if match.group(2) == "ch" else LH))
    return None


def _extent_style(props):
    """The inline size styles of the common props min, max (max.h is the scroller's) and basis."""
    out = []
    for key in ("min", "max"):
        box = props.get(key)
        if not isinstance(box, dict):
            continue
        for axis, name in (("w", "width"), ("h", "height")):
            if key == "max" and axis == "h":
                continue
            css = _extent_css(box.get(axis))
            if css:
                out.append("%s-%s: %s" % (key, name, css))
    basis = props.get("basis")
    if isinstance(basis, (int, float)) and not isinstance(basis, bool) and 0 < basis <= 1:
        out.append("width: %g%%" % round(basis * 100, 2))
    return "; ".join(out)


def _finish(node, elem, comp, content, block, mode, ctx, extra):
    skip = ()
    if mode == "cell":
        skip = ("padding-h", "margin", "size")
        left = comp.length("padding-left")
        right = comp.length("padding-right")
        pad_left = int(round((left if isinstance(left, float) else 0.0) / CW))
        pad_right = int(round((right if isinstance(right, float) else 0.0) / CW))
        content = "&nbsp;" * pad_left + content + "&nbsp;" * pad_right
    if mode == "auto" and block:
        size = _extent_style(node.get("p") or {})
        if size:
            extra = "; ".join(part for part in (size, extra) if part)
    markup = _wrap(comp, content, block, skip, extra)
    frag = _Frag(markup, block, comp.display in _BLOCK_DISPLAYS, comp, node, elem)
    frag.order = int(comp.number("order", 0) or 0)
    return frag


def _render_control(node, props, elem, comp, ctx, st):
    radio = elem.meta["control"] == "radio"
    checked = bool(props.get("checked"))
    if checked:
        glyph = "&nbsp;&#9679;" if radio else "&nbsp;&#10003;"
    else:
        glyph = "&nbsp;&nbsp;"
    markup = _wrap(comp, glyph, False, skip=("size",))
    if ctx.surface_id is not None and not props.get("disabled") and not (radio and checked):
        markup = '<a href="%s" style="text-decoration: none; color: %s">%s</a>' % (
            _change_href(node, ctx), _color_css(comp), markup)
    frag = _Frag(markup, False, False, comp, node, elem)
    frag.order = int(comp.number("order", 0) or 0)
    return frag


def draw_el(node, ctx):
    """An ``el`` node as minihtml (the drawer registered for kind "el")."""
    st = _state(ctx)
    elem = _elem_for(node, st, ctx)
    frag = _render(node, ctx, st, elem, "auto")
    return frag.html if frag is not None else ""


def style_for_node(node, ctx):
    """The style attribute text the program's sheets give a node of any kind (for drawers of other kinds)."""
    st = _state(ctx)
    elem = _elem_for(node, st, ctx)
    return tsp_style.compute(elem, st.env).box_style()[0]


# ---------------------------------------------------------------------------------------------
# media
# ---------------------------------------------------------------------------------------------

MEDIA_MAX_BYTES = 4 * 1024 * 1024
_MEDIA_CACHE = {}
_GIT_CACHE = {}                # (work dir, git spec) -> (time, bytes or None, error or None)
GIT_CACHE_SECONDS = 5.0


def _media_bytes(path, rev, cwd):
    """(bytes or None, error text or None) of the file a media node names (``rev`` reads it from git)."""
    base = cwd if cwd and os.path.isdir(cwd) else None
    expanded = os.path.expanduser(path)
    if rev:
        work = base or (os.path.dirname(expanded) if os.path.isabs(expanded) else None)
        if work is None or not os.path.isdir(work):
            return None, "no working directory for git"
        target = os.path.relpath(expanded, work).replace("\\", "/") if os.path.isabs(expanded) else expanded
        spec = "%s:./%s" % (rev, target)         # "HEAD:./a.png", ":0:./a.png" (the index)
        key = (work, spec)
        cached = _GIT_CACHE.get(key)
        if cached is not None and time.time() - cached[0] < GIT_CACHE_SECONDS:
            return cached[1], cached[2]
        try:
            result = subprocess.run(["git", "-C", work, "show", spec], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=2)
            if result.returncode != 0:
                data, error = None, "not in git at %s" % rev
            elif len(result.stdout) > MEDIA_MAX_BYTES:
                data, error = None, "file over %d MB" % (MEDIA_MAX_BYTES // (1024 * 1024))
            else:
                data, error = result.stdout, None
        except (OSError, subprocess.SubprocessError):
            data, error = None, "git is not available"
        _GIT_CACHE[key] = (time.time(), data, error)     # a failure is remembered too: no git run per redraw
        for old in list(_GIT_CACHE)[:-16]:
            _GIT_CACHE.pop(old, None)
        return data, error
    full = expanded if os.path.isabs(expanded) else (os.path.join(base, expanded) if base else None)
    if full is None:
        return None, "relative path and no working directory"
    try:
        stat = os.stat(full)
    except OSError:
        return None, "file not found"
    if stat.st_size > MEDIA_MAX_BYTES:
        return None, "file over %d MB" % (MEDIA_MAX_BYTES // (1024 * 1024))
    key = ("file", full, stat.st_mtime, stat.st_size)
    if key in _MEDIA_CACHE:
        return _MEDIA_CACHE[key], None
    try:
        with open(full, "rb") as handle:
            data = handle.read()
    except OSError:
        return None, "cannot read file"
    _MEDIA_CACHE[key] = data
    for old in list(_MEDIA_CACHE)[:-16]:
        _MEDIA_CACHE.pop(old, None)
    return data, None


def _media_frame(label, detail, ctx, extra_style=""):
    theme = ctx.theme
    style = "background-color: %s; border: 1px solid %s; border-radius: 6px; padding: 6px 12px; margin: 4px 0" % (
        theme.color("cardBg"), theme.color("border"))
    if extra_style:
        style += "; " + extra_style       # the program's sheet comes after the default look
    return ('<div style="%s"><span style="color: %s">&#9635; %s</span>%s</div>'
            % (_attr(style), theme.color("muted"), tsp_html.esc(label),
               '<br><span style="color: %s">%s</span>' % (theme.color("dim"), tsp_html.esc(detail)) if detail else ""))


def draw_media(node, ctx):
    """A ``media`` node: the picture of its blob or file, else a labelled frame (alt / title / path)."""
    props = node.get("p") or {}
    path = str(props.get("path") or "")
    rev = str(props.get("rev") or "")
    link = props.get("link")
    alt = tsp_html.plain_text(props.get("alt")) or tsp_html.plain_text(props.get("title"))
    name = os.path.basename(path.replace("\\", "/")) if path else ""
    label = alt or name or "media"
    if rev:
        label = "%s @ %s" % (label, rev)
    encoded = None
    detail = ""
    blob = props.get("blob")
    if blob and ctx.blobs.get(blob):
        encoded = ctx.blobs[blob]
    elif path:
        data, error = _media_bytes(path, rev, getattr(ctx, "cwd", None))
        if data is not None:
            encoded = base64.b64encode(data).decode("ascii")
            detail = "%d KB" % max(1, len(data) // 1024)
        else:
            detail = error or ""
    else:
        detail = "no path"
    info = tsp_html._image_info(encoded) if encoded else None
    style = style_for_node(node, ctx) if getattr(ctx, "rules", None) else ""
    style_attr = ' style="%s"' % _attr(style) if style else ""
    if not info or info[1] <= 0 or info[2] <= 0:
        if encoded and not detail.endswith("not a picture Sublime draws"):
            detail = (detail + " - " if detail else "") + "not a picture Sublime draws"
        return _media_frame(label, detail, ctx, style)
    mime, width, height = info
    key = "media:%s" % (link or node.get("id"))
    step = ctx.local.get(key, 0) if isinstance(getattr(ctx, "local", None), dict) else 0
    step = step if step in (0, 1, 2) else 0
    available = max(160.0, (getattr(ctx, "cols", 100) or 100) * CW - 24.0)
    if step == 0:
        scale = min(1.0, 480.0 / width, 320.0 / height)
    else:
        scale = min(float(step), available / width)
    shown_w, shown_h = max(1, int(width * scale)), max(1, int(height * scale))
    picture = '<img src="data:%s;base64,%s" width="%d" height="%d">' % (mime, encoded, shown_w, shown_h)
    caption_parts = [label, "%dx%d" % (width, height)]
    if step:
        caption_parts.append("%d%%" % int(scale * 100))
    caption = '<br><span style="color: %s">%s</span>' % (ctx.theme.color("dim"), tsp_html.esc("  ".join(caption_parts)))
    if ctx.surface_id is not None:
        event = {"local": "set", "key": key, "value": (step + 1) % 3}
        picture = '<a href="%s" title="click to zoom" style="text-decoration: none">%s</a>' % (
            tsp_html.event_href(event), picture)
    return '<div%s>%s%s</div>' % (style_attr, picture, caption)


DRAWERS = {"el": draw_el, "media": draw_media}
