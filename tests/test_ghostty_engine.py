"""Unit tests for the DLL-free parts of ghostty_engine / ghostty_vt.

Everything that needs libghostty-vt (GhosttyParser's FFI paths) is out of
scope here: the DLL is a built binary artifact that isn't in the repo. The
pure translation helpers around it are testable, so they are tested — cell
text/colour/style translation is where the wrong-colour bugs live.

Run from repo root:
    python -m unittest tests.test_ghostty_engine -v
"""
import ctypes
import os
import unittest

from terminal import ghostty_vt as gvt
from terminal.colors import (
    ATTR_BG_MASK,
    ATTR_FG_MASK,
    BG_SHIFT,
    BOLD,
    FAINT,
    ITALIC,
    REVERSE,
    UNDERLINE,
    quantize256,
)
from terminal.ghostty_engine import (
    GhosttyParser,
    _color_id,
)


def _detached_parser():
    """A GhosttyParser that never loaded the DLL — pure helpers only."""
    return GhosttyParser.__new__(GhosttyParser)


class RawVtInputTests(unittest.TestCase):
    class _Native:
        def terminal_vt_write(self, _term, _data, _length):
            pass

    def _parser(self):
        parser = _detached_parser()
        parser._g = self._Native()
        parser._term = None
        parser.s = type("ScreenStub", (), {"sync_output": False})()
        parser._mode = lambda _mode: False
        return parser

    def test_feed_passes_vt_sequences_to_ghostty_unchanged(self):
        parser = self._parser()
        writes = []
        parser._g.terminal_vt_write = (
            lambda _term, data, length: writes.append(bytes(data[:length]))
        )
        parser._sync = lambda: None

        text = "before\x1b[?1049;2004h\x1b[Hinside\x1b[?1049;2004lafter"
        parser.feed(text)

        self.assertEqual(writes, [text.encode()])

    def test_broker_bootstrap_advances_native_terminal_without_sync(self):
        parser = self._parser()
        writes = []
        parser._g.terminal_vt_write = (
            lambda _term, data, length: writes.append(bytes(data[:length]))
        )
        parser._sync = lambda: self.fail("broker bootstrap must not sync")

        parser.feed_bootstrap("restored replay")

        self.assertEqual(writes, [b"restored replay"])

    def test_open_synchronized_frame_defers_python_sync(self):
        parser = self._parser()
        parser._mode = lambda _mode: True
        parser._sync = lambda: self.fail("open synchronized frame must not sync")
        parser.feed("\x1b[?2026hpartial frame")
        self.assertTrue(parser.s.sync_output)

    def test_closed_synchronized_frame_synchronizes_once(self):
        parser = self._parser()
        calls = []
        parser._sync = lambda: calls.append("sync")
        parser.feed("rest of frame\x1b[?2026l")
        self.assertEqual(calls, ["sync"])

    def test_resize_forces_full_scrollback_rebuild_on_next_sync(self):
        # Screen.resize() only clips/pads cells -- it does not reflow (no
        # per-row "was this a wrapped continuation" bookkeeping to reflow
        # from). Forcing _last_scrollback_rows back to -1 is what makes
        # the *next* _sync_scrollback() fall through to a full rebuild
        # from the native terminal (which does reflow) instead of an
        # incremental append or a shortcut that trusted Screen.resize()'s
        # own shallow adjustment. Deliberately reverted 2026-09-02 -- see
        # GhosttyParser.resize()'s own comment for the live-verified bug
        # the removed shortcut used to leave in place.
        from terminal.screen import Screen

        parser = _detached_parser()
        parser._g = type(
            "Native", (), {"terminal_resize": staticmethod(lambda *a: gvt.SUCCESS)}
        )()
        parser._term = None
        parser.s = Screen(80, 24)
        parser._last_scrollback_rows = 12345

        parser.resize(100, 30)

        self.assertEqual(parser._last_scrollback_rows, -1)


def _style(**kwargs):
    style = gvt.GhosttyStyle().init()
    for name, value in kwargs.items():
        setattr(style, name, value)
    return style


def _style_color(tag, palette=0, rgb=(0, 0, 0)):
    color = gvt.GhosttyStyleColor()
    color.tag = tag
    if tag == gvt.STYLE_COLOR_PALETTE:
        color.value.palette = palette
    elif tag == gvt.STYLE_COLOR_RGB:
        color.value.rgb = gvt.GhosttyColorRgb(*rgb)
    return color


class ColorIdTests(unittest.TestCase):
    def test_failed_lookup_is_default(self):
        self.assertEqual(_color_id(gvt.INVALID_VALUE, gvt.GhosttyColorRgb(255, 0, 0)), 0)

    def test_rgb_is_quantized_to_a_one_based_palette_id(self):
        self.assertEqual(
            _color_id(gvt.SUCCESS, gvt.GhosttyColorRgb(255, 0, 0)),
            quantize256(255, 0, 0) + 1,
        )


class StyleFlagTests(unittest.TestCase):
    def setUp(self):
        self.parser = _detached_parser()

    def test_no_styles(self):
        self.assertEqual(self.parser._style_flags(_style()), 0)

    def test_each_rendered_style_maps_to_its_bit(self):
        self.assertEqual(self.parser._style_flags(_style(bold=True)), BOLD)
        self.assertEqual(self.parser._style_flags(_style(inverse=True)), REVERSE)
        self.assertEqual(self.parser._style_flags(_style(faint=True)), FAINT)
        self.assertEqual(self.parser._style_flags(_style(italic=True)), ITALIC)

    def test_underline_is_an_enum_not_a_bool(self):
        self.assertEqual(self.parser._style_flags(_style(underline=0)), 0)
        for value in (1, 2, 3):  # single / double / curly all mean "underlined"
            self.assertEqual(self.parser._style_flags(_style(underline=value)), UNDERLINE)

    def test_flags_combine(self):
        flags = self.parser._style_flags(_style(bold=True, inverse=True, italic=True))
        self.assertEqual(flags, BOLD | REVERSE | ITALIC)

    def test_unrendered_styles_are_ignored(self):
        self.assertEqual(
            self.parser._style_flags(_style(strikethrough=True, overline=True, blink=True)),
            0,
        )


class FinishCellTests(unittest.TestCase):
    def setUp(self):
        self.parser = _detached_parser()

    def test_empty_text_becomes_a_blank_cell(self):
        text, attr = self.parser._finish_cell("", 5, 0, 0)
        self.assertEqual(text, " ")
        self.assertEqual(attr, 0)  # fg dropped so rstrip can trim the cell

    def test_blank_keeps_its_background(self):
        _text, attr = self.parser._finish_cell(" ", 5, 3, 0)
        self.assertEqual(attr & ATTR_FG_MASK, 5)
        self.assertEqual((attr & ATTR_BG_MASK) >> BG_SHIFT, 3)

    def test_reversed_blank_keeps_its_foreground(self):
        _text, attr = self.parser._finish_cell(" ", 5, 0, REVERSE)
        self.assertEqual(attr & ATTR_FG_MASK, 5)

    def test_real_glyph_keeps_its_foreground(self):
        text, attr = self.parser._finish_cell("x", 5, 0, BOLD)
        self.assertEqual(text, "x")
        self.assertEqual(attr & ATTR_FG_MASK, 5)
        self.assertTrue(attr & BOLD)


class ResolveStyleColorTests(unittest.TestCase):
    def setUp(self):
        self.parser = _detached_parser()
        self.palette = (gvt.GhosttyColorRgb * 256)(
            *[gvt.GhosttyColorRgb(0, 0, 0) for _ in range(256)]
        )
        self.palette[9] = gvt.GhosttyColorRgb(255, 0, 0)

    def test_none_is_default(self):
        color = _style_color(gvt.STYLE_COLOR_NONE)
        self.assertEqual(self.parser._resolve_style_color(color, self.palette), 0)

    def test_palette_entry_is_resolved_through_the_palette(self):
        color = _style_color(gvt.STYLE_COLOR_PALETTE, palette=9)
        self.assertEqual(
            self.parser._resolve_style_color(color, self.palette),
            quantize256(255, 0, 0) + 1,
        )

    def test_rgb_is_quantized(self):
        color = _style_color(gvt.STYLE_COLOR_RGB, rgb=(0, 0, 255))
        self.assertEqual(
            self.parser._resolve_style_color(color, self.palette),
            quantize256(0, 0, 255) + 1,
        )


class GhosttyVtBindingTests(unittest.TestCase):
    def test_point_packs_tag_and_coordinates(self):
        pt = gvt.point(gvt.POINT_TAG_SCREEN, 4, 9)
        self.assertEqual(pt.tag, gvt.POINT_TAG_SCREEN)
        self.assertEqual((pt.value.coordinate.x, pt.value.coordinate.y), (4, 9))

    def test_struct_init_records_its_own_size(self):
        self.assertEqual(gvt.GhosttyStyle().init().size, ctypes.sizeof(gvt.GhosttyStyle))
        self.assertEqual(
            gvt.GhosttyGridRef().init().size, ctypes.sizeof(gvt.GhosttyGridRef)
        )

    def test_modifier_bitmask(self):
        self.assertEqual(gvt.st_mods_to_ghostty(), 0)
        self.assertEqual(
            gvt.st_mods_to_ghostty(ctrl=True, alt=True, shift=True),
            gvt.MODS_CTRL | gvt.MODS_ALT | gvt.MODS_SHIFT,
        )
        self.assertEqual(gvt.st_mods_to_ghostty(shift=True), gvt.MODS_SHIFT)

    def test_key_name_map_covers_named_and_printable_keys(self):
        self.assertEqual(gvt.ST_KEY_TO_GHOSTTY["a"], gvt.KEY_A)
        self.assertEqual(gvt.ST_KEY_TO_GHOSTTY["keypad_enter"], gvt.KEY_NUMPAD_ENTER)
        self.assertNotIn("mouse1", gvt.ST_KEY_TO_GHOSTTY)

    def test_missing_library_raises(self):
        with self.assertRaises(OSError):
            gvt.load_library("/nonexistent/ghostty-vt.dll")


def _dll_available():
    # Collection must not download a native binary. Provision it explicitly
    # before running native tests, as documented for offline installation.
    path = os.environ.get("GHOSTTY_VT_DLL") or gvt.DEFAULT_DLL_PATH
    if not os.path.isfile(path):
        return False
    try:
        gvt.load_library(path)
        return True
    except OSError:
        return False


@unittest.skipUnless(_dll_available(), "ghostty-vt.dll not present")
class BuildInfoTests(unittest.TestCase):
    def test_loaded_library_reports_recorded_version(self):
        self.assertEqual(gvt.libghostty_version(gvt.load_library()), "0.1.0-dev")


# The gitignored DLL (terminal/bin/ghostty-vt.dll, built from the ~/tools
# ghostty checkout) isn't in the repo, so these tests skip cleanly wherever
# it's absent rather than failing the suite.
@unittest.skipUnless(_dll_available(), "ghostty-vt.dll not present")
class WritePtyCallbackTests(unittest.TestCase):
    """Covers the write_pty/size wiring that fixes a real hang: a startup
    capability probe (DA/kitty-flags/XTVERSION/size query) that
    libghostty-vt parses internally but, without these callbacks, has
    nowhere to send a response -- so the child blocks forever waiting for
    an answer that never comes. See ghostty_engine.py's GhosttyParser
    docstrings on bind_write_pty/_on_size_query."""

    def setUp(self):
        from terminal.screen import Screen
        self.responses = []
        self.parser = GhosttyParser(Screen(80, 24))
        self.parser.bind_write_pty(self.responses.append)

    def tearDown(self):
        self.parser._g.terminal_free(self.parser._term)

    def test_unbound_sink_is_a_noop_not_a_crash(self):
        from terminal.screen import Screen
        parser = GhosttyParser(Screen(80, 24))
        try:
            parser.feed("\x1b[c")  # DA1 query, sink never bound
        finally:
            parser._g.terminal_free(parser._term)
        # No exception is the assertion; nothing else to check.

    def test_kitty_flags_query_gets_a_response(self):
        # This is the exact query Grok's client uses to detect Kitty
        # keyboard protocol support (grok doctor: "keyboard protocol is
        # unavailable" when it goes unanswered).
        self.parser.feed("\x1b[?u")
        self.assertEqual(self.responses, [b"\x1b[?0u"])

    def test_device_attributes_query_gets_a_response(self):
        self.parser.feed("\x1b[c")
        self.assertEqual(len(self.responses), 1)
        self.assertTrue(self.responses[0].startswith(b"\x1b[?"))

    def test_size_query_reports_real_screen_dimensions(self):
        self.parser.feed("\x1b[18t")
        self.assertEqual(self.responses, [b"\x1b[8;24;80t"])

    def test_size_query_after_resize_reports_new_dimensions(self):
        self.parser.resize(100, 40)
        self.parser.feed("\x1b[18t")
        self.assertEqual(self.responses, [b"\x1b[8;40;100t"])


@unittest.skipUnless(_dll_available(), "ghostty-vt.dll not present")
class SyncOutputModeTests(unittest.TestCase):
    """screen.sync_output (DEC mode 2026) is queried fresh from the native
    terminal after every feed (ghostty_terminal_mode_get), replacing what
    used to be a separate Python regex tracker in ai_terminal.py. Native
    modes are inherently a boolean level, not a stack, so a repeated "h"
    while already open (confirmed live: Grok sends exactly this, h twice
    per l) can never become a nested/counted open the way a naive counter
    could."""

    def setUp(self):
        from terminal.screen import Screen
        self.screen = Screen(80, 24)
        self.parser = GhosttyParser(self.screen)

    def tearDown(self):
        self.parser._g.terminal_free(self.parser._term)

    def test_closed_by_default(self):
        self.assertFalse(self.screen.sync_output)

    def test_opens_on_h_and_closes_on_l(self):
        self.parser.feed("\x1b[?2026h")
        self.assertTrue(self.screen.sync_output)
        self.parser.feed("some content")
        self.assertTrue(self.screen.sync_output, "stays open until the matching l")
        self.parser.feed("\x1b[?2026l")
        self.assertFalse(self.screen.sync_output)

    def test_repeated_h_is_a_level_not_a_stack(self):
        self.parser.feed("\x1b[?2026h\x1b[?2026h")
        self.assertTrue(self.screen.sync_output)
        # A single closing l must fully close it -- if this were a stack
        # (incrementing on each h), it would incorrectly still read open
        # after only one l.
        self.parser.feed("\x1b[?2026l")
        self.assertFalse(self.screen.sync_output)


@unittest.skipUnless(_dll_available(), "ghostty-vt.dll not present")
class AlternateScreenTests(unittest.TestCase):
    def setUp(self):
        from terminal.screen import Screen

        self.screen = Screen(20, 4)
        self.parser = GhosttyParser(self.screen)

    def tearDown(self):
        self.parser.close()

    def test_decset_1049_uses_ghosttys_real_alternate_buffer(self):
        self.parser.feed("primary")
        self.parser.feed("\x1b[?1049halt\x1b[?2004h")

        self.assertTrue(self.screen.alt_screen)
        self.assertIn("alt", "".join(self.screen.grid[0]))
        self.assertIn(2004, self.screen.private_modes)

        self.parser.feed("\x1b[?1049l")
        self.assertFalse(self.screen.alt_screen)
        self.assertEqual("".join(self.screen.grid[0][:7]), "primary")


@unittest.skipUnless(_dll_available(), "ghostty-vt.dll not present")
class ParserCloseTests(unittest.TestCase):
    """GhosttyParser.close() frees the terminal, render state, and (if
    ever created) the key/mouse encoder/event -- previously nothing did,
    so a closed tab leaked all of it until Sublime restarted. No tearDown
    here: each test is responsible for its own single close() call, since
    a second real free of an already-freed native handle (not exercised
    by these tests, which test the idempotency guard, not double-freeing
    past it) would be the actual bug this class exists to catch."""


    def test_close_is_safe_with_no_keys_ever_encoded(self):
        from terminal.screen import Screen
        parser = GhosttyParser(Screen(80, 24))
        parser.close()  # no exception is the assertion

    def test_close_frees_the_lazily_created_key_encoder_too(self):
        from terminal.screen import Screen
        parser = GhosttyParser(Screen(80, 24))
        # Allocates _key_encoder/_key_event on first use -- see encode_key.
        parser.encode_key("a")
        self.assertTrue(hasattr(parser, "_key_encoder"))
        parser.close()  # no exception is the assertion

    def test_close_is_idempotent(self):
        from terminal.screen import Screen
        parser = GhosttyParser(Screen(80, 24))
        parser.close()
        parser.close()  # must not double-free; no exception is the assertion

    def test_close_frees_the_lazily_created_mouse_encoder_too(self):
        from terminal.screen import Screen
        parser = GhosttyParser(Screen(80, 24))
        parser.encode_mouse(0, 1, 1)
        self.assertTrue(hasattr(parser, "_mouse_encoder"))
        parser.close()


@unittest.skipUnless(_dll_available(), "ghostty-vt.dll not present")
class NativeMouseEncodeTests(unittest.TestCase):
    """GhosttyParser.encode_mouse respects live DECSET tracking/format."""

    def setUp(self):
        from terminal.screen import Screen
        self.parser = GhosttyParser(Screen(80, 24))

    def tearDown(self):
        self.parser.close()

    def test_no_tracking_encodes_empty(self):
        self.assertEqual(self.parser.encode_mouse(0, 3, 4), "")

    def test_sgr_click_after_decset_1000_1006(self):
        self.parser.feed("\x1b[?1000h\x1b[?1006h")
        self.assertEqual(self.parser.encode_mouse(0, 3, 4), "\x1b[<0;3;4M")
        self.assertEqual(
            self.parser.encode_mouse(0, 3, 4, press=False),
            "\x1b[<0;3;4m",
        )

    def test_normal_mode_drops_motion(self):
        self.parser.feed("\x1b[?1000h\x1b[?1006h")
        self.assertEqual(
            self.parser.encode_mouse(0, 1, 1, motion=True),
            "",
        )

    def test_any_event_mode_reports_motion(self):
        self.parser.feed("\x1b[?1003h\x1b[?1006h")
        self.assertEqual(
            self.parser.encode_mouse(0, 1, 1, motion=True),
            "\x1b[<32;1;1M",
        )

    def test_same_cell_motion_is_deduped_natively(self):
        self.parser.feed("\x1b[?1003h\x1b[?1006h")
        self.assertEqual(
            self.parser.encode_mouse(0, 2, 2, motion=True),
            "\x1b[<32;2;2M",
        )
        self.assertEqual(self.parser.encode_mouse(0, 2, 2, motion=True), "")
        self.assertEqual(
            self.parser.encode_mouse(0, 3, 2, motion=True),
            "\x1b[<32;3;2M",
        )





if __name__ == "__main__":
    unittest.main()
