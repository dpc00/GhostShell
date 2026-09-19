"""libghostty-vt-backed VT parser -- the sole VT engine for ai_terminal.py.

Contract: __init__(screen), feed(text), resize(cols, rows), reset() --
Screen/render.py/caret.py/mouse.py only depend on this contract and
Screen's grid/attrs/x/y/history/private_modes/cursor_visible surface, not
on this module's internals. See ghostty_vt.py for the ctypes binding layer
and DLL location.

The byte stream from the child process is written to libghostty-vt
unmodified -- no escape codes are stripped, substituted, or rewritten, and
no scrollback splicing/merging is performed. Real alternate-screen
buffer swapping and DECSET 2026 (synchronized output) state are both
queried from the native terminal, not tracked or altered here.
"""
import ctypes

from . import ghostty_vt as gvt
from .colors import pack_attr, quantize256, rstrip_cells, BOLD, REVERSE, FAINT, ITALIC, UNDERLINE, XTERM256_RGB
from .screen import BLANK


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

# libghostty-vt max_scrollback is a byte budget, not a line count
# (ghostty-org/ghostty#12769). scrollback_history_size is lines; convert
# so native never trims before Python's history_cap. 64 bytes/cell is
# well above the ~1.5 bytes/col seen empirically on sparse filler.
_SCROLLBACK_BYTES_PER_CELL = 64


def _scrollback_bytes(line_cap, cols):
    return max(1, int(line_cap)) * max(1, int(cols)) * _SCROLLBACK_BYTES_PER_CELL


def _blank_wide_spacers(cells, width_of):
    """Drop Ghostty's extra column after a width-2 grapheme.

    Native grid: 🐍 occupies two cells; the second is a spacer space.
    Concatenating that space into the Sublime line adds a third em when
    the font already paints 🐍 at ~2em, so a full-width TUI box overflows,
    the H-scrollbar steals viewport height, and rows flip 47↔48.
    """
    out = list(cells)
    i = 0
    while i < len(out):
        text, _attr = out[i]
        try:
            wide = width_of(text) >= 2
        except (TypeError, ValueError):
            wide = False
        if wide and i + 1 < len(out) and out[i + 1][0] in (" ", ""):
            out[i + 1] = ("", 0)
            i += 2
            continue
        i += 1
    return out


class GhosttyParser:
    """__init__(screen), feed(text), resize(cols, rows), reset()."""

    def __init__(self, screen, dll_path=None):
        self.s = screen
        self._g = gvt.Ghostty(gvt.load_library(dll_path))

        cap = screen.history_cap or 300
        self._term = gvt.GhosttyTerminal()
        opts = gvt.GhosttyTerminalOptions(
            cols=screen.cols,
            rows=screen.rows,
            max_scrollback=_scrollback_bytes(cap, screen.cols),
        )
        gvt.check(
            self._g.terminal_new(None, ctypes.byref(self._term), opts),
            "ghostty_terminal_new",
        )

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
        # An unapplied palette silently requantizes every named ANSI color to
        # the wrong id, which is exactly what this override exists to prevent.
        gvt.check(
            self._g.terminal_set(self._term, gvt.TERMINAL_OPT_COLOR_PALETTE, palette),
            "ghostty_terminal_set(COLOR_PALETTE)",
        )

        # write_pty is the linchpin: without it, libghostty-vt parses DA/
        # kitty-flags/XTVERSION/size/enquiry queries internally but has
        # nowhere to send the formatted response, so the child blocks
        # forever on a startup capability probe (confirmed: Grok Build hung
        # 43+ minutes on a plain "hello" until winpty faked these answers).
        # Bound later via bind_write_pty() once the real pty exists -- see
        # that method's docstring. The trampoline reads this slot at call
        # time so it's a safe no-op before binding, not a crash.
        self._write_pty_sink = None
        self._write_pty_cb = gvt.GhosttyTerminalWritePtyFn(self._on_write_pty)
        gvt.check(
            self._g.terminal_set(
                self._term, gvt.TERMINAL_OPT_WRITE_PTY,
                ctypes.cast(self._write_pty_cb, ctypes.c_void_p),
            ),
            "ghostty_terminal_set(WRITE_PTY)",
        )

        # SIZE (XTWINOPS CSI 14/16/18 t) and ENQUIRY (ENQ 0x05) have no
        # built-in library default -- unlike DA/XTVERSION/kitty-flags, which
        # libghostty-vt answers sensibly on its own once WRITE_PTY exists,
        # these are silently ignored unless a callback is registered. Either
        # can be the specific query a TUI blocks its startup probe on.
        self._size_cb = gvt.GhosttyTerminalSizeFn(self._on_size_query)
        gvt.check(
            self._g.terminal_set(
                self._term, gvt.TERMINAL_OPT_SIZE,
                ctypes.cast(self._size_cb, ctypes.c_void_p),
            ),
            "ghostty_terminal_set(SIZE)",
        )

        # ENQUIRY (ENQ 0x05) has no callback here -- see ghostty_vt.py's
        # comment by the (absent) GhosttyTerminalEnquiryFn for why: ctypes
        # cannot build a callback whose C return type is a struct-by-value.
        # Left unregistered; the library silently ignores ENQ.

        # Color scheme (CSI ? 996 n) isn't a hang risk (well-behaved clients
        # tolerate silence), but it's cheap and unblocks apps that adapt
        # their palette to light/dark. DARK is the correct default: this
        # host has no live signal for ST's active color scheme's brightness.
        self._color_scheme_cb = gvt.GhosttyTerminalColorSchemeFn(self._on_color_scheme)
        gvt.check(
            self._g.terminal_set(
                self._term, gvt.TERMINAL_OPT_COLOR_SCHEME,
                ctypes.cast(self._color_scheme_cb, ctypes.c_void_p),
            ),
            "ghostty_terminal_set(COLOR_SCHEME)",
        )

        self._render_state = gvt.GhosttyRenderState()
        gvt.check(
            self._g.render_state_new(None, ctypes.byref(self._render_state)),
            "ghostty_render_state_new",
        )

        self._row_iter = gvt.GhosttyRenderStateRowIterator()
        gvt.check(
            self._g.render_state_row_iterator_new(None, ctypes.byref(self._row_iter)),
            "ghostty_render_state_row_iterator_new",
        )
        self._cells = gvt.GhosttyRenderStateRowCells()
        gvt.check(
            self._g.render_state_row_cells_new(None, ctypes.byref(self._cells)),
            "ghostty_render_state_row_cells_new",
        )

        self._utf8_buf = (ctypes.c_uint8 * 64)()
        self._last_scrollback_rows = -1

    def close(self):
        """Free every native resource this parser owns: the terminal, its
        render state (+ row iterator/cells), and the key/mouse encoder
        events if they were ever created (lazy -- see encode_key /
        encode_mouse). Idempotent, so a caller doesn't need to track
        whether it already called this.

        Freed in reverse acquisition order (encoders/events were created
        last, if at all; the terminal was created first). The caller must
        ensure nothing else can still be calling feed()/encode_key()/etc.
        on this instance before calling this -- freeing while another
        thread is mid-call is a native use-after-free, not a Python
        exception. See _Terminal.kill, which joins the PTY reader thread
        first for exactly this reason.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True
        mouse_event = getattr(self, "_mouse_event", None)
        if mouse_event is not None:
            self._g.mouse_event_free(mouse_event)
        mouse_encoder = getattr(self, "_mouse_encoder", None)
        if mouse_encoder is not None:
            self._g.mouse_encoder_free(mouse_encoder)
        key_event = getattr(self, "_key_event", None)
        if key_event is not None:
            self._g.key_event_free(key_event)
        key_encoder = getattr(self, "_key_encoder", None)
        if key_encoder is not None:
            self._g.key_encoder_free(key_encoder)
        self._g.render_state_row_cells_free(self._cells)
        self._g.render_state_row_iterator_free(self._row_iter)
        self._g.render_state_free(self._render_state)
        self._g.terminal_free(self._term)


    def feed(self, text):
        data = text.encode("utf-8", "surrogateescape")
        # ghostty_terminal_vt_write returns void (see its restype in
        # ghostty_vt.py) -- nothing to check here.
        self._g.terminal_vt_write(self._term, data, len(data))
        if self._mode(gvt.MODE_SYNC_OUTPUT):
            # Mode 2026 makes the native update atomic.  A resize repaint can
            # span dozens of PTY reads; syncing each intermediate read walks
            # the active grid through per-cell ctypes calls even though the
            # renderer must not display it.  Keep only the native terminal
            # current and materialize its final grid/history once, when the
            # closing chunk arrives.
            self.s.sync_output = True
        else:
            self._sync()

    def feed_bootstrap(self, text):
        """Advance only the native VT during broker replay.

        Do not materialize Python cells per chunk. finish_bootstrap
        publishes the native grid and scrollback once, at the replay
        boundary. Query responses still work because libghostty
        processes the bytes normally.
        """
        data = text.encode("utf-8", "surrogateescape")
        self._g.terminal_vt_write(self._term, data, len(data))

    def finish_bootstrap(self):
        """Publish native grid and scrollback after broker replay."""
        self._last_scrollback_rows = -1
        self._sync()
        self.s.sync_output = False

    def resize(self, cols, rows):
        # Screen is resized only once the terminal agreed: the two sizes must
        # stay in lockstep or _sync_grid quietly stops updating the grid.
        gvt.check(
            self._g.terminal_resize(self._term, cols, rows, 1, 1),
            "ghostty_terminal_resize",
        )
        self.s.resize(cols, rows)
        # Screen.resize only clips/pads active-grid cells and truncates
        # scrollback rows on narrow -- it does not reflow (there is no
        # per-row "was this a wrapped continuation" bookkeeping to reflow
        # from). The native terminal genuinely does reflow on resize; force
        # a full rebuild of Python's history from it on the next sync
        # rather than trusting Screen.resize's own shallow adjustment.
        # Deliberately reverted 2026-09-02: an earlier optimization here
        # (skip the rebuild, trust "Screen.resize already reflowed it")
        # was live-verified to leave old scrollback wrapped at whatever
        # width it was originally drawn at, permanently, once a resize's
        # synchronized replay closed -- confirmed by diffing the same
        # historical region across a narrow and a wide capture. The
        # resize<->replay oscillation this optimization was mistaken for
        # guarding against was already fixed independently, days earlier,
        # by pinning cols against the gutter-digit-width crossing
        # (gutter_digit_delta / accepted_cols, terminal/layout.py) -- this
        # rebuild does not reintroduce that bug.
        self._last_scrollback_rows = -1

    def reset(self):
        gvt.check(self._g.terminal_reset(self._term), "ghostty_terminal_reset")
        self._last_scrollback_rows = -1
        self._sync()

    def bind_write_pty(self, sink):
        """Bind the callable that receives libghostty-vt's formatted query
        responses (DA/kitty-flags/XTVERSION/size/enquiry) as raw bytes to
        write back to the real pty.

        Must be called before the child process starts (GhosttyParser
        itself is constructed in _make_parser; _spawn binds this from
        _Terminal.__init__, then prepare()'s the writer, then pty.start()).
        Pass None to unbind (e.g. on teardown); the callback is then a
        no-op rather than writing into a dead pty.
        """
        self._write_pty_sink = sink

    def _on_write_pty(self, term, userdata, data, length):
        sink = self._write_pty_sink
        if sink is None:
            return
        try:
            # Exceptions raised across a ctypes callback boundary don't
            # propagate -- they print to stderr (nowhere useful under ST)
            # and leave the native call in an undefined state. Swallowing
            # here after logging is deliberate, not an oversight.
            sink(bytes(data[:length]))
        except Exception as e:
            print("[ghostty_engine] write_pty sink failed: %s" % e)

    def _on_size_query(self, term, userdata, out_size):
        size = out_size.contents
        size.rows = self.s.rows
        size.columns = self.s.cols
        # No real font-metric concept inside a Sublime text view; 0 is the
        # spec-legal "unknown" answer, not a placeholder guess.
        size.cell_width = 0
        size.cell_height = 0
        return True

    def _on_color_scheme(self, term, userdata, out_scheme):
        out_scheme.contents.value = gvt.COLOR_SCHEME_DARK
        return True

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

    def encode_mouse(
        self,
        button,
        col,
        row,
        *,
        press=True,
        motion=False,
        shift=False,
        meta=False,
        ctrl=False,
    ):
        """Encode a mouse event through libghostty-vt's mouse encoder.

        Tracking mode and output format come from live DECSET on the native
        terminal (9/1000/1002/1003 and 1005/1006/1015/1016). Returns:

            str   — encoded report (empty if tracking is off or the event
                    is filtered, e.g. motion in 1000-mode)
            None  — encoder unavailable (caller may fall back to mouse.py)
        """
        from .mouse import BTN_RELEASE_X10, _PROTO_TO_GHOSTTY_BUTTON

        if not hasattr(self, "_mouse_encoder"):
            enc = gvt.GhosttyMouseEncoder()
            rc = self._g.mouse_encoder_new(None, ctypes.byref(enc))
            if rc != gvt.SUCCESS:
                self._mouse_encoder = None
                self._mouse_event = None
            else:
                evt = gvt.GhosttyMouseEvent()
                rc2 = self._g.mouse_event_new(None, ctypes.byref(evt))
                if rc2 != gvt.SUCCESS:
                    self._g.mouse_encoder_free(enc)
                    self._mouse_encoder = None
                    self._mouse_event = None
                else:
                    self._mouse_encoder = enc
                    self._mouse_event = evt

        enc = self._mouse_encoder
        evt = self._mouse_event
        if enc is None or evt is None:
            return None

        proto = int(button)
        no_button = motion and proto == BTN_RELEASE_X10
        gbtn = None if no_button else _PROTO_TO_GHOSTTY_BUTTON.get(proto)
        if gbtn is None and not no_button:
            return None

        if not press:
            action = gvt.MOUSE_ACTION_RELEASE
        elif motion:
            action = gvt.MOUSE_ACTION_MOTION
        else:
            action = gvt.MOUSE_ACTION_PRESS

        col = max(1, int(col))
        row = max(1, int(row))

        self._sync_mouse_encoder(enc)
        self._g.mouse_encoder_setopt_bool(
            enc,
            gvt.MOUSE_ENCODER_OPT_ANY_BUTTON_PRESSED,
            press or (motion and not no_button),
        )


        self._g.mouse_event_set_action(evt, action)
        if no_button:
            self._g.mouse_event_clear_button(evt)
        else:
            self._g.mouse_event_set_button(evt, gbtn)
        mods = 0
        if shift:
            mods |= gvt.MODS_SHIFT
        if ctrl:
            mods |= gvt.MODS_CTRL
        if meta:
            mods |= gvt.MODS_ALT
        self._g.mouse_event_set_mods(evt, mods)
        self._g.mouse_event_set_position(
            evt, gvt.GhosttyMousePosition(float(col - 1), float(row - 1))
        )

        buf = ctypes.create_string_buffer(128)
        written = ctypes.c_size_t(0)
        rc = self._g.mouse_encoder_encode(
            enc, evt, buf, len(buf), ctypes.byref(written)
        )
        if rc != gvt.SUCCESS:
            return None
        return buf.raw[: written.value].decode("latin-1")


    def _sync_mouse_encoder(self, enc):
        """Push current DEC mouse mode/format/size into the encoder.

        setopt_from_terminal always clears last-cell dedup, so we set
        event/format/size ourselves. Unchanged values keep last_cell.
        """
        if self._mode(gvt.MODE_ANY_MOUSE):
            event = gvt.MOUSE_TRACKING_ANY
        elif self._mode(gvt.MODE_BUTTON_MOUSE):
            event = gvt.MOUSE_TRACKING_BUTTON
        elif self._mode(gvt.MODE_NORMAL_MOUSE):
            event = gvt.MOUSE_TRACKING_NORMAL
        elif self._mode(gvt.MODE_X10_MOUSE):
            event = gvt.MOUSE_TRACKING_X10
        else:
            event = gvt.MOUSE_TRACKING_NONE

        if self._mode(gvt.MODE_SGR_PIXELS):
            fmt = gvt.MOUSE_FORMAT_SGR_PIXELS
        elif self._mode(gvt.MODE_URXVT_MOUSE):
            fmt = gvt.MOUSE_FORMAT_URXVT
        elif self._mode(gvt.MODE_SGR_MOUSE):
            fmt = gvt.MOUSE_FORMAT_SGR
        elif self._mode(gvt.MODE_UTF8_MOUSE):
            fmt = gvt.MOUSE_FORMAT_UTF8
        else:
            fmt = gvt.MOUSE_FORMAT_X10

        self._g.mouse_encoder_setopt_int(enc, gvt.MOUSE_ENCODER_OPT_EVENT, event)
        self._g.mouse_encoder_setopt_int(enc, gvt.MOUSE_ENCODER_OPT_FORMAT, fmt)
        self._g.mouse_encoder_setopt_bool(
            enc, gvt.MOUSE_ENCODER_OPT_TRACK_LAST_CELL, True
        )

        cols, rows = int(self.s.cols), int(self.s.rows)
        if getattr(self, "_mouse_size", None) != (cols, rows):
            size = gvt.mouse_encoder_size(cols, rows, 1, 1)
            self._g.mouse_encoder_setopt(
                enc, gvt.MOUSE_ENCODER_OPT_SIZE, ctypes.byref(size)
            )
            self._mouse_size = (cols, rows)


    def _get(self, data_id, out):
        """terminal_get, checked: a failed read otherwise reads as a real value.

        The out-param keeps its zero value when the call fails, which renders
        as a cursor at (0, 0), an empty scrollback or a hidden cursor —
        indistinguishable from the terminal genuinely being in that state.
        """
        gvt.check(
            self._g.terminal_get(self._term, data_id, ctypes.byref(out)),
            "ghostty_terminal_get(%d)" % data_id,
        )
        return out.value

    def _get_u16(self, data_id):
        return self._get(data_id, ctypes.c_uint16())

    def _get_size(self, data_id):
        return self._get(data_id, ctypes.c_size_t())

    def _get_bool(self, data_id):
        return self._get(data_id, ctypes.c_bool())

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

    def _grapheme_width(self, text):
        """Cells occupied by `text`, via ghostty_unicode_grapheme_width."""
        fn = getattr(self._g, "unicode_grapheme_width", None)
        if not text:
            return 0
        if fn is None:
            return 1
        cps = (ctypes.c_uint32 * len(text))(*map(ord, text))
        w = ctypes.c_uint8()
        fn(cps, len(text), ctypes.byref(w))
        return int(w.value)


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
        # Skipping the update freezes the visible screen while the child keeps
        # running, which reads as a hung agent rather than a failed call.
        gvt.check(
            self._g.render_state_update(self._render_state, self._term),
            "ghostty_render_state_update",
        )

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
        gvt.check(
            self._g.render_state_get(
                self._render_state, gvt.RENDER_STATE_DATA_DIRTY, ctypes.byref(frame_dirty)
            ),
            "ghostty_render_state_get(DIRTY)",
        )
        if frame_dirty.value != gvt.RENDER_STATE_DIRTY_FALSE:
            gvt.check(
                self._g.render_state_get(
                    self._render_state,
                    gvt.RENDER_STATE_DATA_ROW_ITERATOR,
                    ctypes.byref(self._row_iter),
                ),
                "ghostty_render_state_get(ROW_ITERATOR)",
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
                    pending = []
                    while self._g.render_state_row_cells_next(self._cells) and len(pending) < cols:
                        pending.append(self._cell_from_render_cells())
                    pending = _blank_wide_spacers(pending, self._grapheme_width)
                    for text, attr in pending:
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

        s.alt_screen = (
            self._get(gvt.TERMINAL_DATA_ACTIVE_SCREEN, ctypes.c_int())
            == gvt.SCREEN_ALTERNATE
        )
        s.sync_output = self._mode(gvt.MODE_SYNC_OUTPUT)

        for mode in (
            gvt.MODE_X10_MOUSE,
            gvt.MODE_NORMAL_MOUSE,
            gvt.MODE_BUTTON_MOUSE,
            gvt.MODE_ANY_MOUSE,
            gvt.MODE_UTF8_MOUSE,
            gvt.MODE_SGR_MOUSE,
            gvt.MODE_URXVT_MOUSE,
            gvt.MODE_SGR_PIXELS,
            gvt.MODE_BRACKETED_PASTE,
        ):
            s.private_modes.discard(mode)
            if self._mode(mode):
                s.private_modes.add(mode)


    def _sync_scrollback(self):
        scrollback_rows = self._get_size(gvt.TERMINAL_DATA_SCROLLBACK_ROWS)
        last = self._last_scrollback_rows

        # A resize (last == -1, see GhosttyParser.resize) falls through to
        # the full extraction below rather than trusting a shortcut here --
        # deliberately reverted 2026-09-02, see resize()'s comment for why.
        if scrollback_rows == last:
            return

        s = self.s
        cols = s.cols

        palette = (gvt.GhosttyColorRgb * 256)()
        # An unread palette resolves every scrollback cell's color to id 0.
        gvt.check(
            self._g.terminal_get(
                self._term, gvt.TERMINAL_DATA_COLOR_PALETTE, palette
            ),
            "ghostty_terminal_get(COLOR_PALETTE)",
        )

        # Row 0 is the top of ghostty's scrollback (terminal.h), so as long
        # as the row count only grew since last sync, the new rows are a
        # contiguous run at the tail -- fetch just those and let history's
        # own maxlen evict the oldest, instead of re-walking (and re-issuing
        # 3 FFI calls per cell for) the entire capped scrollback on every
        # single line that scrolls. Any other transition (first sync, reset,
        # resize-triggered reflow/shrink) can't be trusted as a pure
        # append, so fall back to a full rebuild.
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
            cells = _blank_wide_spacers(cells, self._grapheme_width)
            if notify:
                s._retire_line(cells)
            else:
                s.history.append(rstrip_cells(cells))


        # Native ghostty's max_scrollback is not a strict row count, so a
        # full rebuild (first sync after a broker replay, resize, reset) can
        # import more rows than the cap. The incremental path trims per line
        # via _retire_line; the rebuild appends directly, so trim here.
        if not notify and not s.trim_paused:
            s._enforce_history_cap()

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
