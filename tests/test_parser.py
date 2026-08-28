"""Unit tests for terminal/parser.py (ANSI/VT state machine).

Run from repo root:
    python -m unittest tests.test_parser -v
"""
import unittest

from terminal.colors import ATTR_BG_MASK, ATTR_FG_MASK, BG_SHIFT, BOLD, FAINT, REVERSE
from terminal.parser import Parser
from terminal.screen import BLANK, Screen


def _feed(text, cols=20, rows=4, **kwargs):
    screen = Screen(cols, rows)
    Parser(screen, **kwargs).feed(text)
    return screen


def _row(screen, y=0):
    return "".join(screen.grid[y])


class C0Tests(unittest.TestCase):
    def test_cr_lf_bs_tab(self):
        s = _feed("ab\r\n\tc", cols=12, rows=3)
        self.assertEqual(_row(s, 0), "ab" + BLANK * 10)
        self.assertEqual(_row(s, 1), BLANK * 8 + "c" + BLANK * 3)
        s = _feed("ab\bX")
        self.assertEqual(_row(s)[:2], "aX")

    def test_vertical_tab_and_form_feed_line_feed(self):
        s = _feed("a\x0bb\x0cc", cols=4, rows=4)
        self.assertEqual(_row(s, 0), "a" + BLANK * 3)
        self.assertEqual(_row(s, 1)[1], "b")
        self.assertEqual(_row(s, 2)[2], "c")

    def test_bel_and_other_c0_are_dropped(self):
        s = _feed("a\x07\x01\x7fb")
        self.assertEqual(_row(s)[:2], "ab")


class EscTests(unittest.TestCase):
    def test_save_and_restore_cursor(self):
        s = _feed("\x1b[3;4H\x1b7\x1b[1;1H\x1b8")
        self.assertEqual((s.y, s.x), (2, 3))

    def test_index_and_next_line(self):
        s = _feed("ab\x1bDc", cols=6, rows=3)
        self.assertEqual(_row(s, 1)[2], "c")
        s = _feed("ab\x1bEc", cols=6, rows=3)
        self.assertEqual(_row(s, 1)[0], "c")

    def test_ris_resets_screen_and_attrs(self):
        s = Screen(8, 2)
        p = Parser(s)
        p.feed("\x1b[31;1mred\x1b[?1000h")
        p.feed("\x1bc")
        self.assertEqual(_row(s), BLANK * 8)
        self.assertEqual(s.private_modes, set())
        p.feed("x")
        self.assertEqual(s.attrs[0][0], 0)

    def test_reverse_index_is_consumed(self):
        s = _feed("a\x1bMb")
        self.assertEqual(_row(s)[:2], "ab")

    def test_unknown_escape_consumes_only_its_final(self):
        s = _feed("\x1b(B" + "x")
        self.assertEqual(_row(s)[:2], "Bx")


class OscTests(unittest.TestCase):
    def test_bel_terminated_title(self):
        s = _feed("\x1b]0;my title\x07done")
        self.assertEqual(_row(s)[:4], "done")

    def test_st_terminated_title(self):
        s = _feed("\x1b]0;my title\x1b\\done")
        self.assertEqual(_row(s)[:4], "done")


class CsiFramingTests(unittest.TestCase):
    def test_intermediate_bytes_are_ignored(self):
        # DECSCUSR (CSI 1 SP q) is consumed and dropped, text still lands.
        s = _feed("\x1b[1 qX")
        self.assertEqual(_row(s)[0], "X")

    def test_invalid_byte_aborts_the_sequence(self):
        s = _feed("\x1b[1\x01X")
        self.assertEqual(_row(s)[0], "X")

    def test_unhandled_final_is_dropped(self):
        s = _feed("ab\x1b[2PX")  # DCH not implemented
        self.assertEqual(_row(s)[:3], "abX")

    def test_non_numeric_params_default_to_zero(self):
        s = _feed("\x1b[;5HX")  # row omitted -> 1, col 5
        self.assertEqual((s.y, s.x), (0, 5))
        self.assertEqual(_row(s)[4], "X")


class CursorPositioningTests(unittest.TestCase):
    def test_cup_and_hvp_are_one_based(self):
        s = _feed("\x1b[2;3H")
        self.assertEqual((s.y, s.x), (1, 2))
        s = _feed("\x1b[2;3f")
        self.assertEqual((s.y, s.x), (1, 2))

    def test_arrows_default_to_one(self):
        s = _feed("\x1b[5;5H\x1b[A\x1b[C")  # rows=4, so CUP row 5 clamps to y=3
        self.assertEqual((s.y, s.x), (2, 5))
        s = _feed("\x1b[5;5H\x1b[2B\x1b[2D")
        self.assertEqual((s.y, s.x), (3, 2))  # rows=4, so y clamps

    def test_cha_and_vpa(self):
        s = _feed("\x1b[2;2H\x1b[7G")
        self.assertEqual((s.y, s.x), (1, 6))
        s = _feed("\x1b[2;5H\x1b[3d")
        self.assertEqual((s.y, s.x), (2, 4))

    def test_save_restore_via_csi(self):
        s = _feed("\x1b[2;2H\x1b[s\x1b[1;1H\x1b[u")
        self.assertEqual((s.y, s.x), (1, 1))


class EraseTests(unittest.TestCase):
    def test_ed_and_el_default_to_zero(self):
        s = _feed("abcd\x1b[1;3H\x1b[K", cols=6, rows=2)
        self.assertEqual(_row(s), "ab" + BLANK * 4)
        s = _feed("abcd\x1b[1;1H\x1b[J", cols=6, rows=2)
        self.assertEqual(_row(s), BLANK * 6)

    def test_ech_defaults_to_one_and_clamps_to_row_end(self):
        s = _feed("abcd\x1b[1;2H\x1b[X", cols=6, rows=2)
        self.assertEqual(_row(s), "a" + BLANK + "cd" + BLANK * 2)
        s = _feed("abcd\x1b[1;3H\x1b[99X", cols=6, rows=2)
        self.assertEqual(_row(s), "ab" + BLANK * 4)

    def test_ech_clears_attributes_too(self):
        s = _feed("\x1b[31mabc\x1b[1;1H\x1b[3X", cols=6, rows=2)
        self.assertEqual(s.attrs[0][:3], [0, 0, 0])


class ScrollTests(unittest.TestCase):
    def test_su_retires_rows_into_history(self):
        s = Screen(4, 3, history_cap=10)
        p = Parser(s)
        p.feed("ab\r\ncd\x1b[2S")
        self.assertEqual([("".join(ch for ch, _ in line)) for line in s.history], ["ab", "cd"])
        self.assertEqual(_row(s, 0), BLANK * 4)

    def test_sd_pushes_rows_down(self):
        s = _feed("ab\x1b[T", cols=4, rows=3)
        self.assertEqual(_row(s, 0), BLANK * 4)
        self.assertEqual(_row(s, 1), "ab" + BLANK * 2)


class PrivateModeTests(unittest.TestCase):
    def test_unknown_private_modes_are_recorded(self):
        s = _feed("\x1b[?2004h\x1b[?2026h")
        self.assertEqual(s.private_modes, {2004, 2026})
        s = _feed("\x1b[?2004h\x1b[?2004l")
        self.assertEqual(s.private_modes, set())

    def test_multiple_modes_in_one_sequence(self):
        s = _feed("\x1b[?1002;1006h")
        self.assertEqual(s.private_modes, {1002, 1006})

    def test_non_numeric_private_param_skipped(self):
        s = _feed("\x1b[?1000:2;1006h")
        self.assertEqual(s.private_modes, {1006})

    def test_alt_screen_mode_recorded_even_when_forced_to_main(self):
        s = _feed("\x1b[?1049h", force_main_screen=True)
        self.assertFalse(s.alt_screen)
        self.assertIn(1049, s.private_modes)

    def test_non_private_set_mode_is_dropped(self):
        s = _feed("\x1b[4hX")
        self.assertEqual(s.private_modes, set())
        self.assertEqual(_row(s)[0], "X")


class SgrTests(unittest.TestCase):
    def _attr(self, sequence):
        s = _feed(sequence + "X")
        return s.attrs[0][0]

    def test_bare_csi_m_resets(self):
        s = Screen(6, 2)
        p = Parser(s)
        p.feed("\x1b[31;1m")
        p.feed("\x1b[mX")
        self.assertEqual(s.attrs[0][0], 0)

    def test_basic_and_bright_foreground(self):
        self.assertEqual(self._attr("\x1b[31m") & ATTR_FG_MASK, 2)
        self.assertEqual(self._attr("\x1b[91m") & ATTR_FG_MASK, 10)

    def test_basic_and_bright_background(self):
        self.assertEqual((self._attr("\x1b[41m") & ATTR_BG_MASK) >> BG_SHIFT, 2)
        self.assertEqual((self._attr("\x1b[101m") & ATTR_BG_MASK) >> BG_SHIFT, 10)

    def test_default_color_resets(self):
        self.assertEqual(self._attr("\x1b[31;39m") & ATTR_FG_MASK, 0)
        self.assertEqual((self._attr("\x1b[41;49m") & ATTR_BG_MASK) >> BG_SHIFT, 0)

    def test_256_color_indices(self):
        self.assertEqual(self._attr("\x1b[38;5;9m") & ATTR_FG_MASK, 10)
        self.assertEqual((self._attr("\x1b[48;5;0m") & ATTR_BG_MASK) >> BG_SHIFT, 1)

    def test_truecolor_background_is_quantized(self):
        from terminal.colors import quantize256

        bg = (self._attr("\x1b[48;2;0;0;255m") & ATTR_BG_MASK) >> BG_SHIFT
        self.assertEqual(bg, quantize256(0, 0, 255) + 1)

    def test_truncated_extended_color_spec_is_default(self):
        self.assertEqual(self._attr("\x1b[38;5m") & ATTR_FG_MASK, 0)
        self.assertEqual(self._attr("\x1b[38;2;10m") & ATTR_FG_MASK, 0)
        self.assertEqual(self._attr("\x1b[38;9m") & ATTR_FG_MASK, 0)

    def test_extended_spec_does_not_swallow_following_params(self):
        attr = self._attr("\x1b[38;5;9;1m")
        self.assertEqual(attr & ATTR_FG_MASK, 10)
        self.assertTrue(attr & BOLD)

    def test_bold_faint_and_reverse_clears(self):
        self.assertTrue(self._attr("\x1b[1m") & BOLD)
        self.assertFalse(self._attr("\x1b[1;21m") & BOLD)
        attr = self._attr("\x1b[1;2;22m")
        self.assertFalse(attr & BOLD)
        self.assertFalse(attr & FAINT)
        self.assertFalse(self._attr("\x1b[7;27m") & REVERSE)

    def test_parsed_but_unrendered_styles_keep_the_stream_in_sync(self):
        attr = self._attr("\x1b[3;4;9;23;24;29;31m")
        self.assertEqual(attr & ATTR_FG_MASK, 2)


if __name__ == "__main__":
    unittest.main()
