"""Read a native TSP tab (omp) as plain text.

The tab's own buffer holds one blank line per panel and the panels are phantoms, so nothing that reads view
text can see them.  The content lives in the TSP document of the terminal (``term._tsp``); this module turns
it into plain text, one entry per top-level panel.  No Sublime dependency except the view passed in, so
sublime-mcp can call it for any terminal tab.
"""

from . import tsp_html, tsp_text

DEFAULT_COLS = 100
DEFAULT_LAST = 20
DEFAULT_MAX_CHARS = 2000


def _text(node, cols):
    try:
        lines = tsp_text._draw(node, cols)
    except Exception:
        lines = []
    text = "\n".join(lines).strip("\n")
    return text if text.strip() else tsp_html.node_text(node)


def _cut(text, max_chars):
    if max_chars and len(text) > max_chars:
        return text[:max_chars] + " ...[%d more characters]" % (len(text) - max_chars)
    return text


def read_surface(term, last=DEFAULT_LAST, max_chars=DEFAULT_MAX_CHARS, cols=DEFAULT_COLS):
    """The newest surface of ``term`` as {panels, overlay, dock, view}; an {"error": ...} when it has none."""
    host = getattr(term, "_tsp", None)
    if host is None:
        return {"error": "this tab has no TSP host (tsp_enabled is off for its profile)"}
    surface_id = host.active_surface()
    if surface_id is None:
        return {"error": "the program is not drawing natively right now (no open surface)"}
    snapshot = host.documents[surface_id].snapshot()
    regions = {region.get("id"): region.get("c") or [] for region in snapshot.get("c") or []}
    main = regions.get("main", [])
    first = max(0, len(main) - last) if last and last > 0 else 0
    panels = []
    for index in range(first, len(main)):
        node = main[index]
        kind, caption = tsp_html.entry_outline(node)
        props = node.get("p") or {}
        panels.append({
            "index": index,
            "kind": kind,
            "k": node.get("k"),
            "role": props.get("role"),
            "title": tsp_html.plain_text(props.get("title")) or caption,
            "text": _cut(_text(node, cols), max_chars),
        })
    overlays = regions.get("layer", [])
    dock = "\n".join(_text(node, cols) for node in regions.get("dock", []))
    view = term.view
    y = view.viewport_position()[1]
    height = view.viewport_extent()[1]
    layout = view.layout_extent()[1]
    return {
        "surface": surface_id,
        "total_panels": len(main),
        "returned_from": first,
        "panels": panels,
        "overlay": _cut(_text(overlays[-1], cols), max_chars * 4) if overlays else None,
        "overlays_open": len(overlays),
        "dock": _cut(dock, max_chars),
        "view": {"scroll_y": y, "viewport_height": height, "layout_height": layout,
                 "at_bottom": y + height >= layout - 2 * (view.line_height() or 20)},
    }
