"""Cursor-aware terminal screen grid + scrollback.

Pure Python — no Sublime imports. Safe to unit-test outside ST.
"""
import collections

from .colors import rstrip_cells

BLANK = " "


class Screen:
    def __init__(self, cols, rows, history_cap=300):
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.x = 0
        self.y = 0
        self.grid = [[BLANK] * self.cols for _ in range(self.rows)]
        # Per-cell packed colour attr, parallel to grid. 0 = default (no region).
        self.attrs = [[0] * self.cols for _ in range(self.rows)]
        # Scrollback: rows that scroll off the top are captured here.
        # Cap is a user setting (scrollback_history_size); does NOT auto-size
        # on window resize. Stored as rstripped [(ch, attr), ...] cell-lists.
        # Unbounded deque, not deque(maxlen=cap): a maxlen deque evicts on
        # every append unconditionally, which is exactly wrong while
        # trim_paused (see there) -- eviction is enforced explicitly by
        # _enforce_history_cap() instead, only when not paused.
        self.history = collections.deque()
        self.history_cap = max(0, int(history_cap))
        # True while a viewer is scrolled back into history (set by the
        # owner, e.g. ai_terminal.py mirroring its own "user is reading
        # scrollback, not following" state -- see set_trim_paused). While
        # True, _retire_line still appends and still fires on_retire_line
        # (a session log must not lose lines just because someone is
        # reading), it just does not evict the oldest line to make room --
        # the read position stays valid instead of the buffer shifting out
        # from under it. Deferred trims are caught up the moment this flips
        # back to False.
        self.trim_paused = False
        # Monotonic count of every line ever retired, including ones the
        # maxlen deque above has since silently evicted. A renderer that
        # replaces the whole view text each frame (ai_terminal.py's non-
        # patched render path) needs this: once history is at cap, each new
        # retirement drops the oldest line, so the *same* fixed viewport
        # pixel offset now points at different text (the buffer moved, not
        # the viewport) -- comparing this counter's growth against
        # len(history)'s growth between two renders gives the exact number
        # of lines evicted from the top, which is how many line-heights the
        # viewport must be shifted to keep showing the same content.
        self.retired_total = 0
        # Optional callback(text: str), fired exactly once per line the
        # instant it permanently retires from the live viewport into
        # history -- i.e. it will never be redrawn or changed again. Callers
        # that push new lines into history MUST go through _retire_line()
        # (never self.history.append() directly), or this notification is
        # silently skipped. Rebuild paths that re-add already-seen entries
        # (resize reflow, set_history_cap) intentionally bypass this -- they
        # are not new content and must not be re-notified.
        self.on_retire_line = None
        # Exception raised by on_retire_line, if it ever raised: the callback is
        # dropped at that point (see _retire_line) so the owner can report the
        # broken logging instead of the failure vanishing line after line.
        self.retire_line_error = None
        self.saved = (0, 0)
        self.alt_screen = False
        # DEC mode 2026 (synchronized output): true while the app is mid
        # batch-update. Native-backed (GhosttyParser queries
        # ghostty_terminal_mode_get after every feed) -- ai_terminal.py's
        # render path defers painting while this is true so a batch isn't
        # shown half-drawn.
        self.sync_output = False
        # DECTCEM (mode 25): whether the app wants its real terminal cursor
        # shown. Defaults True (real terminals start with the cursor visible).
        # Fullscreen TUIs (Textual, ratatui, curses) hide it via ESC[?25l and
        # draw their own focus/highlight styling instead -- paint_host_cursor
        # must not synthesize a cursor block when this is False, or it chases
        # pyte's raw last-write position around the screen on every redraw.
        self.cursor_visible = True
        # DECSCUSR shape: "block" (default), "bar", "underline", or "hollow".
        # Only affects the host-synthesized blank-cell cursor glyph
        # (paint_host_cursor) -- a real character under the cursor is always
        # shown via colour-reversal, which has no shape to swap (see
        # render.py).
        self.cursor_shape = "block"
        self.dirty = True
        # Last hardware cursor column/row while inside the (possibly
        # multi-line) input field. Used by adjust_display_caret when Claude
        # parks the cursor on the status bar so ST restores the exact input
        # position (not end-of-text - 1, and not the first prompt row when
        # the field spans several rows).
        self.input_caret_x = None
        self.input_caret_row = None
        # DEC private modes the app enabled (e.g. 1000/1002/1003 mouse,
        # 1006 SGR coords, 2004 bracketed paste, 1049 alt screen). Parser
        # mutates this set; mouse routing reads it.
        self.private_modes = set()

    def resize(self, cols, rows):
        cols, rows = max(1, cols), max(1, rows)
        new = [[BLANK] * cols for _ in range(rows)]
        new_attrs = [[0] * cols for _ in range(rows)]
        for r in range(min(rows, self.rows)):
            srow = self.grid[r]
            arow = self.attrs[r]
            for c in range(min(cols, self.cols)):
                new[r][c] = srow[c]
                new_attrs[r][c] = arow[c]
        self.grid = new
        self.attrs = new_attrs
        # Clip scrollback rows to the new width when the screen shrinks.
        if cols < self.cols:
            new_hist = collections.deque()
            for row_cells in self.history:
                new_hist.append(rstrip_cells(row_cells[:cols]))
            self.history = new_hist
        self.cols, self.rows = cols, rows
        self.x = min(self.x, cols - 1)
        self.y = min(self.y, rows - 1)
        self.dirty = True

    def reset(self):
        self.grid = [[BLANK] * self.cols for _ in range(self.rows)]
        self.attrs = [[0] * self.cols for _ in range(self.rows)]
        self.history.clear()
        self.x = self.y = 0
        self.private_modes.clear()
        self.input_caret_x = None
        self.input_caret_row = None
        self.cursor_visible = True
        self.cursor_shape = "block"
        self.dirty = True

    def set_private_mode(self, mode, enable):
        """Enable or disable a DEC private mode number (e.g. 1000, 1006)."""
        mode = int(mode)
        if enable:
            self.private_modes.add(mode)
        else:
            self.private_modes.discard(mode)
        self.dirty = True

    @property
    def mouse_tracking(self):
        """Highest active Xterm mouse tracking mode, or 0 if off.

        1000 = click, 1002 = click+drag, 1003 = any-event (motion).
        """
        modes = self.private_modes
        if 1003 in modes:
            return 1003
        if 1002 in modes:
            return 1002
        if 1000 in modes:
            return 1000
        return 0

    @property
    def mouse_sgr(self):
        """True when SGR extended mouse coordinates (1006) are enabled."""
        return 1006 in self.private_modes

    def set_history_cap(self, cap):
        """Change the scrollback cap, preserving contents.

        A live cap edit (settings reload) is a deliberate, explicit user
        action distinct from the ordinary per-line trim, so it applies
        immediately regardless of trim_paused -- unlike a normal retirement,
        this is not something happening *while* someone is mid-read, it is
        the user themselves changing the rule.
        """
        cap = max(0, int(cap))
        if cap == self.history_cap:
            return
        self.history_cap = cap
        self._enforce_history_cap()

    def set_trim_paused(self, paused):
        """Hold off evicting old scrollback while a viewer is reading it.

        See trim_paused on __init__ for why. Resuming (paused: True->False)
        immediately catches up any trims deferred while paused, rather than
        waiting for the next retirement.
        """
        paused = bool(paused)
        was_paused = self.trim_paused
        self.trim_paused = paused
        if was_paused and not paused:
            self._enforce_history_cap()

    def _enforce_history_cap(self):
        while len(self.history) > self.history_cap:
            self.history.popleft()

    def _retire_line(self, raw_cells):
        """Choke point for pushing a genuinely new line into scrollback.

        rstrips, appends to history, and fires on_retire_line with the
        plain text exactly once. Both VT engines' scroll paths (this
        module's own _scroll_up, and ghostty_engine's _sync_scrollback)
        must call this instead of self.history.append() directly.

        Does not evict the oldest line to enforce history_cap while
        trim_paused -- see trim_paused on __init__. on_retire_line still
        fires unconditionally for real lines: a session log must not lose
        lines just because someone is reading scrollback right now.

        A blank line is not appended to history at all (does not consume
        capacity, does not fire on_retire_line, does not count toward
        retired_total). Some CLIs (confirmed: Claude Code) "clear the
        screen" on resize not with a real erase but by homing the cursor
        and flooding 30-100+ blank newlines to scroll the old frame off --
        against a capped history, one such flood evicts most or all of a
        long real conversation to make room for lines that carry no
        content (confirmed live 2026-08-18: a single resize -> 300 real
        lines down to double digits). Blank lines were never something a
        user could read back anyway, so refusing to spend scrollback
        capacity on them costs nothing while making that flood harmless
        regardless of its size. Trade-off: an intentional blank paragraph
        break in real output also won't survive into old scrollback once
        it scrolls off-screen -- deliberately accepted, real content
        surviving matters more than exact blank-line spacing in history
        a user is not currently looking at.
        """
        line = rstrip_cells(raw_cells)
        if not any(ch.strip() for ch, _attr in line):
            return line
        self.history.append(line)
        self.retired_total += 1
        if not self.trim_paused:
            self._enforce_history_cap()
        if self.on_retire_line is not None:
            try:
                self.on_retire_line("".join(ch for ch, _attr in line))
            except Exception as e:
                # Logging must never break rendering, but a callback that
                # raises once raises on every line, so drop it and keep the
                # reason for the owner rather than swallowing it forever.
                self.on_retire_line = None
                self.retire_line_error = e
        return line

    def live_lines_text(self):
        """Plain-text snapshot of every row still in the live viewport (not
        yet retired into history). Used to flush the final, never-scrolled
        screen of a session to a log on close."""
        out = []
        for i in range(self.rows):
            srow = self.grid[i]
            arow = self.attrs[i]
            cells = rstrip_cells([(srow[c], arow[c]) for c in range(self.cols)])
            out.append("".join(ch for ch, _attr in cells))
        return out

    def _scroll_up(self):
        popped = [(self.grid[0][c], self.attrs[0][c]) for c in range(self.cols)]
        self._retire_line(popped)
        self.grid.pop(0)
        self.attrs.pop(0)
        self.grid.append([BLANK] * self.cols)
        self.attrs.append([0] * self.cols)

    def _scroll_down(self):
        self.grid.pop()
        self.attrs.pop()
        self.grid.insert(0, [BLANK] * self.cols)
        self.attrs.insert(0, [0] * self.cols)

    def put_char(self, ch, attr=0):
        if self.x >= self.cols:
            self.x = 0
            self._line_feed()
        self.grid[self.y][self.x] = ch
        self.attrs[self.y][self.x] = attr
        self.x += 1
        self.dirty = True

    def _line_feed(self):
        self.y += 1
        if self.y >= self.rows:
            self._scroll_up()
            self.y = self.rows - 1

    def lf(self):
        self._line_feed()
        self.dirty = True

    def cr(self):
        self.x = 0
        self.dirty = True

    def bs(self):
        if self.x > 0:
            self.x -= 1
        self.dirty = True

    def tab(self):
        self.x = min(((self.x // 8) + 1) * 8, self.cols - 1)
        self.dirty = True

    def move_abs(self, r, c):
        self.y = max(0, min(r, self.rows - 1))
        self.x = max(0, min(c, self.cols - 1))
        self.dirty = True

    def move_rel(self, dy, dx):
        self.y = max(0, min(self.y + dy, self.rows - 1))
        self.x = max(0, min(self.x + dx, self.cols - 1))
        self.dirty = True

    def erase_display(self, n):
        if n == 2 or n == 3:
            self.grid = [[BLANK] * self.cols for _ in range(self.rows)]
            self.attrs = [[0] * self.cols for _ in range(self.rows)]
            if n == 3:
                # CSI 3J = erase scrollback (and screen); 2J leaves scrollback.
                self.history.clear()
        elif n == 0:
            for c in range(self.x, self.cols):
                self.grid[self.y][c] = BLANK
                self.attrs[self.y][c] = 0
            for r in range(self.y + 1, self.rows):
                self.grid[r] = [BLANK] * self.cols
                self.attrs[r] = [0] * self.cols
        elif n == 1:
            for r in range(0, self.y):
                self.grid[r] = [BLANK] * self.cols
                self.attrs[r] = [0] * self.cols
            for c in range(0, self.x + 1):
                self.grid[self.y][c] = BLANK
                self.attrs[self.y][c] = 0
        self.dirty = True

    def erase_line(self, n):
        row = self.grid[self.y]
        arow = self.attrs[self.y]
        if n == 0:
            for c in range(self.x, self.cols):
                row[c] = BLANK
                arow[c] = 0
        elif n == 1:
            for c in range(0, self.x + 1):
                row[c] = BLANK
                arow[c] = 0
        elif n == 2:
            for c in range(self.cols):
                row[c] = BLANK
                arow[c] = 0
        self.dirty = True

    def save_cursor(self):
        self.saved = (self.x, self.y)
        self.dirty = True

    def restore_cursor(self):
        self.x, self.y = self.saved
        self.x = min(self.x, self.cols - 1)
        self.y = min(self.y, self.rows - 1)
        self.dirty = True

    def render_cells(self):
        """Return (rows, cy, cx) for rendering.

        rows is a list of [(ch, attr), ...] cell-lists. History is prepended
        only when not on the alt screen (fullscreen TUI mode).
        """
        grid_rows = []
        cy_in_grid = self.y
        cx = self.x
        for i in range(self.rows):
            srow = self.grid[i]
            arow = self.attrs[i]
            if i == self.y:
                x = max(self.x, 0)
                body = [(srow[c], arow[c]) for c in range(min(x, self.cols))]
                tail = [(srow[c], arow[c]) for c in range(x, self.cols)]
                grid_rows.append(body + rstrip_cells(tail))
            else:
                cells = [(srow[c], arow[c]) for c in range(self.cols)]
                grid_rows.append(rstrip_cells(cells))
        if self.alt_screen:
            return grid_rows, cy_in_grid, cx
        hist = list(self.history)
        return hist + grid_rows, len(hist) + cy_in_grid, cx
