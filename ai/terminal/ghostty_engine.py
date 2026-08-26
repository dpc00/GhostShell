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
# CSI ? 1049/1047/47 h or l -- including when combined with another
# private mode in the same sequence (e.g. `?1049;2004h` for alt-screen +
# bracketed paste together). An earlier version of this regex
# (`\x1b\[\?(1049|1047|47)[hl]`) only matched an isolated alt-screen
# sequence and silently let combined forms through unstripped (confirmed
# live 2026-08-25: `?1049;2004h` and `?1;47h` both leaked). _strip_alt_screen
# below removes just the alt-screen mode numbers from the parameter list
# and keeps the rest, so an unrelated mode toggled in the same sequence
# still reaches the parser.
_ALT_SCREEN_MODES = frozenset(("1049", "1047", "47"))
_PRIVATE_MODE_RE = re.compile(r"\x1b\[\?([0-9;]+)([hl])")
_ALT_SCREEN_PENDING_MAX = 256
# CUP home as Codex/Qwen emit it on a full-frame repaint. Do not treat
# CUP to an arbitrary row (`ESC[12H`) as home.
_CUP_HOME_RE = re.compile(r"\x1b\[(?:(?:0;0|1;1|1|0)?H)")
_SYNC_HL_RE = re.compile(r"\x1b\[\?2026([hl])")


def _history_row_text(row):
    return "".join(ch for ch, _attr in row).rstrip()


def _rows_match(old_row, new_row, allow_splice):
    a = _history_row_text(old_row)
    b = _history_row_text(new_row)
    if a == b:
        return True
    if allow_splice and a and b and (b.startswith(a) or a.startswith(b)):
        return True
    return False


def merge_replace_scroll_history(old_rows, new_rows, splice_window):
    """History after a home+2026 dump's native overflow is known.

    CSI H overwrites the visible screen, then overflow re-scrolls that
    dump's own text into native scrollback. A full-transcript replay
    therefore *reproduces* a suffix of prior Python history (often the
    whole thing), but wrap + in-place overwrite without EL means later
    overflow rows need not equal the old rows line-for-line. Align on
    the first overflow line instead: the leftmost old row that matches
    it (exact or leftover-tail splice) is the start of the reproduced
    suffix; rows before that are from before this replay and are kept.
    The first `splice_window` overflow rows can also carry leftover
    tails of the overwritten screen; prefer the clean prior copy of
    that same line when the new row is the old text plus a tail.

    old_rows: Python history before this rebuild.
    new_rows: native rows [origin, scrollback_rows) for this dump.
    splice_window: screen height (overwrite-and-rescroll window).
    """
    keep = len(old_rows)
    if new_rows:
        new0 = new_rows[0]
        if _history_row_text(new0):
            for j, row in enumerate(old_rows):
                if _rows_match(row, new0, allow_splice=True):
                    keep = j
                    break
    merged = list(old_rows[:keep])
    for i, row in enumerate(new_rows):
        oi = keep + i
        if i < splice_window and oi < len(old_rows):
            a = _history_row_text(old_rows[oi])
            b = _history_row_text(row)
            if a and b.startswith(a) and b != a:
                merged.append(old_rows[oi])
                continue
        merged.append(row)
    return merged


def update_replace_scroll(sync_open, replace, text):
    """Return (sync_open, replace) after scanning one PTY chunk.

    `replace` latches True when CUP home occurs while DECSET 2026 is
    open, and stays set until the caller clears it after 2026 closes.
    A split dump (home in chunk 1, overflow in chunk 2) must not append.
    """
    replace = bool(replace)
    events = [(m.start(), "sync", m.group(1)) for m in _SYNC_HL_RE.finditer(text)]
    events.extend((m.start(), "home", None) for m in _CUP_HOME_RE.finditer(text))
    events.sort()
    open_ = bool(sync_open)
    for _pos, kind, val in events:
        if kind == "sync":
            open_ = val == "h"
        elif kind == "home" and open_:
            replace = True
    return open_, replace


def _rewrite_private_mode(params, action):
    kept = [p for p in params.split(";") if p not in _ALT_SCREEN_MODES]
    if not kept:
        return ""
    return "\x1b[?" + ";".join(kept) + action


class _AltScreenFilter:
    """Incrementally remove DEC alternate-screen modes from a VT stream.

    PTY reads may split a CSI sequence at any byte. A regex applied to each
    read independently therefore lets a split ``ESC[?1049h`` reach Ghostty
    and defeats force_main_screen. This filter retains only a possible
    unfinished private-mode sequence; all ordinary text passes immediately.
    Malformed numeric parameter runs are bounded so they cannot grow memory
    indefinitely.
    """

    def __init__(self, pending_max=_ALT_SCREEN_PENDING_MAX):
        self.pending = ""
        self.pending_max = max(4, int(pending_max))

    def reset(self):
        self.pending = ""

    def flush(self):
        pending = self.pending
        self.pending = ""
        return pending

    def feed(self, text):
        if not self.pending and "\x1b" not in (text or ""):
            return text or ""
        data = self.pending + (text or "")
        self.pending = ""
        if not data:
            return ""

        out = []
        pos = 0
        size = len(data)
        while pos < size:
            esc = data.find("\x1b", pos)
            if esc < 0:
                out.append(data[pos:])
                break
            out.append(data[pos:esc])

            # Retain prefixes that may become ESC[?... on the next read.
            remaining = size - esc
            if remaining == 1 or (remaining == 2 and data[esc + 1] == "["):
                self.pending = data[esc:]
                break
            if data[esc + 1] != "[":
                out.append("\x1b")
                pos = esc + 1
                continue
            if remaining == 2:
                self.pending = data[esc:]
                break
            if data[esc + 2] != "?":
                # Some other complete/incomplete CSI. Ghostty owns it; emit
                # the ESC now and leave its own incremental parser to finish.
                out.append("\x1b")
                pos = esc + 1
                continue

            end = esc + 3
            while end < size and data[end] in "0123456789;":
                end += 1
            if end == size:
                candidate = data[esc:]
                if len(candidate) <= self.pending_max:
                    self.pending = candidate
                else:
                    out.append(candidate)
                break

            action = data[end]
            if end > esc + 3 and action in "hl":
                out.append(_rewrite_private_mode(data[esc + 3:end], action))
                pos = end + 1
                continue

            # Not a DEC private mode set/reset sequence we rewrite. Preserve
            # the bytes examined and continue after them without interpretation.
            out.append(data[esc:end + 1])
            pos = end + 1

        return "".join(out)


def _strip_alt_screen(text):
    """Stateless compatibility helper for complete strings and unit tests."""
    if "\x1b[?" not in text:
        return text
    stream_filter = _AltScreenFilter()
    return stream_filter.feed(text) + stream_filter.flush()


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

        cap = screen.history_cap or 300
        self._term = gvt.GhosttyTerminal()
        opts = gvt.GhosttyTerminalOptions(
            cols=screen.cols, rows=screen.rows, max_scrollback=cap
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
        self._alt_screen_filter = _AltScreenFilter()
        self._last_scrollback_rows = -1
        self._sync_open = False
        self._replace_scroll = False
        self._replace_origin = None

    def close(self):
        """Free every native resource this parser owns: the terminal, its
        render state (+ row iterator/cells), and the key encoder/event if
        one was ever created (lazy -- see encode_key). Idempotent, so a
        caller doesn't need to track whether it already called this.

        Freed in reverse acquisition order (key encoder/event were created
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
        self._alt_screen_filter.reset()
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
        if self.force_main_screen:
            text = self._alt_screen_filter.feed(text)
        self._sync_open, self._replace_scroll = update_replace_scroll(
            self._sync_open, self._replace_scroll, text
        )
        data = text.encode("utf-8", "surrogateescape")
        # ghostty_terminal_vt_write returns void (see its restype in
        # ghostty_vt.py) -- nothing to check here.
        self._g.terminal_vt_write(self._term, data, len(data))
        self._sync()
        # Next chunk starts clean unless 2026 is still open (split dump).
        if not self._sync_open:
            self._replace_scroll = False
            self._replace_origin = None

    def resize(self, cols, rows):
        # Screen is resized only once the terminal agreed: the two sizes must
        # stay in lockstep or _sync_grid quietly stops updating the grid.
        gvt.check(
            self._g.terminal_resize(self._term, cols, rows, 1, 1),
            "ghostty_terminal_resize",
        )
        self.s.resize(cols, rows)
        # A resize can reflow scrollback content without changing its row
        # count, which the row-count-delta check in _sync_scrollback can't
        # detect -- force a full rebuild on the next sync.
        self._last_scrollback_rows = -1

    def reset(self):
        self._alt_screen_filter.reset()
        gvt.check(self._g.terminal_reset(self._term), "ghostty_terminal_reset")
        self._last_scrollback_rows = -1
        self._sync_open = False
        self._replace_scroll = False
        self._replace_origin = None
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

        s.alt_screen = (
            self._get(gvt.TERMINAL_DATA_ACTIVE_SCREEN, ctypes.c_int())
            == gvt.SCREEN_ALTERNATE
        )
        s.sync_output = self._mode(gvt.MODE_SYNC_OUTPUT)

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
        #
        # A full rebuild replays rows already seen in a prior sync (reflowed
        # or not), so it must NOT re-fire on_retire_line for them -- only the
        # incremental-append path below notifies genuinely new lines. This
        # mirrors Screen.resize()'s own scrollback-clip path, which rebuilds
        # s.history by appending directly rather than through _retire_line.
        replace_old = None
        if self._replace_scroll and last >= 0:
            # Home+2026 dump, possibly split across feeds: pin the
            # origin to the first overflow of this batch and rebuild
            # from that native range. Do not blindly clear [0, origin)
            # -- a full-transcript replay re-creates those rows (often
            # with a spliced tail on the first screen), but content
            # from before this replay is not in the dump and must stay.
            if self._replace_origin is None:
                self._replace_origin = last
            origin = self._replace_origin
            if scrollback_rows <= origin:
                return
            replace_old = list(s.history)
            start = origin
            notify = False
        elif 0 <= last < scrollback_rows:
            start = last
            notify = True
        else:
            s.history.clear()
            start = 0
            notify = False

        new_rows = []
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
            if replace_old is not None:
                new_rows.append(rstrip_cells(cells))
            elif notify:
                s._retire_line(cells)
            else:
                s.history.append(rstrip_cells(cells))

        if replace_old is not None:
            s.history.clear()
            for row in merge_replace_scroll_history(replace_old, new_rows, s.rows):
                s.history.append(row)

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
