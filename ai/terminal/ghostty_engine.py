"""libghostty-vt-backed VT parser -- the sole VT engine for ai_terminal.py.

Contract: __init__(screen, force_main_screen), feed(text), resize(cols, rows),
reset() -- Screen/render.py/caret.py/mouse.py only depend on this contract
and Screen's grid/attrs/x/y/history/private_modes/cursor_visible surface, not
on this module's internals. See ghostty_vt.py for the ctypes binding layer
and DLL location.
"""
import ctypes
import re

from . import ghostty_vt as gvt
from .colors import pack_attr, quantize256, rstrip_cells, BOLD, REVERSE, FAINT, ITALIC, UNDERLINE, XTERM256_RGB
from .screen import BLANK


# libghostty-vt implements real primary/alternate screen buffer swapping.
# That means "force_main_screen" -- the user setting that keeps showing the
# scrollback-style view instead of a fullscreen TUI's alt-screen redraw --
# can no longer be emulated by ignoring the *mode flag*; the alternate
# screen would genuinely hold different cell contents once entered. Instead
# strip the alt-screen enter/exit sequences from the byte stream before
# they ever reach the terminal, so it never leaves the primary screen.
# CSI ? 1049/1047/47 h or l (with or without other combined private
# params -- TUIs emit these standalone in practice, so this doesn't try to
# handle a mixed-params form like `?1000;1049h`).
_ALT_SCREEN_RE = re.compile(r"\x1b\[\?(1049|1047|47)[hl]")


def _strip_alt_screen(text):
    if "\x1b[?" not in text:
        return text
    return _ALT_SCREEN_RE.sub("", text)


def _color_id(result, rgb):
    if result != gvt.SUCCESS:
        return 0
    return quantize256(rgb.r, rgb.g, rgb.b) + 1


_CURSOR_SHAPE_NAMES = {
    gvt.RENDER_STATE_CURSOR_VISUAL_STYLE_BAR: "bar",
    gvt.RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK: "block",
    gvt.RENDER_STATE_CURSOR_VISUAL_STYLE_UNDERLINE: "underline",
    gvt.RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK_HOLLOW: "hollow",
}


class GhosttyParser:
    """__init__(screen, force_main_screen), feed(text), resize(cols, rows), reset()."""

    def __init__(self, screen, force_main_screen=True, dll_path=None):
        self.s = screen
        self.force_main_screen = force_main_screen
        self._g = gvt.Ghostty(gvt.load_library(dll_path))

        cap = screen.history.maxlen or 300
        self._term = gvt.GhosttyTerminal()
        opts = gvt.GhosttyTerminalOptions(
            cols=screen.cols, rows=screen.rows, max_scrollback=cap
        )
        rc = self._g.terminal_new(None, ctypes.byref(self._term), opts)
        if rc != gvt.SUCCESS:
            raise RuntimeError("ghostty_terminal_new failed: %d" % rc)

        # Ghostty's built-in default 0-15 palette doesn't match colors.py's
        # vivid Terminus-style ANSI16 table. Since resolved cell colors come
        # back as RGB (not the original 0-15 index) and get requantized via
        # quantize256() against colors.py's XTERM256_RGB, a mismatched
        # palette makes every named ANSI color round-trip to the wrong id
        # (e.g. SGR 32 "green" resolving to some unrelated cube entry
        # instead of id 3). Overriding the terminal's default palette with
        # our own table makes the round-trip exact.
        palette = (gvt.GhosttyColorRgb * 256)(
            *[gvt.GhosttyColorRgb(r, g, b) for (r, g, b) in XTERM256_RGB]
        )
        self._g.terminal_set(self._term, gvt.TERMINAL_OPT_COLOR_PALETTE, palette)

        self._render_state = gvt.GhosttyRenderState()
        rc = self._g.render_state_new(None, ctypes.byref(self._render_state))
        if rc != gvt.SUCCESS:
            raise RuntimeError("ghostty_render_state_new failed: %d" % rc)

        self._row_iter = gvt.GhosttyRenderStateRowIterator()
        self._g.render_state_row_iterator_new(None, ctypes.byref(self._row_iter))
        self._cells = gvt.GhosttyRenderStateRowCells()
        self._g.render_state_row_cells_new(None, ctypes.byref(self._cells))

        self._utf8_buf = (ctypes.c_uint8 * 64)()
        self._last_scrollback_rows = -1

    def feed(self, text):
        if self.force_main_screen:
            text = _strip_alt_screen(text)
        data = text.encode("utf-8", "surrogateescape")
        self._g.terminal_vt_write(self._term, data, len(data))
        self._sync()

    def resize(self, cols, rows):
        self._g.terminal_resize(self._term, cols, rows, 1, 1)
        self.s.resize(cols, rows)
        # A resize can reflow scrollback content without changing its row
        # count, which the row-count-delta check in _sync_scrollback can't
        # detect -- force a full rebuild on the next sync.
        self._last_scrollback_rows = -1

    def reset(self):
        self._g.terminal_reset(self._term)
        self._last_scrollback_rows = -1
        self._sync()

    def encode_key(self, key, ctrl=False, alt=False, shift=False):
        """Encode a key event through libghostty-vt's key encoder.

        Syncs the encoder from the live terminal state on every call so that
        app-cursor mode, Kitty keyboard protocol flags, modifyOtherKeys, and
        alt-escape prefix are always current.

        Args:
            key: Sublime Text key name (lowercase), e.g. "a", "enter", "up".
            ctrl / alt / shift: modifier state booleans.

        Returns:
            bytes  — the encoded escape sequence (may be empty bytes if the key
                     generates no output, e.g. an unmodified modifier key press).
            None   — the key is not recognised (caller should fall back to
                     _translate_key / _encode_win32_key).
        """
        # Lazy encoder + event creation (per-GhosttyParser instance).
        if not hasattr(self, "_key_encoder"):
            enc = gvt.GhosttyKeyEncoder()
            rc = self._g.key_encoder_new(None, ctypes.byref(enc))
            if rc != gvt.SUCCESS:
                self._key_encoder = None
                self._key_event = None
            else:
                evt = gvt.GhosttyKeyEvent()
                rc2 = self._g.key_event_new(None, ctypes.byref(evt))
                if rc2 != gvt.SUCCESS:
                    self._g.key_encoder_free(enc)
                    self._key_encoder = None
                    self._key_event = None
                else:
                    self._key_encoder = enc
                    self._key_event = evt

        enc = self._key_encoder
        evt = self._key_event
        if enc is None or evt is None:
            return None

        # Resolve the key to a GhosttyKey int.  Single printable chars that
        # aren't in the named map are passed with key=0 (UNIDENTIFIED) and
        # the character itself via set_utf8 — the encoder uses the text field
        # for printable keys, and the logical key for everything else.
        kl = key.lower()
        gkey = gvt.ST_KEY_TO_GHOSTTY.get(kl)

        if gkey is None:
            if len(key) == 1:
                gkey = 0  # GHOSTTY_KEY_UNIDENTIFIED; text carries it
            else:
                return None  # Unknown named key → let legacy path handle it

        # Sync encoder options from the live terminal state.  This picks up:
        #   • cursor-key application mode (DEC 1)
        #   • keypad application mode (DEC 66)
        #   • Kitty keyboard protocol flags
        #   • modifyOtherKeys mode 2 (DEC 1036 / xterm)
        #   • alt-escape prefix (DEC 1036)
        self._g.key_encoder_setopt_from_terminal(enc, self._term)

        # Populate the event.
        self._g.key_event_set_key(evt, gkey)
        self._g.key_event_set_mods(evt, gvt.st_mods_to_ghostty(ctrl, alt, shift))
        self._g.key_event_set_action(evt, gvt.KEY_ACTION_PRESS)

        if len(key) == 1:
            # Pass the raw unshifted character so the encoder can derive the
            # correct Ctrl/Alt sequences from the logical key + mods pair.
            # event.h: "Must contain the unmodified character before any
            # Ctrl/Meta transformations. Do not pass C0 control characters."
            # This applies even for letters/digits/punctuation that also
            # have a logical GhosttyKey entry (gkey set above) -- the encoder
            # needs the utf8 text to produce output for a plain unmodified
            # press; a logical key with no text yields empty bytes for any
            # key that isn't itself a special escape sequence.
            ch = key if not shift else key.lower()
            self._g.key_event_set_utf8(evt, ch.encode("utf-8"), len(ch.encode("utf-8")))
            self._g.key_event_set_unshifted_codepoint(evt, ord(ch.lower()))
        else:
            # Named key: no utf8 text, encoder works from the logical key.
            self._g.key_event_set_utf8(evt, None, 0)
            self._g.key_event_set_unshifted_codepoint(evt, 0)

        # Encode into a fixed 128-byte buffer (sufficient for all standard
        # sequences; encoder returns OUT_OF_SPACE if not, in which case we
        # fall back to the legacy path rather than allocating dynamically).
        buf = ctypes.create_string_buffer(128)
        written = ctypes.c_size_t(0)
        rc = self._g.key_encoder_encode(enc, evt, buf, len(buf), ctypes.byref(written))
        if rc == gvt.SUCCESS:
            return bytes(buf.raw[: written.value])
        # OUT_OF_SPACE (oversized sequence) or other error: signal fallback.
        return None


    def _get_u16(self, data_id):
        v = ctypes.c_uint16()
        self._g.terminal_get(self._term, data_id, ctypes.byref(v))
        return v.value

    def _get_size(self, data_id):
        v = ctypes.c_size_t()
        self._g.terminal_get(self._term, data_id, ctypes.byref(v))
        return v.value

    def _get_bool(self, data_id):
        v = ctypes.c_bool()
        self._g.terminal_get(self._term, data_id, ctypes.byref(v))
        return v.value

    def _mode(self, mode_value):
        v = ctypes.c_bool()
        rc = self._g.terminal_mode_get(self._term, mode_value, ctypes.byref(v))
        return bool(rc == gvt.SUCCESS and v.value)

    def _style_flags(self, style):
        flags = 0
        if style.bold:
            flags |= BOLD
        if style.inverse:
            flags |= REVERSE
        if style.faint:
            flags |= FAINT
        if style.italic:
            flags |= ITALIC
        if style.underline:
            # underline is a style enum (none/single/double/curly/...), not a
            # bool; any non-zero value means "draw some underline".
            flags |= UNDERLINE
        return flags

    def _finish_cell(self, text, fg, bg, flags):
        if not text or text == " ":
            # A space's foreground is never visible; dropping it here keeps
            # trailing-blank trim working (Screen.render_cells()'s rstrip
            # only trims exact (" ", 0) cells).
            if not bg and not (flags & REVERSE):
                fg = 0
        return (text or " "), pack_attr(fg, bg, flags)

    def _cell_from_render_cells(self):
        """Active-grid path: render_state_row_cells_* on self._cells (fast, resolves colors)."""
        cells = self._cells
        get = lambda data_id, out: self._g.render_state_row_cells_get(cells, data_id, out)

        style = gvt.GhosttyStyle().init()
        get(gvt.RENDER_STATE_ROW_CELLS_DATA_STYLE, ctypes.byref(style))

        fg_rgb = gvt.GhosttyColorRgb()
        fg_rc = get(gvt.RENDER_STATE_ROW_CELLS_DATA_FG_COLOR, ctypes.byref(fg_rgb))
        bg_rgb = gvt.GhosttyColorRgb()
        bg_rc = get(gvt.RENDER_STATE_ROW_CELLS_DATA_BG_COLOR, ctypes.byref(bg_rgb))

        buf = gvt.GhosttyBuffer()
        buf.ptr = ctypes.cast(self._utf8_buf, ctypes.POINTER(ctypes.c_uint8))
        buf.cap = len(self._utf8_buf)
        rc = get(gvt.RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8, ctypes.byref(buf))
        text = bytes(self._utf8_buf[: buf.len]).decode("utf-8", "replace") if rc == gvt.SUCCESS and buf.len else ""

        flags = self._style_flags(style)
        return self._finish_cell(text, _color_id(fg_rc, fg_rgb), _color_id(bg_rc, bg_rgb), flags)

    def _resolve_style_color(self, color, palette):
        if color.tag == gvt.STYLE_COLOR_RGB:
            rgb = color.value.rgb
            return quantize256(rgb.r, rgb.g, rgb.b) + 1
        if color.tag == gvt.STYLE_COLOR_PALETTE:
            rgb = palette[color.value.palette]
            return quantize256(rgb.r, rgb.g, rgb.b) + 1
        return 0

    def _cell_from_grid_ref(self, ref, palette):
        """Scrollback path: grid_ref_style/graphemes (codepoints, unresolved colors)."""
        style = gvt.GhosttyStyle().init()
        rc = self._g.grid_ref_style(ctypes.byref(ref), ctypes.byref(style))
        if rc != gvt.SUCCESS:
            return self._finish_cell("", 0, 0, 0)

        cps = (ctypes.c_uint32 * 8)()
        n = ctypes.c_size_t()
        grc = self._g.grid_ref_graphemes(ctypes.byref(ref), cps, len(cps), ctypes.byref(n))
        text = "".join(chr(cps[i]) for i in range(n.value)) if grc == gvt.SUCCESS else ""

        flags = self._style_flags(style)
        fg = self._resolve_style_color(style.fg_color, palette)
        bg = self._resolve_style_color(style.bg_color, palette)
        return self._finish_cell(text, fg, bg, flags)

    def _sync_grid(self):
        rc = self._g.render_state_update(self._render_state, self._term)
        if rc != gvt.SUCCESS:
            return

        cols = self._get_u16(gvt.TERMINAL_DATA_COLS)
        rows = self._get_u16(gvt.TERMINAL_DATA_ROWS)
        s = self.s
        if cols != s.cols or rows != s.rows:
            # Terminal was resized out from under us (shouldn't happen --
            # resize() is the only path that changes cols/rows -- but don't
            # write out of bounds if it does).
            return

        # Dirty tracking: render_state_update() above folds the terminal's
        # own change-tracking into this render state, but per render.h it's
        # sticky -- the caller (us) must clear it after consuming, or every
        # row looks dirty forever. GHOSTTY_RENDER_STATE_DIRTY_FALSE means no
        # row changed since our last sync, so the walk (and its per-cell FFI
        # calls) can be skipped entirely -- this is the common case for a
        # single keystroke or cursor move.
        frame_dirty = ctypes.c_int()
        self._g.render_state_get(
            self._render_state, gvt.RENDER_STATE_DATA_DIRTY, ctypes.byref(frame_dirty)
        )
        if frame_dirty.value != gvt.RENDER_STATE_DIRTY_FALSE:
            self._g.render_state_get(
                self._render_state, gvt.RENDER_STATE_DATA_ROW_ITERATOR, ctypes.byref(self._row_iter)
            )
            row_dirty = ctypes.c_bool()
            y = 0
            while self._g.render_state_row_iterator_next(self._row_iter) and y < rows:
                self._g.render_state_row_get(
                    self._row_iter, gvt.RENDER_STATE_ROW_DATA_DIRTY, ctypes.byref(row_dirty)
                )
                if row_dirty.value:
                    self._g.render_state_row_get(
                        self._row_iter, gvt.RENDER_STATE_ROW_DATA_CELLS, ctypes.byref(self._cells)
                    )
                    grow = s.grid[y]
                    arow = s.attrs[y]
                    x = 0
                    while self._g.render_state_row_cells_next(self._cells) and x < cols:
                        text, attr = self._cell_from_render_cells()
                        grow[x] = text
                        arow[x] = attr
                        x += 1
                    while x < cols:
                        grow[x] = BLANK
                        arow[x] = 0
                        x += 1
                    clear_row = ctypes.c_bool(False)
                    self._g.render_state_row_set(
                        self._row_iter, gvt.RENDER_STATE_ROW_OPTION_DIRTY, ctypes.byref(clear_row)
                    )
                y += 1
            clear_frame = ctypes.c_int(gvt.RENDER_STATE_DIRTY_FALSE)
            self._g.render_state_set(
                self._render_state, gvt.RENDER_STATE_OPTION_DIRTY, ctypes.byref(clear_frame)
            )

        s.x = min(self._get_u16(gvt.TERMINAL_DATA_CURSOR_X), s.cols - 1)
        s.y = min(self._get_u16(gvt.TERMINAL_DATA_CURSOR_Y), s.rows - 1)
        # DECTCEM (ESC[?25l/h): fullscreen TUIs (Textual, ratatui, curses)
        # hide the real cursor and draw their own focus/highlight styling
        # instead. render.py's paint_host_cursor must not synthesize a
        # cursor block when this is False, or it chases the last-written
        # cell around the screen on every redraw.
        s.cursor_visible = self._get_bool(gvt.TERMINAL_DATA_CURSOR_VISIBLE)

        cursor_style = ctypes.c_int()
        rc = self._g.render_state_get(
            self._render_state, gvt.RENDER_STATE_DATA_CURSOR_VISUAL_STYLE, ctypes.byref(cursor_style)
        )
        s.cursor_shape = _CURSOR_SHAPE_NAMES.get(cursor_style.value, "block") if rc == gvt.SUCCESS else "block"

        active_screen = ctypes.c_int()
        self._g.terminal_get(self._term, gvt.TERMINAL_DATA_ACTIVE_SCREEN, ctypes.byref(active_screen))
        s.alt_screen = active_screen.value == gvt.SCREEN_ALTERNATE

        s.private_modes.discard(1000)
        s.private_modes.discard(1002)
        s.private_modes.discard(1003)
        s.private_modes.discard(1006)
        s.private_modes.discard(2004)
        for mode in (gvt.MODE_NORMAL_MOUSE, gvt.MODE_BUTTON_MOUSE, gvt.MODE_ANY_MOUSE,
                     gvt.MODE_SGR_MOUSE, gvt.MODE_BRACKETED_PASTE):
            if self._mode(mode):
                s.private_modes.add(mode)

    def _sync_scrollback(self):
        scrollback_rows = self._get_size(gvt.TERMINAL_DATA_SCROLLBACK_ROWS)
        last = self._last_scrollback_rows
        if scrollback_rows == last:
            return

        s = self.s
        cols = s.cols

        palette = (gvt.GhosttyColorRgb * 256)()
        self._g.terminal_get(self._term, gvt.TERMINAL_DATA_COLOR_PALETTE, palette)

        # Row 0 is the top of ghostty's scrollback (terminal.h), so as long
        # as the row count only grew since last sync, the new rows are a
        # contiguous run at the tail -- fetch just those and let history's
        # own maxlen evict the oldest, instead of re-walking (and re-issuing
        # 3 FFI calls per cell for) the entire capped scrollback on every
        # single line that scrolls. Any other transition (first sync, reset,
        # resize-triggered reflow/shrink) can't be trusted as a pure
        # append, so fall back to a full rebuild.
        #
        # A full rebuild replays rows already seen in a prior sync (reflowed
        # or not), so it must NOT re-fire on_retire_line for them -- only the
        # incremental-append path below notifies genuinely new lines. This
        # mirrors Screen.resize()'s own scrollback-clip path, which rebuilds
        # s.history by appending directly rather than through _retire_line.
        if 0 <= last < scrollback_rows:
            start = last
            notify = True
        else:
            s.history.clear()
            start = 0
            notify = False

        for y in range(start, scrollback_rows):
            cells = []
            for x in range(cols):
                pt = gvt.point(gvt.POINT_TAG_SCREEN, x, y)
                ref = gvt.GhosttyGridRef().init()
                rc = self._g.terminal_grid_ref(self._term, pt, ctypes.byref(ref))
                if rc != gvt.SUCCESS:
                    cells.append((" ", 0))
                    continue
                cells.append(self._cell_from_grid_ref(ref, palette))
            if notify:
                s._retire_line(cells)
            else:
                s.history.append(rstrip_cells(cells))

        self._last_scrollback_rows = scrollback_rows
        s.dirty = True

    def _sync(self):
        self._sync_grid()
        self._sync_scrollback()
        self._sync_title()
        self.s.dirty = True

    def _sync_title(self):
        # GhosttyString is a borrowed pointer valid only until the next
        # terminal_vt_write()/reset() -- must decode to a Python str here,
        # inside _sync(), not hold onto the struct for later.
        s = gvt.GhosttyString()
        rc = self._g.terminal_get(self._term, gvt.TERMINAL_DATA_TITLE, ctypes.byref(s))
        if rc != gvt.SUCCESS:
            return
        # len=0 means "no title set" per the API -- it does not distinguish
        # "never set" from "explicitly cleared", so both map to None here.
        self._title = ctypes.string_at(s.ptr, s.len).decode("utf-8", "replace") if s.len else None

    def get_title(self):
        """Current OSC 0/2 window title, or None if the app never set one."""
        return getattr(self, "_title", None)
