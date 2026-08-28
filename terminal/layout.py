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


def gutter_digit_delta(total_lines, scrollback_cap):
    """Column-width correction to cancel ST's real gutter-digit fluctuation.

    ST's native line-number gutter (excluded from viewport_extent) widens
    by one digit every time total_lines crosses a power of ten (999->1000,
    9999->10000...). During an active full-history replay that crossing
    happens repeatedly as lines are added/rebuilt, so the real gutter
    digit count -- and therefore the measured viewport width -- genuinely
    changes mid-replay, retriggering a PTY resize that restarts the replay
    at a new width, which crosses the boundary again. See ai/TODO.md
    "4-digit gutter reserve".

    Returns (reserved_digits - actual_digits): the caller subtracts this
    many character-widths from usable_w so cols stays pinned to what it
    would be if the gutter were always reserved at scrollback_cap's digit
    width (the buffer's real ceiling, so that digit count never changes
    once reached) instead of the buffer's current, moving line count.
    """
    actual_digits = len(f"{max(int(total_lines), 1):d}")
    reserved_digits = len(f"{max(int(scrollback_cap), 1):d}")
    return reserved_digits - actual_digits


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
