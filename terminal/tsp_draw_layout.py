"""TSP drawers for: col, row, card, section, rule, spacer, status, seg, toast, overlay (and the props every node takes).

DRAWERS maps a kind to a drawer function(node, ctx) -> HTML; tsp_html registers it over its own.

minihtml has no flexbox and no widths, so the layout kinds work from character cells (see tsp_html.vis_len):
a ``row`` lays its inline children on one line and pads with spaces to honour ``justify``, ``grow``, ``min``,
``max`` and ``basis``; block children of a row stack.  What cannot be expressed is listed in each drawer.
"""

import html as _html
import re

from . import tsp_html

DRAWERS = {}


# ---- icons -----------------------------------------------------------------------------

ICON_NAMES = frozenset((
    "app-btop app-bun app-claude app-codex app-deno app-docker app-go app-hammer app-helix app-hermes app-htop "
    "app-java app-node app-omp app-python app-ruby app-rust app-vim logo tern pi-mark arrow-up arrow-down arrow-left "
    "arrow-right back forward chev chev-r chev-up corner-down-right expand shrink minimize fit zoom-in zoom-out more "
    "grip compass check x x-circle warn info help bell shield shield-alert slash slash-circle lock key key-round plus "
    "minus clock timer doc file file-code file-image file-pdf file-plus files folder folder-go folder-minus "
    "folder-open folder-plus markdown note clipboard save inbox image newspaper trash branch commit merge rebase pull "
    "push fetch stash cherry diff unified split split-down revert history play pause stop rewind fast-forward "
    "frame-next frame-prev repeat redo undo run reel mic wave vibrate power send share open download copy scissors "
    "bold italic underline type align-left align-center align-right align-justify pen eraser wand cursor selection "
    "path-insert prompt vector blur ink palette columns grid sidebar tabs-h tabs-v dock-up dock-down dock-left "
    "dock-right pip pip-tl pip-tr pip-bl pip-exit layers stack kanban canvas diagram flow peek tree box eye eye-off "
    "terminal code braces binary bug cpu database server laptop monitor gauge activity chart plug puzzle wrench gear "
    "sliders stethoscope hash tag link globe broadcast search funnel funnel-x sort-x swap list list-checks pin "
    "keyboard user users message brain sparkle lightbulb bolt flame rocket moon sun cart footprints scale log-in "
    "log-out").split())

_ICON_ALIASES = {"bash": "terminal", "shell": "terminal", "edit": "pen", "read": "doc", "web": "globe",
                 "todo": "list-checks", "task": "users", "error": "warn", "write": "file-plus",
                 "grep": "search", "glob": "search", "find": "sparkle", "ls": "folder", "fetch": "globe",
                 "browser": "globe", "eval": "code", "python": "code", "lsp": "braces", "proc": "activity"}

_LENS_FAMILY = {"git": "branch", "build": "app-hammer"}


def icon_name(name):
    """The icon's own name for an icon name or alias (anything up to the last dot dropped), or None (TspData's table)."""
    from . import tsp_draw_data
    return tsp_draw_data.icon_name(name)


def icon_glyph(name):
    """A plain-text stand-in for the icon ``name`` ("" when unknown: an unknown name draws no icon)."""
    from . import tsp_draw_data
    return tsp_draw_data.icon_glyph(name) or ""


def role_icon(role):
    """The icon a card's ``role`` implies (see the card section of the layout page), or None."""
    if not isinstance(role, str) or not role:
        return None
    parts = role.split(".")
    if parts[0] == "lens" and len(parts) > 1:
        return _LENS_FAMILY.get(parts[1], "terminal")
    if parts[:2] == ["omp", "tool"]:
        tool = parts[2] if len(parts) > 2 else ""
        name = _ICON_ALIASES.get(tool, tool)
        return name if name in ICON_NAMES else "plug"
    if len(parts) > 1:
        second = _ICON_ALIASES.get(parts[1], parts[1])
        return second if second in ICON_NAMES else None
    return None


# ---- shared pieces -----------------------------------------------------------------------

# the tone as a palette colour name (a tone is a semantic colour of the node's chrome)
TONE_TOKEN = {"neutral": "muted", "accent": "accent", "info": "accent", "success": "success", "warning": "warning",
              "error": "error", "pending": "dim", "muted": "dim", "user": "accent"}
# tones whose cards take a tint of the program's palette
TONE_FILL = {"success": "toolSuccessBg", "error": "toolErrorBg", "pending": "toolPendingBg",
             "user": "userMessageBg", "info": "infoBg"}

GAP_PX = {"none": 0, "xs": 2, "sm": 6, "md": 10, "lg": 16}
# a row's gap as spaces (a cell is about 8 px: sm = 2 cells as omp's rows have always had)
_ROW_GAP = {"none": "", "xs": "&#8201;", "sm": "&nbsp;&nbsp;", "md": "&nbsp;&nbsp;&nbsp;", "lg": "&nbsp;&nbsp;&nbsp;&nbsp;"}
_ROW_GAP_DEFAULT = "&nbsp;&nbsp;"


def _gap(props):
    """The ``gap`` prop when it is a string, else None."""
    gap = props.get("gap")
    return gap if isinstance(gap, str) else None


def _slug(name):
    """``name`` as a class-name fragment."""
    return re.sub(r"[^\w-]", "", name)



def _number(value, default=0.0):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _tone_color(ctx, tone, default="muted"):
    return ctx.theme.color(TONE_TOKEN.get(tone, default))


def _class_attr(node, *names):
    classes = [name for name in names if name]
    return ' class="%s"' % " ".join(classes) if classes else ""


def _hex(color, alpha):
    return tsp_html._hex_with_alpha(color, alpha)


def _alpha_ring(ctx, tone):
    """The ring colour of a framed node: the plain hairline, or the tone colour."""
    if tone in (None, "neutral") or tone not in TONE_TOKEN:
        return ctx.theme.color("border")
    color = _tone_color(ctx, tone)
    if tone in ("error", "warning", "success", "accent"):
        return color
    return _hex(color, 0.5) if len(color) == 7 else color


class _Within:
    """Shrink ``ctx.avail`` (the cells a line may use) while a drawer draws indented children."""

    def __init__(self, ctx, cells):
        self.ctx, self.cells = ctx, cells

    def __enter__(self):
        self.before = self.ctx.avail
        self.ctx.avail = max(8, self.before - self.cells)

    def __exit__(self, *exc):
        self.ctx.avail = self.before
        return False


def _draw_children(node, ctx, skip=None):
    """The drawn HTML of the children of ``node`` (those that draw something), leaving out ``skip``."""
    out = []
    for child in node.get("c") or []:
        if child is skip:
            continue
        piece = tsp_html.draw(child, ctx)
        if piece:
            out.append(piece)
    return out


def _head_parts(node, ctx):
    """(head HTML as inline content, the child that holds it or None).

    ``head`` is a string or spans, or the id of one of the node's own children, which is then drawn in the
    head instead of the body.  A string that names no child is plain text."""
    value = (node.get("p") or {}).get("head")
    child = None
    if isinstance(value, str):
        child = next((c for c in node.get("c") or [] if c.get("id") == value), None)
    if child is None:
        return tsp_html.text_html(value, ctx), None
    drawn = tsp_html.draw(child, ctx)
    inline = tsp_html.inline_of(drawn) if drawn else ""
    if inline is None:      # a block head (a column of lines): keep it, it stands over the body
        inline = drawn
    return inline, child


# ---- col ---------------------------------------------------------------------------------

_TEXT_ALIGN = {"center": "center", "end": "right", "start": "left"}


def _draw_col(node, ctx):
    """A vertical stack: children one under another, ``gap`` apart.

    ``align`` center/end/start aligns each child's content (text-align); baseline and stretch change nothing.
    ``justify`` and ``wrap`` have no visible effect on a stack that is as tall as its content, as in Tern.
    An empty stack draws nothing (class sf-void) -- a growing one only matters inside a row, which sees it
    through ``grow``.  Not done: virtualization (a phantom view scrolls natively, there is nothing to save)."""
    props = node.get("p") or {}
    if props.get("role") == "omp.editor":
        return tsp_html._draw_composer_box(node, ctx)
    parts = _draw_children(node, ctx)
    if not parts:
        return ""
    gap = _gap(props)
    align = props.get("align")
    classes = _class_attr(node, ("gap-%s" % _slug(gap)) if gap else None,
                          ("al-%s" % align) if align in ("start", "center", "end", "baseline", "stretch") else None,
                          ("js-%s" % props["justify"]) if props.get("justify") in ("between", "end") else None,
                          "wrap" if props.get("wrap") else None)
    style = ' style="text-align: %s"' % _TEXT_ALIGN[align] if align in _TEXT_ALIGN else ""
    if props.get("role") == "omp.editor.ask.approval":
        # omp's permission prompt: a card with an accent edge, like its other panels
        theme = ctx.theme
        style = (' style="background-color: %s; border: 1px solid %s; border-left: 3px solid %s; '
                 'border-radius: 8px; padding: 8px 12px; margin: 5px 0"'
                 % (theme.color("cardBg"), theme.color("borderMuted"), theme.color("accent")))
    return "<div%s%s>%s</div>" % (classes, style, tsp_html.stack_blocks(parts, GAP_PX.get(gap, 0)))


# ---- row ---------------------------------------------------------------------------------

def _row_items(node, ctx):
    """[{html, inline, grow, min, max}] for the children of a row, in order.

    A child that draws nothing is dropped, unless it is an empty stack that grows: that is the spacer
    that pushes what follows to the far end."""
    items = []
    children = node.get("c") or []
    # A growing text that truncates (an error line beside its buttons) gets only the room the other items leave,
    # not the whole row: drawn to the full width it pushed the buttons past the right edge.
    fillers = [child for child in children
               if child.get("k") == "text" and (child.get("p") or {}).get("truncate")
               and _number((child.get("p") or {}).get("grow")) > 0]
    early = {}
    if fillers and len(children) > len(fillers):
        used = 0
        for child in children:
            if child not in fillers:
                early[id(child)] = tsp_html.draw(child, ctx)
                used += tsp_html.vis_len(early[id(child)], ctx.cell_px) + 2
        for child in fillers:
            with _Within(ctx, used):
                early[id(child)] = tsp_html.draw(child, ctx)
    for child in children:
        cp = child.get("p") or {}
        drawn = early[id(child)] if id(child) in early else tsp_html.draw(child, ctx)
        grow = _number(cp.get("grow"))
        if not drawn:
            if grow > 0 and child.get("k") in ("row", "col") and not cp.get("hidden"):
                items.append({"html": "", "inline": True, "grow": grow, "min": None, "max": None})
            continue
        inline = tsp_html.inline_of(drawn)
        if inline is not None and drawn is not inline and child.get("k") not in ("text", "row", "col"):
            inline = None       # only a text or a nested stack that is one line sits in the line
        smallest, biggest = cp.get("min"), cp.get("max")
        basis = cp.get("basis")
        wide = tsp_html.extent_cells(smallest.get("w"), ctx.avail) if isinstance(smallest, dict) else None
        if isinstance(basis, (int, float)) and not isinstance(basis, bool) and basis > 0:
            wide = max(wide or 0, int(basis * ctx.avail))      # a fraction of the row is its starting width
        items.append({"html": inline if inline is not None else drawn, "inline": inline is not None, "grow": grow,
                      "min": wide,
                      "max": tsp_html.extent_cells(biggest.get("w"), ctx.avail) if isinstance(biggest, dict) else None})
    return items


def _lay_line(run, sep, justify, ctx):
    """One line from inline items: sizes, then the spaces that ``grow`` and ``justify`` ask for."""
    pieces, widths = [], []
    for item in run:
        piece = item["html"]
        width = tsp_html.vis_len(piece, ctx.cell_px) if piece else 0
        if item["max"] is not None and width > item["max"]:
            piece, width = tsp_html.fit_width(piece, item["max"], ctx.cell_px), item["max"]
        if item["min"] is not None and width < item["min"]:
            piece, width = piece + "&nbsp;" * (item["min"] - width), item["min"]
        pieces.append(piece)
        widths.append(width)
    sep_width = tsp_html.vis_len(sep, ctx.cell_px) if sep else 0
    extra = ctx.avail - sum(widths) - sep_width * (len(run) - 1) - 2     # 2 cells of margin: a line that wraps is worse
    seps = [sep] * (len(run) - 1)
    if extra >= 2:
        grows = [item["grow"] for item in run]
        total = sum(grows)
        if total > 0:
            given = 0
            last = max(index for index, value in enumerate(grows) if value > 0)
            for index, value in enumerate(grows):
                if value > 0:
                    share = extra - given if index == last else int(extra * value / total)
                    pieces[index] += "&nbsp;" * share
                    given += share
        elif justify == "end":
            pieces[0] = "&nbsp;" * extra + pieces[0]
        elif justify == "between" and len(run) > 1:
            per, rest = divmod(extra, len(run) - 1)
            seps = [sep + "&nbsp;" * (per + (1 if index < rest else 0)) for index in range(len(run) - 1)]
    out = []
    for index, piece in enumerate(pieces):
        out.append(piece)
        if index < len(seps):
            out.append(seps[index])
    return "".join(out)


def _draw_row(node, ctx):
    """A horizontal row.  Inline children (text, chips, keys, icons, one-line stacks) share a line, ``gap``
    apart, and the spaces that ``justify``, ``grow``, ``basis`` and ``min``/``max`` width ask for are added from
    character widths.  A block child (a card, code, a column of lines) cannot sit beside text in minihtml, so it
    stacks under the line before it; ``align`` has nothing to align on one line and ``wrap`` is the line's
    own wrapping (a row never clips, so a long line wraps where Tern would truncate)."""
    props = node.get("p") or {}
    gap = _gap(props)
    sep = _ROW_GAP.get(gap, "") if gap is not None else _ROW_GAP_DEFAULT
    items = _row_items(node, ctx)
    real = [item for item in items if item["html"]]
    if not real:
        return ""
    justify = props.get("justify")
    parts, run = [], []

    def flush():
        if run:
            parts.append(_lay_line(run, sep, justify, ctx))
            del run[:]

    only_inline = all(item["inline"] for item in items)
    for item in items:
        if item["inline"]:
            run.append(item)
        else:
            flush()
            parts.append(item["html"])
    flush()
    align = props.get("align")
    classes = _class_attr(node, ("gap-%s" % _slug(gap)) if gap else None,
                          ("al-%s" % align) if align in ("start", "center", "end", "baseline", "stretch") else None,
                          ("js-%s" % justify) if justify in ("between", "end") else None,
                          "wrap" if props.get("wrap") else None)
    if only_inline:
        style = ""
        if props.get("role") == "omp.btn":
            # omp's buttons (Details, Deny, Approve): a small bordered chip in the button's tone
            tone = _tone_color(ctx, props.get("tone"), "accent")
            style = (' style="background-color: %s; color: %s; border: 1px solid %s; border-radius: 6px; '
                     'padding: 2px 10px"' % (tsp_html._hex_with_alpha(tone, 0.16), tone, tone))
        return "<div%s%s>%s</div>" % (classes, style, parts[0])
    blocks = []
    for index, piece in enumerate(parts):
        blocks.append(piece if tsp_html.starts_block(piece.lstrip()) else "<div>%s</div>" % piece)
    return "<div%s>%s</div>" % (classes, tsp_html.stack_blocks(blocks, GAP_PX.get(gap, 0)))


# ---- spacer and rule ---------------------------------------------------------------------

_SPACER_PX = {"none": 0, "xs": 2, "sm": 6, "md": 12, "lg": 20}


def _draw_spacer(node, ctx):
    """Fixed vertical space: none 0, xs 2, sm 6, md 12 (default), lg 20 px; an unknown size is 8 px."""
    size = (node.get("p") or {}).get("size")
    size = size if isinstance(size, str) else None
    pixels = _SPACER_PX.get(size, 12 if size is None else 8)
    if pixels == 0:
        return ""
    return '<div class="sz-%s" style="font-size: 1px; line-height: %dpx">&nbsp;</div>' % (_slug(size or "md"), pixels)


def _draw_rule(node, ctx):
    """A hairline; with a ``label`` the label sits in the middle between two hairlines."""
    label = tsp_html.text_html((node.get("p") or {}).get("label"), ctx)
    color = ctx.theme.color("border")
    if not label:
        return '<div class="bare" style="border-top: 1px solid %s; margin: 6px 0"></div>' % color
    room = ctx.avail - tsp_html.vis_len(label, ctx.cell_px) - 2
    side = max(3, room // 2)
    line = '<span style="color: %s">%s</span>' % (color, "&#9472;" * side)
    return ('<div style="margin: 2px 0">%s&nbsp;<span class="sf-rule-label" style="color: %s">%s</span>&nbsp;%s</div>'
            % (line, ctx.theme.color("dim"), label, line))


# ---- card and section --------------------------------------------------------------------

STATUS_CHIPS = {"pending": ("◷", "Pending", "dim"), "running": ("●", "Running", "accent"),
                "done": ("✓", "Done", "success"), "error": ("✗", "Failed", "error"),
                "cancelled": ("■", "Canceled", "dim")}

_INNER_GAP = 8          # --sf-inner-gap


def _preview_lines(props):
    """Lines a collapsed card shows of its body: "auto" is 10, {lines: n} is n (truncated); anything else none."""
    preview = props.get("preview")
    if preview == "auto":
        return 10
    if isinstance(preview, dict):
        lines = preview.get("lines")
        if isinstance(lines, (int, float)) and not isinstance(lines, bool) and int(lines) >= 1:
            return int(lines)
    return None


def _chevron(collapsed):
    return "&#9656;" if collapsed else "&#9662;"


def _head_row(node, ctx, icon_html, head_html, right_html):
    """The head line: icon, head content, then the status chip and chevron pushed to the right edge."""
    left = icon_html + head_html
    if not (left or right_html):
        return ""
    used = tsp_html.vis_len(left, ctx.cell_px) + tsp_html.vis_len(right_html, ctx.cell_px)
    room = ctx.avail - used - 2
    pad = "&nbsp;" * room if right_html and room >= 2 else ("&nbsp;" if left and right_html else "")
    return tsp_html._toggle_link(node, ctx, left + pad + right_html)


def _draw_card(node, ctx):
    """A framed box: head row (role icon, head, status chip, chevron), body, folding with preview clamp.

    Approximated: the ring is the tone colour (tinted tones take the palette's fill) and ``selected`` is a
    2 px accent ring (no halo); ``inset`` removes the body padding but the children keep their own corners; a
    collapsed card's fade over the clamp is a plain cut and "N more lines" is a link; the status dot does
    not pulse."""
    props = node.get("p") or {}
    role = props.get("role") if isinstance(props.get("role"), str) else ""
    tone = props.get("tone") if props.get("tone") in TONE_TOKEN else None
    unframed = bool(getattr(ctx, "unframed", False))     # inside a tool or agent body: no ring, fill or chips
    bare = props.get("variant") == "bare" or unframed
    inset = bool(props.get("inset"))
    collapsible = bool(props.get("collapsible"))
    collapsed = tsp_html.fold_state(node, ctx)
    theme = ctx.theme
    inner_cells = 2 if bare else (0 if inset else 4)

    with _Within(ctx, inner_cells):
        head_html, head_child = _head_parts(node, ctx)
        status = STATUS_CHIPS.get(props.get("status"))
        chip_html = ""
        if status and not unframed:
            chip_html = tsp_html.chip("%s %s" % (status[0], status[1]), ctx, status[2])
        chevron = ""
        if collapsible:
            chevron = '<span style="color: %s">&nbsp;%s</span>' % (theme.color("dim"), _chevron(collapsed))
        has_head = bool(head_html) or bool(chip_html) or collapsible
        icon = ""
        glyph = icon_glyph(role_icon(role)) if has_head else ""
        if glyph:
            icon = '<span style="color: %s">%s</span>&nbsp;' % (
                _tone_color(ctx, tone) if tone not in (None, "neutral") else theme.color("dim"), tsp_html.esc(glyph))
        head_line = ""
        if has_head:
            head_line = '<div style="font-weight: bold">%s</div>' % _head_row(
                node, ctx, icon, head_html, chip_html + chevron)

        body_html, clamped_more = "", 0
        if not collapsed or _preview_lines(props):
            body_html = tsp_html.stack_blocks(_draw_children(node, ctx, head_child), _INNER_GAP)
            limit = _preview_lines(props) if collapsed else None
            if limit and body_html:
                cut, total = tsp_html.measure_clip(body_html, ctx.avail, limit, ctx.cell_px)
                if total > limit:
                    body_html, clamped_more = cut, total - limit
    more = ""
    if clamped_more:
        label = "%d more line%s" % (clamped_more, "" if clamped_more == 1 else "s")
        more = ('<div style="color: %s">%s</div>' % (
            theme.color("dim"), tsp_html._toggle_link(node, ctx, "%s &#9662;" % label)))
    flags = ["collapsible" if collapsible else None, "collapsed" if collapsible and collapsed else None,
             "nopreview" if collapsible and collapsed and not _preview_lines(props) else None,
             "clamped" if clamped_more else None, "selected" if props.get("selected") else None,
             "inset" if inset else None, "bare" if bare else None, "nohead" if not has_head else None]
    if status:
        flags.append("st-%s" % props.get("status"))
    classes = _class_attr(node, *flags)

    if inset and head_line:
        head_line = '<div style="padding: 8px 12px 4px 12px">%s</div>' % head_line
    if bare:
        body = '<div style="margin-left: 14px">%s</div>' % body_html if body_html else ""
        return '<div%s style="margin: 2px 0">%s%s%s</div>' % (classes, head_line, body, more)
    background = theme.color(TONE_FILL.get(tone, "cardBg"))
    border = _alpha_ring(ctx, tone)
    width, edge = "1px", ""
    if role == "omp.welcome":
        background, border, edge = theme.color("cardBg"), theme.color("borderMuted"), "border-left: 3px solid %s; " % theme.color("accent")
        head_line = ""
    elif role == "omp.user" or tone == "user":
        background = theme.color("userMessageBg")
    elif tone in ("error", "warning", "success", "accent"):
        edge = "border-left: 3px solid %s; " % _tone_color(ctx, tone)
    if props.get("selected"):
        border, width = theme.color("accent"), "2px"
    padding = "0" if inset else "8px 12px"
    return ('<div%s style="background-color: %s; border: %s solid %s; %sborder-radius: 8px; padding: %s; margin: 5px 0">'
            '%s%s%s</div>' % (classes, background, width, border, edge, padding, head_line,
                              ('<div style="margin-top: 2px">%s</div>' % body_html) if body_html and head_line else body_html,
                              more))


def _draw_section(node, ctx):
    """A lighter disclosure group: chevron and head over a body indented past a hairline.  The chevron always
    shows; a section has no icon, status, clamp, selected, inset or variant (those props are ignored)."""
    props = node.get("p") or {}
    collapsible = bool(props.get("collapsible"))
    collapsed = tsp_html.fold_state(node, ctx)
    theme = ctx.theme
    with _Within(ctx, 3):
        head_html, head_child = _head_parts(node, ctx)
        head = '<div style="color: %s">%s</div>' % (
            theme.color("muted"), tsp_html._toggle_link(node, ctx, "%s %s" % (_chevron(collapsed), head_html)))
        body = ""
        if not collapsed:
            body = tsp_html.stack_blocks(_draw_children(node, ctx, head_child), _INNER_GAP)
    classes = _class_attr(node, "collapsible" if collapsible else None,
                          "collapsed nopreview" if collapsible and collapsed else None)
    if body:
        body = ('<div style="margin-left: 5px; padding-left: 14px; border-left: 1px solid %s">%s</div>'
                % (theme.color("borderMuted"), body))
    return "<div%s>%s%s</div>" % (classes, head, body)


# ---- seg and status ----------------------------------------------------------------------

def _seg_icon_name(props):
    name = icon_name(props.get("icon"))
    if name is None and not props.get("icon"):
        role = props.get("role")
        if isinstance(role, str) and role.startswith("omp.status."):
            name = icon_name(role[len("omp.status."):])
    return name


def _draw_seg(node, ctx):
    """One status segment: icon, children, text.  ``tone`` colours the icon and lifts the text; a click
    action makes it a link (drawn by draw()).  Outside a ``status`` it is just one segment."""
    props = node.get("p") or {}
    tone = props.get("tone") if props.get("tone") in TONE_TOKEN else None
    spans = props.get("spans")
    text = tsp_html.text_html(spans, ctx) if spans else tsp_html.esc(props.get("text") or "")
    name = _seg_icon_name(props)
    icon = ""
    if name and icon_glyph(name):
        color = _tone_color(ctx, tone) if tone not in (None, "neutral") else None
        glyph = tsp_html.esc(icon_glyph(name))
        icon = ('<span style="color: %s">%s</span>&nbsp;' % (color, glyph)) if color else "%s&nbsp;" % glyph
    kids = []
    for child in node.get("c") or []:
        drawn = tsp_html.draw(child, ctx)
        inline = tsp_html.inline_of(drawn) if drawn else None
        if inline:
            kids.append(inline + "&nbsp;")
    inner = icon + "".join(kids) + text
    if not inner:
        return ""
    style = ' style="color: %s"' % ctx.theme.color("text") if tone else ""
    return "<span%s%s>&nbsp;%s&nbsp;</span>" % (_class_attr(node), style, inner)


def _draw_status(node, ctx):
    """A status strip: left group, right group (``seg`` with ``side`` right).  While the segments are wider than
    the row the one with the lowest ``priority`` is dropped (ties: the one farthest from its edge, the right
    side on an exact tie); a seg with ``min.w`` shrinks to it with an ellipsis instead, and the last one
    standing truncates.  Widths are counted in character cells."""
    props = node.get("p") or {}
    theme = ctx.theme
    items, extra_blocks = [], []
    side_index = {"left": 0, "right": 0}
    for child in node.get("c") or []:
        drawn = tsp_html.draw(child, ctx)
        if not drawn:
            continue
        cp = child.get("p") or {}
        if child.get("k") != "seg":
            inline = tsp_html.inline_of(drawn)
            if inline is None:
                extra_blocks.append(drawn)
            else:
                items.append({"html": inline, "seg": False, "side": "left", "priority": 0, "min": None,
                              "order": 0, "w": tsp_html.vis_len(inline, ctx.cell_px)})
            continue
        side = "right" if cp.get("side") == "right" else "left"
        order = side_index[side] * 2 + (1 if side == "right" else 0)
        side_index[side] += 1
        width = tsp_html.vis_len(drawn, ctx.cell_px)
        smallest = cp.get("min")
        minimum = tsp_html.extent_cells(smallest.get("w"), ctx.avail) if isinstance(smallest, dict) else None
        items.append({"html": drawn, "seg": True, "side": side, "priority": _number(cp.get("priority")),
                      "min": min(minimum + 2, width) if minimum is not None else None, "order": order, "w": width})
    room = ctx.avail - 2        # the strip's own padding
    kept = list(items)
    while sum((i["min"] if i["min"] is not None else i["w"]) for i in kept) > room:
        segs = [i for i in kept if i["seg"]]
        if len(segs) <= 1:
            break
        victim = min(segs, key=lambda i: (i["priority"], -i["order"]))
        kept.remove(victim)
    over = sum(i["w"] for i in kept) - room
    if over > 0:
        for item in kept:
            if item["min"] is not None and item["w"] > item["min"] and over > 0:
                cut = min(over, item["w"] - item["min"])
                item["html"] = tsp_html.fit_width(item["html"], item["w"] - cut, ctx.cell_px)
                item["w"] -= cut
                over -= cut
        segs = [i for i in kept if i["seg"]]
        if over > 0 and len(segs) == 1:     # the last one standing truncates below its minimum
            only = segs[0]
            only["html"] = tsp_html.fit_width(only["html"], max(3, only["w"] - over), ctx.cell_px)
            only["w"] = max(3, only["w"] - over)
    left = "".join(i["html"] for i in kept if i["side"] == "left")
    right = "".join(i["html"] for i in kept if i["side"] == "right")
    if not (left or right) and not extra_blocks:
        return ""
    gap = room - sum(i["w"] for i in kept) - 1
    middle = "&nbsp;" * gap if right and gap >= 1 else ""
    fill = ""
    if not props.get("transparent") and "statusLineBg" in theme.tokens:
        fill = "background-color: %s; " % theme.color("statusLineBg")
    classes = _class_attr(node, "transparent" if props.get("transparent") else None)
    return ('<div%s style="%scolor: %s; border-top: 1px solid %s; padding: 2px 8px">%s%s%s</div>%s'
            % (classes, fill, theme.color("muted"), theme.color("borderMuted"), left, middle, right,
               "".join(extra_blocks)))


# ---- toast -------------------------------------------------------------------------------

TOAST_LIFE_MS = {"success": 2200, "info": 5200, "error": 5200}


def toast_tone(props):
    """error -> "error"; info or accent -> "info"; anything else -> "success"."""
    tone = props.get("tone")
    return "error" if tone == "error" else ("info" if tone in ("info", "accent") else "success")


def toast_key(node):
    """Identity of one showing: a toast shows again when its text, sub, tone or ttl changes."""
    props = node.get("p") or {}
    return "toast:%s|%s|%s|%s|%s" % (node.get("id"), props.get("text"), props.get("sub"), props.get("tone"),
                                     props.get("ttl"))


def toast_life_ms(props):
    """How long the notice stays: 2.2 s (success) or 5.2 s (info, error); ``ttl`` can only shorten it."""
    life = TOAST_LIFE_MS[toast_tone(props)]
    ttl = props.get("ttl")
    if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl >= 0:
        life = min(life, int(ttl))
    return life


def scan_toasts(children, ctx):
    """Choose the one toast the window shows (the newest that is still up) and remember it in ``ctx.toast_winner``.

    One toast shows at a time in the whole window: a newer one replaces the one up."""
    alive = []
    counter = [0]

    def walk(nodes):
        for node in nodes:
            if node.get("k") == "toast" and not (node.get("p") or {}).get("hidden"):
                counter[0] += 1
                age = ctx.clock.visible_ms(toast_key(node))
                if age <= toast_life_ms(node.get("p") or {}):
                    alive.append((age, -counter[0], node.get("id")))
            walk(node.get("c") or [])

    walk(children)
    ctx.toast_winner = min(alive)[2] if alive else None


def _toast_html(props, ctx):
    tone = toast_tone(props)
    theme = ctx.theme
    glyph, color = {"error": ("&#9650;", theme.color("error")), "info": ("&#9679;", theme.color("accent")),
                    "success": ("&#10003;", theme.color("success"))}[tone]
    sub = props.get("sub")
    detail = ""
    if isinstance(sub, str) and sub.strip():
        first = sub.splitlines()[0][:120]
        detail = '&nbsp;&nbsp;<tt style="color: %s">%s</tt>' % (theme.color("dim"), tsp_html.esc(first))
    return ('<div class="toast %s" style="background-color: %s; color: %s; border: 1px solid %s; border-radius: 12px; '
            'padding: 4px 12px; margin: 4px 0"><span style="color: %s">%s</span>&nbsp;%s%s</div>'
            % ({"error": "err", "info": "info", "success": ""}[tone], theme.color("infoBg"), theme.color("text"),
               color, color, glyph, tsp_html.esc(props.get("text") or ""), detail))


def _draw_toast(node, ctx):
    """A transient notice.  Tern shows it at the bottom of the window, one at a time, for 2.2 s (success) or
    5.2 s (info, error), shortened by ``ttl``; it shows again when text, sub, tone or ttl change.

    With ``ctx.toast_sink`` (a list) the node draws nothing in the flow and the notice goes to the sink so the
    view can show it at the bottom of the window; without it the notice is drawn where the node is."""
    props = node.get("p") or {}
    age = ctx.clock.visible_ms(toast_key(node))
    if age > toast_life_ms(props):
        return ""
    winner = getattr(ctx, "toast_winner", "unscanned")
    if winner != "unscanned" and winner != node.get("id"):
        return ""
    ctx.animated = True         # keep redrawing until it expires
    notice = _toast_html(props, ctx)
    sink = getattr(ctx, "toast_sink", None)
    if sink is not None:
        del sink[:]
        sink.append(notice)
        return ""
    return notice


# ---- overlay -----------------------------------------------------------------------------

_OVERLAY_SIZE = ("sm", "md", "lg", "full")


def overlay_covers(node):
    """True for an overlay that covers the view under it: a ``modal`` or ``full`` one (or a picker)."""
    props = node.get("p") or {}
    return node.get("k") == "picker" or (node.get("k") == "overlay" and (bool(props.get("modal")) or props.get("size") == "full"))


def _draw_overlay(node, ctx):
    """A framed card with an optional title row (``head``) over its children, 4 px apart.

    Floating is not possible in a phantom: the card is drawn in the flow of the layer, so ``anchor`` only
    sets its class, ``size`` is the cell width of the card (sm 40, md 60, lg 90) at most, and ``modal`` adds
    the class (no dim backdrop).  ``aria`` is the card's accessible name (an attribute)."""
    props = node.get("p") or {}
    theme = ctx.theme
    anchor = props.get("anchor")
    placement = "at-center"
    if anchor == "top":
        placement = "at-top"
    elif anchor == "bottom":
        placement = "at-bottom"
    elif isinstance(anchor, dict) and anchor.get("node"):
        placement = "at-node"
    elif isinstance(anchor, dict) and anchor.get("caret"):
        placement = "at-caret"
    size = props.get("size") if props.get("size") in _OVERLAY_SIZE else ("sm" if placement in ("at-node", "at-caret") else "md")
    cells = {"sm": 40, "md": 60, "lg": 90}.get(size)
    with _Within(ctx, 4 + (max(0, ctx.avail - cells) if cells else 0)):
        head = tsp_html.text_html(props.get("head"), ctx)
        head_row = ""
        if tsp_html.plain_text(props.get("head")).strip():
            role = props.get("role") if isinstance(props.get("role"), str) else ""
            name = icon_name(_ICON_ALIASES.get(role.split(".")[-1], role.split(".")[-1])) if role.startswith("omp.overlay.") else None
            icon = ('<span style="color: %s">%s</span>&nbsp;' % (theme.color("dim"), tsp_html.esc(icon_glyph(name)))) if name else ""
            head_row = '<div class="sf-ov-head" style="font-weight: bold">%s%s</div>' % (icon, head)
        body = tsp_html.stack_blocks(_draw_children(node, ctx), 4)
    inner = head_row + ('<div class="sf-ov-body" style="margin-top: 4px">%s</div>' % body if body and head_row else body)
    if not inner:
        return ""
    aria = props.get("aria") if isinstance(props.get("aria"), str) else tsp_html.plain_text(props.get("head"))
    classes = _class_attr(node, "sz-%s" % size, placement, "modal" if props.get("modal") else None,
                          None if head_row else "nohead")
    label = ' aria-label="%s"' % _html.escape(aria, quote=True) if aria else ""
    return ('<div%s%s style="background-color: %s; border: 1px solid %s; border-radius: 10px; padding: 6px 12px; margin: 5px 0">%s</div>'
            % (classes, label, theme.color("cardBg"), theme.color("borderAccent"), inner))


DRAWERS.update({
    "col": _draw_col, "row": _draw_row, "card": _draw_card, "section": _draw_section, "rule": _draw_rule,
    "spacer": _draw_spacer, "status": _draw_status, "seg": _draw_seg, "toast": _draw_toast,
    "overlay": _draw_overlay,
})
