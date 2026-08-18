"""Viewport-measure helpers. Pure Python — no Sublime imports."""


def accepted_cols(last_cols, measured_cols):
    """Return the column count the PTY should use.

    last_cols is the size last applied (or None on first measure).
    measured_cols is what _measure just reported.

    Growing by exactly one column is the 32↔33 attractor (line-number
    gutter digit width and/or H-scrollbar toggling). Shrinking by one
    is how we lock onto the size that fits. A real sash-drag is ≥2.
    """
    if last_cols is None:
        return measured_cols
    if measured_cols == last_cols + 1:
        return last_cols
    return measured_cols


def follow_line_count(total_lines, ignore_trailing=0):
    """Snap-to-bottom line count after dropping a configured trailer.

    ignore_trailing comes from the follow_ignore_trailing_lines setting
    (default 0). The engine does not inspect line contents.
    """
    try:
        n = int(total_lines)
        drop = int(ignore_trailing)
    except (TypeError, ValueError):
        return 0
    if drop <= 0:
        return max(0, n)
    return max(0, n - drop)
