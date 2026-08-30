"""Build view text + colour region list from screen cells.

Pure Python — no Sublime imports.
"""
from .colors import REVERSE, pack_attr, scope_name_for

# Baked into the ST color scheme (see ai_terminal._HOST_CURSOR_SCOPE). Used when
# the host synthesizes a cursor so visibility does not depend on dynamic
# ai.fb.* registration / flush races.
HOST_CURSOR_SCOPE = "ai.terminal.host_cursor"


def cell_needs_host_cursor(rows, cy, cx):
    """True when the PTY cursor cell is not already SGR-reverse (app cursor)."""
    if rows is None or cy is None or cx is None:
        return False
    if cy < 0 or cx < 0 or cy >= len(rows):
        return False
    row = rows[cy]
    if cx >= len(row):
        # Past rstripped blanks — shell/Grok insertion point.
        return True
    _ch, attr = row[cx]
    return not bool(attr & REVERSE)


# Blank-cell cursor glyphs (display-only, never sent to the PTY), keyed by
# Screen.cursor_shape (DECSCUSR, via GhosttyRenderStateCursorVisualStyle).
# Bright-white-on-black attr: ST often drops region *fill* and keeps only
# foreground. Reverse-default is black-on-white → black glyph on black
# terminal when fill is dropped = invisible. White glyph stays visible
# without fill.
# Mid-line (on a real character) has no shape to swap -- there is only one
# glyph per cell in a monospace text buffer, so a bar/underline cursor there
# would either hide the character or require a second column. That case
# always uses colour-reversal instead, regardless of shape (see below).
_HOST_CURSOR_GLYPHS = {
    "block": "\u2588",      # █ full block
    "bar": "\u258f",        # ▏ left one eighth block
    "underline": "\u2581", # ▁ lower one eighth block
    "hollow": "\u25af",    # ▯ white vertical rectangle
}
_HOST_CURSOR_ATTR = pack_attr(fg=16, bg=1)  # → ai.fb.16.1 white on near-black


def paint_host_cursor(rows, cy, cx, shape="block"):
    """Ensure a visible cursor cell when the app did not SGR-reverse it.

    ST host caret is invisible (matches bg) so Claude/ratatui reverse-video is
    not doubled. Apps that never reverse the cursor cell need a host paint:

    - Blank / EOL insertion point → shaped glyph per DECSCUSR (block/bar/
      underline/hollow), visible even if region fill drops.
    - Mid-line on a real glyph → keep the character, OR REVERSE (do not
      replace it) -- shape does not apply here, see module comment above.
      Without this, left/right onto typed text has zero visible cursor.

    Display-only overlay; never written back to the Screen grid.

    Returns (rows, host_painted).
    """
    if rows is None or cy is None or cx is None:
        return rows, False
    if cy < 0 or cx < 0 or cy >= len(rows):
        return rows, False
    rows = list(rows)
    row = list(rows[cy])
    # Cursor often sits past rstripped trailing spaces; pad so the cell exists.
    while len(row) <= cx:
        row.append((" ", 0))
    ch, attr = row[cx]
    if attr & REVERSE:
        rows[cy] = row
        return rows, False
    if not ch or ch in (" ", "\u00a0"):
        # EOL / blank insertion point — shaped glyph path.
        glyph = _HOST_CURSOR_GLYPHS.get(shape, _HOST_CURSOR_GLYPHS["block"])
        row[cx] = (glyph, _HOST_CURSOR_ATTR)
    else:
        # Mid-line — invert the real character (do not replace with a glyph).
        row[cx] = (ch, attr | REVERSE)
    rows[cy] = row
    return rows, True


def cursor_text_offset(rows, cy, cx):
    """Character offset of terminal cell (cy, cx) in flattened view text."""
    if rows is None or cy is None or cx is None:
        return None
    if cy < 0 or cx < 0 or cy >= len(rows):
        return None
    if cx >= len(rows[cy]):
        return None
    off = 0
    for i in range(cy):
        off += sum(len(ch) for ch, _attr in rows[i]) + 1  # +1 newline
    return off + sum(len(ch) for ch, _attr in rows[cy][:cx])


def punch_host_cursor_region(regions, off, end=None):
    """Make HOST_CURSOR_SCOPE exclusive over [off, end).

    ST draws overlapping add_regions scopes with undefined z-order: an ai.fb.*
    run that covers the cursor cell often wins mid-line (styled prompt text),
    so the grey host block only shows at EOL where no colour run exists.
    Split/remove any region covering the cell, then append host_cursor alone.
    """
    if off is None:
        return regions
    end = off + 1 if end is None else end
    if end <= off:
        return regions
    out = []
    for item in regions or []:
        b, e, scope = item[0], item[1], item[2]
        if e <= off or b >= end:
            out.append([b, e, scope])
            continue
        if b < off:
            out.append([b, off, scope])
        if e > end:
            out.append([end, e, scope])
    out.append([off, end, HOST_CURSOR_SCOPE])
    return out


def trim_display_rows(rows, cy):
    """Drop trailing blanks, including a cursor parked below the content.

    Keep the last non-blank row. Also keep a blank cursor row when it is
    the next line after that content (empty shell prompt). A cursor two
    or more rows below — Claude parking on the last PTY line after a
    last-row CUP + overflow \\n — is not kept.
    """
    if not rows:
        return rows
    last_nb = -1
    for i, row in enumerate(rows):
        if any(ch.strip() for ch, _ in row):
            last_nb = i
    if last_nb < 0:
        keep = 0 if cy is None else max(0, min(cy, len(rows) - 1))
        return rows[: keep + 1]
    keep = last_nb
    if cy is not None and last_nb < cy <= last_nb + 1:
        keep = min(cy, len(rows) - 1)
    return rows[: keep + 1]


def build_text_and_regions(rows, scope_for=None):
    """Flatten structured rows into view text + [begin, end, scope] regions.

    scope_for: optional callable(attr)->scope; defaults to pure scope_name_for.
    Adjacent equal-scope cells are coalesced. NBSP normalized to space.
    """
    if scope_for is None:
        scope_for = scope_name_for
    parts = []
    regs = []
    offset = 0
    for cells in rows:
        run_scope = None
        run_start = -1
        for ch, attr in cells:
            parts.append(ch)
            scope = scope_for(attr) if attr else None
            if scope != run_scope:
                if run_scope is not None:
                    regs.append([run_start, offset, run_scope])
                run_scope = scope
                run_start = offset
            # One terminal cell can contain a multi-codepoint grapheme (for
            # example ``e`` + COMBINING ACUTE or an emoji ZWJ sequence).
            # Sublime regions index the flattened text, not terminal cells.
            offset += len(ch)
        if run_scope is not None:
            regs.append([run_start, offset, run_scope])
        parts.append("\n")
        offset += 1
    if parts and parts[-1] == "\n":
        parts.pop()
    text = "".join(parts).replace(" ", " ")
    return text, regs
