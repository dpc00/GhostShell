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
import time

from . import ghostty_vt as gvt
from .colors import pack_attr, quantize256, rstrip_cells, BOLD, REVERSE, FAINT, ITALIC, UNDERLINE, XTERM256_RGB
from .screen import BLANK


_COLOR_ID_CACHE = {}
_WIDTH_CACHE = {}


def _color_id(result, rgb):
    if result != gvt.SUCCESS:
        return 0
    # Every cell asks for its fg and bg id; a TUI uses a handful of distinct colours, so
    # this is a pure-function memo (693k calls, few keys, in a 2800-line profile).
    key = (rgb.r, rgb.g, rgb.b)
    cached = _COLOR_ID_CACHE.get(key)
    if cached is None:
        if len(_COLOR_ID_CACHE) > 4096:
            _COLOR_ID_CACHE.clear()
        cached = _COLOR_ID_CACHE[key] = quantize256(key[0], key[1], key[2]) + 1
    return cached


_CURSOR_SHAPE_NAMES = {
    gvt.RENDER_STATE_CURSOR_VISUAL_STYLE_BAR: "bar",
    gvt.RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK: "block",
    gvt.RENDER_STATE_CURSOR_VISUAL_STYLE_UNDERLINE: "underline",
    gvt.RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK_HOLLOW: "hollow",
}

# Scrollback depth is a line count in 1.1.0-dev (TERMINAL_OPT_SCROLLBACK_MAX_LINES), so
# the bytes-per-cell conversion that existed to emulate a line cap is gone with it.


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


_TEXT_VS = "\ufe0e"
_EMOJI_VS = "\ufe0f"


def _text_style_if_symbol(text):
    """Force text presentation on 1-cell Misc Symbols / Dingbats.

    Ghostty widths U+270E/U+2699 as 1. Windows color-emoji fallback paints
    them ~2em, so a full-width TUI box header sticks out past the box-drawing
    rows. U+FE0E requests text style; it is zero-width.
    """

    if not text or text.endswith((_TEXT_VS, _EMOJI_VS)):
        return text
    if len(text) != 1:
        return text
    o = ord(text)
    if 0x2600 <= o <= 0x27BF:
        return text + _TEXT_VS
    return text



class GhosttyParser:
    """__init__(screen), feed(text), resize(cols, rows), reset()."""

    # At most one grid materialisation per this many seconds (see feed()). Below a
    # frame at 40 fps, above the cost of a full repaint (~34 ms at 12 us/cell), so a
    # burst coalesces into roughly one sync per frame instead of one per chunk.
    _SYNC_MIN_INTERVAL = 0.025
    # Scrollback capture waits for this much quiet before it materialises rows, so a
    # burst pays one bounded rebuild instead of per-line FFI for rows it will evict.
    _HISTORY_SETTLE = 0.15

    def __init__(self, screen, dll_path=None):
        self.s = screen
        self._g = gvt.Ghostty(gvt.load_library(dll_path))

        cap = screen.history_cap or 300
        self._term = gvt.GhosttyTerminal()
        # 1.1.0-dev: terminal_new takes cols/rows directly (no options struct) and the
        # scrollback cap is set afterwards, in lines. Both limits exist and the first one
        # reached wins -- the byte limit defaults to only 10 000 bytes, which trimmed a
        # 300-line cap down to 12 rows (measured), so it is removed with NULL here and the
        # line count governs. Native then trims at exactly the configured depth, which is
        # what the old bytes-per-cell conversion was emulating.
        gvt.check(
            self._g.terminal_new(None, ctypes.byref(self._term), screen.cols, screen.rows),
            "ghostty_terminal_new",
        )
        # The byte limit defaults to only 10 000 bytes and wins when reached first, which
        # trimmed a 300-line cap down to 12 rows (measured). Passing NULL does not remove
        # that default -- terminal.h's "pass SIZE_MAX or NULL" only clears a limit you set
        # yourself; SIZE_MAX reports as NO_VALUE and really lifts it.
        unlimited_bytes = ctypes.c_size_t((1 << 64) - 1)
        gvt.check(
            self._g.terminal_set(
                self._term, gvt.TERMINAL_OPT_SCROLLBACK_MAX_BYTES,
                ctypes.byref(unlimited_bytes),
            ),
            "ghostty_terminal_set(SCROLLBACK_MAX_BYTES=SIZE_MAX)",
        )
        # The line limit trims in whole pages in 1.1.0-dev, and each trim overshoots by
        # about a page: asking for 300 kept 52 rows, asking for 1000 kept 818 (measured
        # 2026-11-07, both with the byte limit lifted). Native therefore gets a backlog
        # well above the line cap -- Screen.history still enforces exactly `cap` rows --
        # and nothing is delegated to a limit that cannot honour the number it was given.
        scrollback_lines = ctypes.c_size_t(max(cap * 4, 1200))
        gvt.check(
            self._g.terminal_set(
                self._term, gvt.TERMINAL_OPT_SCROLLBACK_MAX_LINES,
                ctypes.byref(scrollback_lines),
            ),
            "ghostty_terminal_set(SCROLLBACK_MAX_LINES)",
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
        # Frame-cadence sync gate, see feed(): at most one grid materialisation per
        # window; minute differences do not matter, so a monotonic clock is enough.
        self._last_sync_mono = 0.0
        self._last_feed_mono = 0.0
        self._sync_pending = False
        self._history_pending = False

        # Reused cell-read buffers. _cell_from_render_cells runs once per cell of every
        # dirty row, so allocating its GhosttyStyle/2x GhosttyColorRgb/GhosttyBuffer and
        # four byref() objects per cell cost more than the FFI calls they feed: 1.3M
        # byref allocations alone in a 2800-line profile (2026-11-07). They are outputs
        # (the getters overwrite them) and are read immediately, so one set per parser is
        # enough -- measured 12.3 s -> see terminal notes.
        self._c_style = gvt.GhosttyStyle().init()
        self._c_styled = ctypes.c_bool()
        self._c_mode_cfg = gvt.GhosttyTerminalModeConfig()
        self._c_fg = gvt.GhosttyColorRgb()
        self._c_bg = gvt.GhosttyColorRgb()
        self._c_buf = gvt.GhosttyBuffer()
        self._c_buf.ptr = ctypes.cast(self._utf8_buf, ctypes.POINTER(ctypes.c_uint8))
        self._c_buf.cap = len(self._utf8_buf)
        self._r_style = ctypes.byref(self._c_style)
        self._r_fg = ctypes.byref(self._c_fg)
        self._r_bg = ctypes.byref(self._c_bg)
        self._r_buf = ctypes.byref(self._c_buf)
        # One FFI call per cell instead of four (ghostty_render_state_row_cells_get_multi).
        # It stops at the first key without a value, and FG/BG_COLOR legitimately have
        # none on unstyled cells, so the pair asked together is the one that is always
        # present: the cell's text and whether it has any styling at all. An unstyled
        # cell -- most of a TUI screen -- then costs a single call and no style work.
        self._cell_keys = (ctypes.c_int * 2)(
            gvt.RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8,
            gvt.RENDER_STATE_ROW_CELLS_DATA_HAS_STYLING,
        )
        self._cell_vals = (ctypes.c_void_p * 2)(
            ctypes.addressof(self._c_buf),
            ctypes.addressof(self._c_styled),
        )
        self._cell_written = ctypes.c_size_t()

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
        self._last_feed_mono = time.monotonic()
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
            return
        # Materialising the grid costs ~12 us per cell -- 4800 cells for one full-screen
        # TUI repaint -- so a burst that repaints the whole screen per chunk spends
        # seconds of UI thread per second of output (measured 2026-11-07: 12.3 s for
        # 2800 lines before the per-cell work was cut, 3.1 s after). Painting is already
        # coalesced to _RENDER_MIN_INTERVAL_MS, so the grid only has to be current when
        # something looks at it: inside the window a chunk only advances the native
        # terminal (where the byte stream lives) and marks the screen stale for
        # flush_pending(), which the render path calls before it builds a frame. This is
        # what makes an intermediate replay frame cost nothing instead of everything.
        now = time.monotonic()
        if now - self._last_sync_mono < self._SYNC_MIN_INTERVAL:
            self._sync_pending = True
            return
        self._last_sync_mono = now
        self._sync()

    def flush_pending(self):
        """Materialise whatever the coalescing gates held back (grid and/or history).

        The render path calls this before building a frame. Grid work is done whenever it
        is pending; history capture waits for the stream to settle (see _HISTORY_SETTLE),
        because a burst's intermediate scrollback is evicted before anyone sees it.
        """
        if not self._sync_pending and not self._history_pending:
            return False
        now = time.monotonic()
        if (self._history_pending and not self._sync_pending
                and now - self._last_feed_mono < self._HISTORY_SETTLE):
            return False
        self._last_sync_mono = now
        self._sync()
        return True

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
        # ghostty_terminal_reset returns nothing (void, ghostty_vt.py:875),
        # so there is no result code to check. Passing its None to
        # gvt.check() raised on every call, so Nuke (ctrl+alt+k) never
        # cleared the engine and the old screen came back on the next paint.
        self._g.terminal_reset(self._term)
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
            # 1.1.0-dev takes the text as a GhosttyString BY VALUE (see the binding); the
            # buffer must outlive the call, so it is kept in this scope.
            _raw = ctypes.create_string_buffer(ch.encode("utf-8"), len(ch.encode("utf-8")))
            _text = gvt.GhosttyString(
                ctypes.cast(_raw, ctypes.POINTER(ctypes.c_uint8)), len(_raw.raw)
            )
            self._g.key_event_set_utf8(evt, _text)
            self._g.key_event_set_unshifted_codepoint(evt, ord(ch.lower()))
        else:
            # Named key: no utf8 text, encoder works from the logical key.
            self._g.key_event_set_utf8(evt, gvt.GhosttyString(None, 0))
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
        # 1.1.0-dev dropped ghostty_terminal_mode_get: a mode is read through
        # ghostty_terminal_get(GHOSTTY_TERMINAL_DATA_MODE) with a GhosttyTerminalModeConfig
        # carrying the mode to query. GhosttyMode packs the mode number in bits 0-14 and
        # "is ANSI" in bit 15, so the raw DEC private numbers this file passes are already
        # the right encoding (ghostty_mode_new(n, false) == n).
        cfg = self._c_mode_cfg
        cfg.mode = mode_value
        cfg.value = False
        rc = self._g.terminal_get(self._term, gvt.TERMINAL_DATA_MODE, ctypes.byref(cfg))
        return bool(rc == gvt.SUCCESS and cfg.value)

    def _style_flags(self, style):
        """GhosttyStyleFlags (style.h) -> GhostShell's cell attribute flags.

        1.1.0-dev packs the attributes into one 16-bit mask: bits 0-7 are the on/off
        flags, bits 8-10 the underline style (any non-zero means "draw some underline"),
        instead of the bool-per-attribute struct of 0.1.0-dev.
        """
        f = style.flags
        flags = 0
        if f & gvt.STYLE_FLAG_BOLD:
            flags |= BOLD
        if f & gvt.STYLE_FLAG_ITALIC:
            flags |= ITALIC
        if f & gvt.STYLE_FLAG_FAINT:
            flags |= FAINT
        if f & gvt.STYLE_FLAG_INVERSE:
            flags |= REVERSE
        if f & gvt.STYLE_UNDERLINE_MASK:
            flags |= UNDERLINE
        return flags

    def _finish_cell(self, text, fg, bg, flags):
        if not text or text == " ":
            # A space's foreground is never visible; dropping it here keeps
            # trailing-blank trim working (Screen.render_cells()'s rstrip
            # only trims exact (" ", 0) cells).
            if not bg and not (flags & REVERSE):
                fg = 0
            return " ", pack_attr(fg, bg, flags)
        return _text_style_if_symbol(text), pack_attr(fg, bg, flags)


    def _grapheme_width(self, text):
        """Cells occupied by `text`, via ghostty_unicode_grapheme_width.

        Called once per cell of every synced row (through _blank_wide_spacers), and the
        answer depends only on the text, so it is memoised: a screen reuses a few dozen
        distinct graphemes and the native call needs a ctypes array per question.
        """
        cached = _WIDTH_CACHE.get(text)
        if cached is not None:
            return cached
        fn = getattr(self._g, "unicode_grapheme_width", None)
        if not text:
            return 0
        if fn is None:
            return 1
        cps = (ctypes.c_uint32 * len(text))(*map(ord, text))
        w = ctypes.c_uint8()
        fn(cps, len(text), ctypes.byref(w))
        if len(_WIDTH_CACHE) > 8192:
            _WIDTH_CACHE.clear()
        _WIDTH_CACHE[text] = int(w.value)
        return int(w.value)


    def _cell_from_render_cells(self):
        """Active-grid path: one bulk cells call per cell, then plain struct reads.

        This runs once per cell of every dirty row (4800 for a full-screen TUI repaint),
        so it does as little per-cell work as possible: the four data kinds a cell needs
        come from a single ghostty_render_state_row_cells_get_multi call into buffers and
        pointers allocated once per parser, and the getter/lambda/allocation-per-cell
        shape this replaced measured 12.3 s for 2800 lines (2026-11-07).
        """
        rc = self._g.render_state_row_cells_get_multi(
            self._cells, 2, self._cell_keys, self._cell_vals, self._cell_written
        )
        buf = self._c_buf
        text = ""
        if rc == gvt.SUCCESS or self._cell_written.value > 0:
            if buf.len:
                text = bytes(self._utf8_buf[: buf.len]).decode("utf-8", "replace")
        if rc != gvt.SUCCESS or not self._c_styled.value:
            # No explicit styling: the resolved fg/bg are the renderer's defaults and no
            # flags are set, which is exactly what _finish_cell does with zeros.
            return self._finish_cell(text, 0, 0, 0)
        get = self._g.render_state_row_cells_get
        cells = self._cells
        get(cells, gvt.RENDER_STATE_ROW_CELLS_DATA_STYLE, self._r_style)
        fg_rc = get(cells, gvt.RENDER_STATE_ROW_CELLS_DATA_FG_COLOR, self._r_fg)
        bg_rc = get(cells, gvt.RENDER_STATE_ROW_CELLS_DATA_BG_COLOR, self._r_bg)
        return self._finish_cell(
            text, _color_id(fg_rc, self._c_fg), _color_id(bg_rc, self._c_bg),
            self._style_flags(self._c_style),
        )

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
            # A burst -- a TUI repainting its whole conversation -- scrolls rows faster
            # than anybody can read them, and Screen.history keeps only history_cap of
            # them, so materialising every retired line (per-cell FFI, ~8 us per cell)
            # buys a scrollback nobody sees: 336k cells / 2.7 s for 2800 lines, of which
            # 300 survived (measured 2026-11-07). While output is still arriving, leave
            # history alone and let it rebuild once the stream settles.
            if scrollback_rows - last > 16 and (time.monotonic() - self._last_feed_mono) < self._HISTORY_SETTLE:
                self._history_pending = True
                return
            # Rebuild from the newest rows only: anything above the cap would be evicted
            # by _retire_line immediately. Rows scrolled past between syncs are skipped,
            # which is what the cap meant for them anyway.
            start = max(last, scrollback_rows - max(0, s.history_cap))
            notify = start == last
        else:
            s.history.clear()
            # Native max_scrollback is a byte budget (scrollback_history_size x cols x
            # 64), not a row count, so after a reattach the replay's scrollback can hold
            # tens of thousands of short rows. Reading them all costs cols x ~3 FFI calls
            # per row on the UI thread and every row above history_cap is trimmed away
            # immediately below -- on 2026-11-07 that made the first paint after a plugin
            # reload take minutes. Only the tail that can survive the cap is readable, so
            # start there: the result is identical, the walk is bounded by the setting.
            start = max(0, scrollback_rows - max(0, s.history_cap))
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
        self._sync_pending = False
        self._history_pending = False
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
