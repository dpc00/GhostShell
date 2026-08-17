"""Unit tests for the DLL-free parts of ghostty_engine / ghostty_vt.

Everything that needs libghostty-vt (GhosttyParser's FFI paths) is out of
scope here: the DLL is a built binary artifact that isn't in the repo. The
pure translation helpers around it are testable, so they are tested — cell
text/colour/style translation is where the wrong-colour bugs live.

Run from repo root:
    python -m unittest tests.test_ghostty_engine -v
"""
import ctypes
import unittest

from ai.terminal import ghostty_vt as gvt
from ai.terminal.colors import (
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
from ai.terminal.ghostty_engine import GhosttyParser, _color_id, _strip_alt_screen


def _detached_parser():
    """A GhosttyParser that never loaded the DLL — pure helpers only."""
    return GhosttyParser.__new__(GhosttyParser)


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


class StripAltScreenTests(unittest.TestCase):
    def test_alt_screen_enter_and_exit_are_removed(self):
        self.assertEqual(
            _strip_alt_screen("a\x1b[?1049hb\x1b[?1049lc"), "abc"
        )
        self.assertEqual(_strip_alt_screen("\x1b[?47h\x1b[?1047l"), "")

    def test_other_private_modes_survive(self):
        text = "\x1b[?1000h\x1b[?2004h"
        self.assertEqual(_strip_alt_screen(text), text)

    def test_text_without_private_modes_is_returned_unchanged(self):
        text = "plain \x1b[31mred\x1b[0m"
        self.assertIs(_strip_alt_screen(text), text)


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
    try:
        gvt.load_library()
        return True
    except OSError:
        return False


# The gitignored DLL (ai/terminal/bin/ghostty-vt.dll, built from the ~/tools
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
        from ai.terminal.screen import Screen
        self.responses = []
        self.parser = GhosttyParser(Screen(80, 24), force_main_screen=False)
        self.parser.bind_write_pty(self.responses.append)

    def tearDown(self):
        self.parser._g.terminal_free(self.parser._term)

    def test_unbound_sink_is_a_noop_not_a_crash(self):
        from ai.terminal.screen import Screen
        parser = GhosttyParser(Screen(80, 24), force_main_screen=False)
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


def _history_lines(screen):
    return ["".join(ch for ch, _ in row).rstrip() for row in screen.history]


def _visible_text(screen):
    hist = "\n".join(_history_lines(screen))
    live = "\n".join("".join(screen.grid[r]).rstrip() for r in range(screen.rows))
    return hist + "\n" + live


@unittest.skipUnless(_dll_available(), "ghostty-vt.dll not present")
class HomeReplaceScrollTests(unittest.TestCase):
    """Codex (and similar) home+dump a full transcript inside CSI ?2026.

    Overflow must replace Screen.history, not append another copy. A
    production change that went back to always-append would make LINE-00
    appear twice after the second paint.
    """

    def setUp(self):
        from ai.terminal.screen import Screen

        self.screen = Screen(20, 4, history_cap=200)
        self.parser = GhosttyParser(self.screen, force_main_screen=True)

    def tearDown(self):
        self.parser._g.terminal_free(self.parser._term)

    def test_2026_home_dump_does_not_duplicate_transcript_in_history(self):
        first = "\n".join("LINE-%02d" % i for i in range(12)) + "\n"
        self.parser.feed(first)
        replay = "\x1b[?2026h\x1b[H" + first + "LINE-99\n" + "\x1b[?2026l"
        self.parser.feed(replay)
        self.assertEqual(_visible_text(self.screen).count("LINE-00"), 1)

    def test_split_2026_home_dump_does_not_duplicate(self):
        first = "\n".join("LINE-%02d" % i for i in range(12)) + "\n"
        self.parser.feed(first)
        self.parser.feed("\x1b[?2026h\x1b[H" + first[:40])
        self.parser.feed(first[40:] + "LINE-99\n\x1b[?2026l")
        self.assertEqual(_visible_text(self.screen).count("LINE-00"), 1)

    def test_fifty_testing_agent_dumps_keep_one_copy(self):
        from ai.terminal.screen import Screen
        from tests.mock_agent_cli import encode_replay_frame, make_turn_lines

        self.parser._g.terminal_free(self.parser._term)
        self.screen = Screen(80, 24, history_cap=2000)
        self.parser = GhosttyParser(self.screen, force_main_screen=True)
        lines = []
        for n in range(50):
            lines.extend(make_turn_lines(n, body_lines=1))
            self.parser.feed(encode_replay_frame(lines))
        vis = _visible_text(self.screen)
        self.assertEqual(vis.count("TURN-00"), 2)
        self.assertEqual(vis.count("TURN-49"), 2)

    def test_plain_scroll_without_home_still_appends(self):
        self.parser.feed("AAA\nBBB\nCCC\nDDD\nEEE\n")
        n1 = len(self.screen.history)
        self.parser.feed("FFF\nGGG\n")
        self.assertGreater(len(self.screen.history), n1)
        joined = "\n".join(_history_lines(self.screen))
        self.assertIn("AAA", joined)
        self.assertIn("EEE", joined)


class ReplaceScrollStateTests(unittest.TestCase):
    def test_2026_then_home_marks_replace(self):
        from ai.terminal.ghostty_engine import update_replace_scroll

        open_, replace = update_replace_scroll(False, False, "\x1b[?2026h\x1b[Hhello")
        self.assertTrue(open_)
        self.assertTrue(replace)

    def test_home_after_open_batch_from_prior_feed(self):
        from ai.terminal.ghostty_engine import update_replace_scroll

        open_, replace = update_replace_scroll(True, False, "\x1b[Hmore")
        self.assertTrue(open_)
        self.assertTrue(replace)

    def test_replace_latches_across_chunks_until_2026_closes(self):
        from ai.terminal.ghostty_engine import update_replace_scroll

        open_, replace = update_replace_scroll(True, True, "LINE-00\nLINE-01\n")
        self.assertTrue(open_)
        self.assertTrue(replace)

    def test_2026_without_home_does_not_replace(self):
        from ai.terminal.ghostty_engine import update_replace_scroll

        open_, replace = update_replace_scroll(False, False, "\x1b[?2026hspin\x1b[?2026l")
        self.assertFalse(open_)
        self.assertFalse(replace)


if __name__ == "__main__":
    unittest.main()
