"""Unit tests for ai/terminal/mouse.py (xterm mouse report encoding).

Run from repo root:
    python -m unittest tests.test_mouse -v
"""
import unittest

from ai.terminal.mouse import (
    BTN_LEFT,
    BTN_RIGHT,
    BTN_WHEEL_DOWN,
    BTN_WHEEL_UP,
    encode_click,
    encode_mouse,
    encode_wheel,
    st_button_to_proto,
    view_point_to_cell,
)


class StButtonTests(unittest.TestCase):
    def test_known_buttons(self):
        self.assertEqual(st_button_to_proto(1), BTN_LEFT)
        self.assertEqual(st_button_to_proto(2), 1)
        self.assertEqual(st_button_to_proto(3), BTN_RIGHT)

    def test_string_button_is_coerced(self):
        self.assertEqual(st_button_to_proto("1"), BTN_LEFT)

    def test_unknown_button_is_none(self):
        self.assertIsNone(st_button_to_proto(4))


class SgrEncodingTests(unittest.TestCase):
    def test_press_and_release_finals(self):
        self.assertEqual(encode_mouse(BTN_LEFT, 3, 4), "\x1b[<0;3;4M")
        self.assertEqual(encode_mouse(BTN_LEFT, 3, 4, press=False), "\x1b[<0;3;4m")

    def test_modifier_and_motion_bits(self):
        self.assertEqual(encode_mouse(BTN_LEFT, 1, 1, motion=True), "\x1b[<32;1;1M")
        self.assertEqual(
            encode_mouse(BTN_LEFT, 1, 1, shift=True, meta=True, ctrl=True),
            "\x1b[<28;1;1M",
        )

    def test_coordinates_are_one_based(self):
        self.assertEqual(encode_mouse(BTN_LEFT, 0, -5), "\x1b[<0;1;1M")

    def test_click_is_press_then_release(self):
        self.assertEqual(
            encode_click(BTN_RIGHT, 2, 7),
            "\x1b[<2;2;7M\x1b[<2;2;7m",
        )

    def test_wheel_has_no_release(self):
        self.assertEqual(encode_wheel(True, 4, 5), "\x1b[<%d;4;5M" % BTN_WHEEL_UP)
        self.assertEqual(encode_wheel(False, 4, 5), "\x1b[<%d;4;5M" % BTN_WHEEL_DOWN)


class X10EncodingTests(unittest.TestCase):
    def test_press(self):
        self.assertEqual(
            encode_mouse(BTN_RIGHT, 2, 3, sgr=False),
            "\x1b[M" + chr(32 + 2) + chr(32 + 2) + chr(32 + 3),
        )

    def test_release_uses_all_buttons_up_code(self):
        seq = encode_mouse(BTN_LEFT, 1, 1, press=False, sgr=False)
        self.assertEqual(seq, "\x1b[M" + chr(32 + 3) + chr(33) + chr(33))

    def test_release_keeps_modifier_bits(self):
        seq = encode_mouse(BTN_LEFT, 1, 1, press=False, sgr=False, ctrl=True, shift=True)
        self.assertEqual(seq[3], chr(32 + 3 + 4 + 16))

    def test_wheel_release_is_not_remapped(self):
        seq = encode_mouse(BTN_WHEEL_UP, 1, 1, press=False, sgr=False)
        self.assertEqual(seq[3], chr(32 + BTN_WHEEL_UP))

    def test_coordinates_and_button_are_clamped_to_223(self):
        seq = encode_mouse(BTN_LEFT, 500, 400, sgr=False)
        self.assertEqual(seq[4], chr(223 + 32))
        self.assertEqual(seq[5], chr(223 + 32))
        seq = encode_mouse(300, 1, 1, sgr=False)
        self.assertEqual(seq[3], chr(223 + 32))


class ViewPointTests(unittest.TestCase):
    def test_maps_view_row_past_history(self):
        self.assertEqual(
            view_point_to_cell(2, 0, hist_len=2, screen_rows=10, screen_cols=80),
            (1, 1),
        )

    def test_click_above_the_live_grid_is_none(self):
        self.assertIsNone(
            view_point_to_cell(1, 0, hist_len=2, screen_rows=10, screen_cols=80)
        )

    def test_click_below_the_live_grid_is_none(self):
        self.assertIsNone(
            view_point_to_cell(12, 0, hist_len=2, screen_rows=10, screen_cols=80)
        )

    def test_column_is_clamped_into_the_grid(self):
        self.assertEqual(
            view_point_to_cell(0, 999, hist_len=0, screen_rows=4, screen_cols=10),
            (10, 1),
        )
        self.assertEqual(
            view_point_to_cell(0, -3, hist_len=0, screen_rows=4, screen_cols=10),
            (1, 1),
        )


if __name__ == "__main__":
    unittest.main()
