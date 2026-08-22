"""Display caret for TUIs that park the hardware cursor off the input field.

Why this exists
---------------
Claude Code (main-screen mode) often CUPs to the footer to repaint token/cost
lines and *leaves* the hardware cursor there. The edit buffer still lives on
the `>` prompt row. A faithful PTY→ST caret mapping then puts the ST caret on
the footer while the user is editing the prompt — left/right appear to do
nothing until Claude happens to CUP back to the prompt (feels like multiple
keystrokes / wrong position).

Grok keeps the hardware cursor on its input row (`│ > … │` in a box). That row
must win over earlier transcript lines that also start with `>`. If we lock
onto a history `>` and remap, the ST caret jumps to the wrong line (e.g. mid-
scrollback) and the real command line looks cursorless.

Terminus feels fine when the TUI keeps the hardware cursor on the input field.
When the TUI parks the cursor on the status bar, every host that draws a
caret from the PTY cursor needs a display mapping. We only remap when we
detect a `>` prompt row *and* the hardware cursor is below the input box.

Column rules
------------
- Input starts after the prompt marker: `>` plus an optional space/NBSP.
  Leading spaces and box borders (`│`) are not the prompt marker.
- Editable columns are [input_start, content_end]. content_end is after the
  last non-blank of the *input field* (not right-side chrome / clock).
- We remember the last editable column while the hardware cursor is on the
  prompt; when parked on the status bar we restore that column.
"""

# Skip when scanning for `>`: spaces and Grok/box vertical borders.
# Without this, Grok's `│ >` row is invisible to find_prompt_row (first
# non-space is `│`), so an older transcript `> …` wins and the caret remaps
# to the wrong line.
_BOX_V = frozenset(
    (
        "\u2502",  # │
        "\u2503",  # ┃
        "\u2551",  # ║
        "\u2524",  # ┤
        "\u251c",  # ├
        "|",
    )
)
_PROMPT_PREFIX = frozenset((" ", "\u00a0")) | _BOX_V

# Prompt marker glyphs, by CLI. ">" covers plain shells and Grok. Claude
# Code uses U+276F (heavy right-pointing angle quotation mark ornament, a
# chevron) instead of a plain ">" -- verified against a live Claude Code
# session; the plain ">" check silently never matched it, so
# find_prompt_row always returned None while running Claude Code. Add the
# next CLI's marker here when it differs too -- real per-CLI work, not
# something one generic check can stand in for.
_PROMPT_MARKERS = frozenset((">", "\u276f"))


def _prompt_marker_col(screen, prompt_y):
    """Column of the `>` prompt marker on the row, or None.

    Allows leading spaces and vertical box borders (Grok: `  │ > text │`).
    The marker must be the first non-prefix cell (not a `>` buried in text).
    """
    row = screen.grid[prompt_y]
    if not row:
        return None
    n = min(len(row), screen.cols)
    for i in range(n):
        ch = row[i]
        if ch in _PROMPT_PREFIX:
            continue
        return i if ch in _PROMPT_MARKERS else None
    return None


def find_prompt_row(screen):
    """Last grid row with a `>` prompt marker (spaces/box border prefix OK).

    Prefers the bottom-most match so Grok's live input box wins over earlier
    transcript lines that also use a leading `>`.
    """
    found = None
    for y in range(screen.rows):
        if _prompt_marker_col(screen, y) is not None:
            found = y
    return found


def input_start_col(screen, prompt_y):
    """First editable column on the prompt row (after `>` and optional blank)."""
    row = screen.grid[prompt_y]
    if not row:
        return 0
    m = _prompt_marker_col(screen, prompt_y)
    if m is None:
        return 0
    # After `>`; skip one following space/NBSP if present.
    nxt = m + 1
    if nxt < len(row) and row[nxt] in (" ", "\u00a0"):
        return nxt + 1
    return nxt


# Long blank run after input text = pad before right-side chrome (Grok clock).
# Single/double spaces stay part of the typed field; 4+ ends the field.
_CONTENT_GAP = 4


def content_end_col(screen, prompt_y):
    """Column after last non-blank of the *input field* (not right-side chrome).

    Grok paints a clock on the same row as `>` (e.g. `7:52 AM` after a long
    pad). Using the absolute last non-blank parked the host caret on the clock
    so the command line looked cursorless. Stop at a run of GAP blanks; text
    after that gap is chrome. Box vertical borders end the field immediately.
    Empty field (only chrome) seats at input_start.
    """
    row = screen.grid[prompt_y]
    start = input_start_col(screen, prompt_y)
    end = start
    gap = 0
    seen_text = False
    n = min(len(row), screen.cols)
    for i in range(start, n):
        ch = row[i]
        if ch in _BOX_V:
            break
        if ch in (" ", "\u00a0"):
            gap += 1
            if seen_text and gap >= _CONTENT_GAP:
                break
            continue
        # Non-blank after a long pad from field start → right chrome only.
        if gap >= _CONTENT_GAP:
            break
        gap = 0
        seen_text = True
        end = i + 1
    return min(max(end, start), screen.cols - 1)


def field_right_limit(screen, prompt_y):
    """Max caret column inside the input field (not on right box border).

    Unlike content_end_col (end of typed text), this is the right edge of the
    editable area so mid-line cursors are not forced to EOL text.
    """
    row = screen.grid[prompt_y]
    start = input_start_col(screen, prompt_y)
    n = min(len(row), screen.cols)
    for i in range(start, n):
        if row[i] in _BOX_V:
            return max(start, i - 1)
    return max(start, n - 1)


def _clamp_input_col(screen, prompt_y, col):
    """Clamp to typed-text span (for footer-park restore only)."""
    start = input_start_col(screen, prompt_y)
    end = content_end_col(screen, prompt_y)
    return min(max(int(col), start), end)


def _clamp_live_col(screen, prompt_y, col):
    """Clamp hardware column on the live prompt row — trust PTY, not content_end.

    Clamping to content_end while the cursor is on the prompt fights mid-line
    edits and Grok's real x (caret jumps to EOL / lags a key behind).
    """
    start = input_start_col(screen, prompt_y)
    limit = field_right_limit(screen, prompt_y)
    return min(max(int(col), start), limit)


def _row_has_content(screen, y):
    """True if row y has any non-blank cell (ignoring pure box borders)."""
    row = screen.grid[y]
    n = min(len(row), screen.cols)
    for i in range(n):
        ch = row[i]
        if ch not in (" ", " "):
            return True
    return False


def input_field_last_row(screen, py):
    """Last row (>= py) that is still part of the (possibly multi-line) input.

    A single-line prompt's field is just `py`. When the composer wraps a long
    or literal multi-line prompt across several rows, those continuation rows
    have no `>` marker of their own, so find_prompt_row can't see them — the
    only signal available is that they are non-blank rows directly below py
    with no gap. The field ends at the first blank row after py (the visual
    separator Claude Code and friends draw before the status footer); a blank
    row immediately at py + 1 means the field really is just the one row.
    """
    last = py
    y = py + 1
    while y < screen.rows and _row_has_content(screen, y):
        last = y
        y += 1
    return last


def _clamp_row_col(screen, y, py, col):
    """Clamp a column on row y of the input field.

    Row py has the `>` marker and right-side chrome to respect; continuation
    rows (y > py) are plain wrapped text with no marker/border semantics, so
    just keep the column inside the grid — trust the PTY's x on those rows.
    """
    if y == py:
        return _clamp_live_col(screen, py, col)
    return min(max(int(col), 0), screen.cols - 1)


def adjust_display_caret(screen, cy, cx):
    """Map PTY cursor to ST caret; pin to prompt when parked on status footer."""
    py = find_prompt_row(screen)
    if py is None:
        return cy, cx

    hist = 0 if screen.alt_screen else len(screen.history)
    field_end = input_field_last_row(screen, py)

    # Live somewhere inside the input field (single row, or any row of a
    # wrapped/multi-line prompt): trust the hardware cursor's row and column.
    if py <= screen.y <= field_end:
        col = _clamp_row_col(screen, screen.y, py, int(screen.x))
        screen.input_caret_x = col
        screen.input_caret_row = screen.y
        return hist + screen.y, col

    # Below the input field (status footer): Claude often parks here while
    # the edit buffer is still on the prompt. Pin display caret to the last
    # known in-field position so left/right/up/down are not dead. (Terminus
    # does not do this; we keep a minimal pin only when hardware is clearly
    # off the input field.)
    if screen.y > field_end:
        row = getattr(screen, "input_caret_row", None)
        col = getattr(screen, "input_caret_x", None)
        if row is None or col is None:
            row = py
            col = content_end_col(screen, py)
        else:
            col = _clamp_row_col(screen, row, py, col)
        return hist + row, col

    return cy, cx


def pad_row_for_caret(rows, cy, cx):
    """Ensure row cy is long enough that index cx (the caret column) exists.

    Must reach length cx + 1, not cx: cursor_text_offset() requires
    `cx < len(row)` to resolve a text offset at all. Previously padded only
    to length cx (one short), so whenever the app parks the hardware cursor
    with DECTCEM off (cursor_visible False -- see _do_render, e.g. Claude
    Code's Ink TUI, which draws its own highlight instead of a real
    terminal cursor) paint_host_cursor's own `<= cx` padding never ran to
    cover the gap. cursor_text_offset then saw cx >= len(row), returned
    None, and the render loop fell back to the coarser (cursor row, col)
    path -- whose line_end clamp landed one column short of the real PTY
    cursor on that now-one-cell-short row. Visible as the ST caret sitting
    one character behind the TUI's own cursor.
    """
    if cy < 0 or cy >= len(rows):
        return rows
    row = list(rows[cy])
    while len(row) <= cx:
        row.append((" ", 0))
    rows = list(rows)
    rows[cy] = row
    return rows
