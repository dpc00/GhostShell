"""TSP text renderer: turns a TspDocument into plain lines of text.

This is the OUTPUT (SINK) block of GhostShell's TSP support.  GhostShell shows a
terminal as text in a Sublime view, so each component kind is drawn as its
plain-text equivalent: a card is a head line with its content indented under
it, a progress node is a bar made of characters, and so on.

Kinds that have no sensible text form (an image, a chart) are drawn as a short
label.  A kind this file does not know is drawn as its children, so a newer
omp never makes the screen go blank.

Each ``_draw_<kind>`` function takes a wire-shaped node dict (see
tsp_document.py) and the width in cells, and returns a list of strings.
Nothing here touches Sublime.
"""

import textwrap

INDENT = "  "


def render_lines(document, width=100):
    """The whole document as a list of text lines."""
    return _draw(document.snapshot(), width)


def plain(value):
    """Text given as a str or as a list of {"t": text, ...} spans, as one str."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(span.get("t", "") for span in value if isinstance(span, dict))
    return str(value)


def _draw(node, width):
    props = node.get("p") or {}
    if props.get("hidden"):
        return []
    handler = _DRAWERS.get(node.get("k"), _draw_children)
    return handler(node, width)


def _children(node, width):
    lines = []
    for child in node.get("c") or []:
        lines.extend(_draw(child, width))
    return lines


def _indent(lines, prefix=INDENT):
    return [prefix + line if line else line for line in lines]


def _draw_children(node, width):
    return _children(node, width)


def _draw_row(node, width):
    # Children that are one line each sit side by side; anything taller is stacked.
    parts = [_draw(child, width) for child in node.get("c") or []]
    parts = [lines for lines in parts if lines]
    if all(len(lines) == 1 for lines in parts):
        return ["  ".join(lines[0] for lines in parts)] if parts else []
    return [line for lines in parts for line in lines]


def _draw_card(node, width):
    props = node.get("p") or {}
    marks = {"running": "●", "done": "✓", "error": "✗", "cancelled": "○", "pending": "◌"}
    head = plain(props.get("head"))
    status = marks.get(props.get("status"), "")
    lines = [" ".join(part for part in (status, head) if part)]
    if props.get("collapsed"):
        return lines
    return lines + _indent(_children(node, width))


def _draw_section(node, width):
    props = node.get("p") or {}
    head = plain(props.get("head"))
    arrow = "▸" if props.get("collapsed") else "▾"
    lines = [arrow + " " + head]
    return lines if props.get("collapsed") else lines + _indent(_children(node, width))


def _draw_rule(node, width):
    label = plain((node.get("p") or {}).get("label"))
    if not label:
        return ["─" * min(width, 80)]
    return ["── " + label + " " + "─" * max(0, min(width, 80) - len(label) - 4)]


def _draw_spacer(node, width):
    return [""]


def _draw_text_block(node, width):
    props = node.get("p") or {}
    text = props.get("text")
    if text is None and props.get("spans"):
        text = plain(props["spans"])
    text = text or ""
    lines = []
    for raw in text.split("\n"):
        wrapped = textwrap.wrap(raw, width) if props.get("wrap") != "none" else [raw]
        lines.extend(wrapped or [""])
    return lines


def _draw_code(node, width):
    props = node.get("p") or {}
    lines = (props.get("text") or "").split("\n")
    if props.get("numbers"):
        start = props.get("start") or 1
        lines = ["%4d  %s" % (start + i, line) for i, line in enumerate(lines)]
    return _indent(lines)


def _draw_raw_text(node, width):
    """Kinds whose text is already laid out (diff, ansi): keep its lines."""
    return ((node.get("p") or {}).get("text") or "").split("\n")


def _draw_image(node, width):
    props = node.get("p") or {}
    return ["[image: %s]" % (props.get("alt") or props.get("builtin") or "picture")]


def _draw_kv(node, width):
    items = (node.get("p") or {}).get("items") or []
    return ["%s: %s" % (plain(item.get("k")), plain(item.get("v"))) for item in items]


def _draw_table(node, width):
    props = node.get("p") or {}
    columns = props.get("cols") or []
    lines = []
    if columns:
        lines.append("  ".join(plain(col.get("head")) or col.get("id", "") for col in columns))
    for row in props.get("rows") or []:
        cells = row.get("cells") or {}
        lines.append("  ".join(plain(cells.get(col.get("id"))) for col in columns))
    return lines


def _draw_tree_nodes(nodes, depth):
    lines = []
    for item in nodes:
        lines.append(INDENT * depth + plain(item.get("label")))
        lines.extend(_draw_tree_nodes(item.get("children") or [], depth + 1))
    return lines


def _draw_tree(node, width):
    return _draw_tree_nodes((node.get("p") or {}).get("nodes") or [], 0)


def _draw_badge(node, width):
    return ["[%s]" % (node.get("p") or {}).get("text", "")]


def _draw_kbd(node, width):
    return ["[%s]" % "+".join((node.get("p") or {}).get("keys") or [])]


def _draw_icon(node, width):
    return ["*"]


def _draw_spinner(node, width):
    return ["⠋ " + plain((node.get("p") or {}).get("label"))]


def _draw_shimmer(node, width):
    props = node.get("p") or {}
    return [props.get("text") or plain(props.get("spans"))]


def _format_age(milliseconds):
    seconds = (milliseconds or 0) / 1000.0
    if seconds < 60:
        return "%.1fs" % seconds
    return "%dm%02ds" % (seconds // 60, seconds % 60)


def _draw_elapsed(node, width):
    props = node.get("p") or {}
    return [_format_age(props.get("stopped", props.get("age")))]


def _bar(value, size=20):
    if value is None:
        return "[" + "·" * size + "]"
    filled = int(round(max(0.0, min(1.0, value)) * size))
    return "[" + "█" * filled + "░" * (size - filled) + "]"


def _draw_progress(node, width):
    props = node.get("p") or {}
    value = props.get("value")
    label = plain(props.get("label"))
    percent = "" if value is None else " %d%%" % round(value * 100)
    return [(_bar(value) + percent + " " + label).rstrip()]


def _draw_meter(node, width):
    props = node.get("p") or {}
    label = plain(props.get("label"))
    return [(_bar(props.get("value"), 12) + " " + label).rstrip()]


def _draw_rate(node, width):
    props = node.get("p") or {}
    return ["%s %s" % (props.get("value"), props.get("unit", ""))]


def _draw_list(node, width):
    selected = (node.get("p") or {}).get("selected")
    lines = []
    for child in node.get("c") or []:
        item = child.get("p") or {}
        marker = "›" if child.get("id") == selected else " "
        line = "%s %s" % (marker, plain(item.get("label")))
        if item.get("detail"):
            line += "  " + plain(item["detail"])
        lines.append(line)
    return lines


def _draw_item(node, width):
    props = node.get("p") or {}
    line = "  " + plain(props.get("label"))
    if props.get("detail"):
        line += "  " + plain(props["detail"])
    return [line]


def _draw_tabs(node, width):
    props = node.get("p") or {}
    parts = []
    for item in props.get("items") or []:
        label = plain(item.get("label"))
        parts.append("[%s]" % label if item.get("id") == props.get("active") else label)
    return [" ".join(parts)]


def _draw_editor(node, width):
    props = node.get("p") or {}
    text = props.get("text") or ""
    if not text:
        text = props.get("placeholder") or ""
    cursor = props.get("cursor")
    if props.get("text") and isinstance(cursor, int) and 0 <= cursor <= len(text):
        text = text[:cursor] + "▏" + text[cursor:]
    lines = (text + (props.get("ghost") or "")).split("\n")
    prompt = plain(props.get("prompt")) or "> "
    return [prompt + lines[0]] + lines[1:]


def _draw_status(node, width):
    segs = [child for child in node.get("c") or [] if child.get("k") == "seg"]
    return [" · ".join(plain((seg.get("p") or {}).get("spans")) for seg in segs)]


def _draw_seg(node, width):
    return [plain((node.get("p") or {}).get("spans"))]


def _draw_toast(node, width):
    return ["[ %s ]" % (node.get("p") or {}).get("text", "")]


def _draw_overlay(node, width):
    head = plain((node.get("p") or {}).get("head"))
    return ([head] if head else []) + _children(node, width)


def _draw_rows(node, width):
    return list((node.get("p") or {}).get("lines") or [])


def _draw_picker(node, width):
    props = node.get("p") or {}
    lines = [plain(props.get("title")) or "Select"]
    for item in props.get("items") or []:
        marker = "›" if item.get("id") == props.get("selected") else " "
        lines.append("%s %s" % (marker, plain(item.get("label"))))
    return lines


def _draw_prefs(node, width):
    props = node.get("p") or {}
    lines = [props.get("title", "Settings")]
    for section in props.get("sections") or []:
        lines.append(INDENT + section.get("title", ""))
        for row in section.get("rows") or []:
            lines.append(INDENT * 2 + row.get("label", ""))
    return lines


def _draw_tool(node, width):
    props = node.get("p") or {}
    marks = {"running": "●", "done": "✓", "error": "✗", "cancelled": "○", "pending": "◌"}
    head = " ".join(part for part in (
        marks.get(props.get("status"), ""), plain(props.get("title")), plain(props.get("target")),
    ) if part)
    if props.get("collapsed"):
        return [head]
    return [head] + _indent(_children(node, width))


def _draw_checklist(node, width):
    marks = {"done": "[x]", "active": "[>]", "blocked": "[!]", "dropped": "[-]", "pending": "[ ]"}
    lines = []
    for phase in (node.get("p") or {}).get("phases") or []:
        lines.append(plain(phase.get("title")))
        for item in phase.get("items") or []:
            lines.append(INDENT + marks.get(item.get("status"), "[ ]") + " " + plain(item.get("text")))
    return lines


def _draw_agent(node, width):
    props = node.get("p") or {}
    head = "%s (%s) %s" % (props.get("name", "agent"), props.get("status", ""), plain(props.get("task")))
    return [head.rstrip()] + _indent(_children(node, width))


def _draw_chart(node, width):
    return ["[chart] " + plain((node.get("p") or {}).get("summary"))]


def _draw_effort(node, width):
    return ["effort: %s" % (node.get("p") or {}).get("level", "")]


# One entry per kind in tsp_document.TSP_KINDS; a kind missing here is drawn as its children.
_DRAWERS = {
    "col": _draw_children, "row": _draw_row, "card": _draw_card,
    "section": _draw_section, "rule": _draw_rule, "spacer": _draw_spacer,
    "text": _draw_text_block, "md": _draw_text_block, "math": _draw_text_block,
    "code": _draw_code, "diff": _draw_raw_text, "ansi": _draw_raw_text,
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
