"""Unit tests for ai/terminal/screen.py (grid, scrollback, erase ops).

Run from repo root:
    python -m unittest tests.test_screen -v
"""
import unittest

from ai.terminal.screen import BLANK, Screen


def _row_text(screen, y):
    return "".join(screen.grid[y])


def _fill_row(screen, y, text):
    for i, ch in enumerate(text[: screen.cols]):
        screen.grid[y][i] = ch
        screen.attrs[y][i] = 7


class ConstructionTests(unittest.TestCase):
    def test_degenerate_size_is_clamped(self):
        s = Screen(0, 0)
        self.assertEqual((s.cols, s.rows), (1, 1))

    def test_defaults(self):
        s = Screen(4, 2)
        self.assertTrue(s.cursor_visible)
        self.assertEqual(s.cursor_shape, "block")
        self.assertEqual(s.private_modes, set())
        self.assertIsNone(s.input_caret_x)


class CursorMotionTests(unittest.TestCase):
    def test_put_char_wraps_and_line_feeds(self):
        s = Screen(2, 2)
        for ch in "abc":
            s.put_char(ch)
        self.assertEqual(_row_text(s, 0), "ab")
        self.assertEqual(_row_text(s, 1), "c" + BLANK)
        self.assertEqual((s.x, s.y), (1, 1))

    def test_wrap_on_last_row_scrolls(self):
        s = Screen(2, 1, history_cap=4)
        for ch in "abc":
            s.put_char(ch)
        self.assertEqual(len(s.history), 1)
        self.assertEqual("".join(ch for ch, _ in s.history[0]), "ab")

    def test_backspace_stops_at_column_zero(self):
        s = Screen(4, 2)
        s.bs()
        self.assertEqual(s.x, 0)
        s.put_char("a")
        s.bs()
        self.assertEqual(s.x, 0)

    def test_tab_advances_to_next_stop_and_clamps(self):
        s = Screen(20, 2)
        s.tab()
        self.assertEqual(s.x, 8)
        s.tab()
        self.assertEqual(s.x, 16)
        s.tab()
        self.assertEqual(s.x, 19)  # clamped to cols - 1

    def test_move_abs_and_rel_clamp(self):
        s = Screen(5, 3)
        s.move_abs(99, 99)
        self.assertEqual((s.y, s.x), (2, 4))
        s.move_rel(-99, -99)
        self.assertEqual((s.y, s.x), (0, 0))
        s.move_rel(1, 2)
        self.assertEqual((s.y, s.x), (1, 2))

    def test_save_restore_clamps_after_shrink(self):
        s = Screen(10, 4)
        s.move_abs(3, 9)
        s.save_cursor()
        s.resize(4, 2)
        s.restore_cursor()
        self.assertEqual((s.y, s.x), (1, 3))


class ScrollbackTests(unittest.TestCase):
    def test_retire_callback_fires_once_per_line(self):
        s = Screen(4, 1, history_cap=10)
        seen = []
        s.on_retire_line = seen.append
        _fill_row(s, 0, "ab")
        s.lf()
        _fill_row(s, 0, "cd")
        s.lf()
        self.assertEqual(seen, ["ab", "cd"])

    def test_retire_callback_exception_never_breaks_scrolling(self):
        s = Screen(4, 1, history_cap=10)

        def boom(_text):
            raise RuntimeError("logging blew up")

        s.on_retire_line = boom
        _fill_row(s, 0, "ab")
        s.lf()
        self.assertEqual(len(s.history), 1)

    def test_retired_lines_are_rstripped(self):
        s = Screen(6, 1, history_cap=10)
        _fill_row(s, 0, "ab")
        s.lf()
        self.assertEqual(len(s.history[0]), 2)

    def test_history_cap_is_honoured(self):
        s = Screen(2, 1, history_cap=2)
        for _ in range(5):
            s.lf()
        self.assertEqual(len(s.history), 2)

    def test_set_history_cap_preserves_contents(self):
        s = Screen(2, 1, history_cap=10)
        s.history.append([("a", 0)])
        s.set_history_cap(5)
        self.assertEqual(s.history.maxlen, 5)
        self.assertEqual(len(s.history), 1)

    def test_set_history_cap_noop_when_unchanged(self):
        s = Screen(2, 1, history_cap=10)
        before = s.history
        s.set_history_cap(10)
        self.assertIs(s.history, before)

    def test_set_history_cap_trims_to_newest(self):
        s = Screen(2, 1, history_cap=10)
        for ch in "abc":
            s.history.append([(ch, 0)])
        s.set_history_cap(1)
        self.assertEqual([c[0][0] for c in s.history], ["c"])

    def test_scroll_down_inserts_blank_top_row(self):
        s = Screen(3, 2)
        _fill_row(s, 0, "abc")
        s._scroll_down()
        self.assertEqual(_row_text(s, 0), BLANK * 3)
        self.assertEqual(_row_text(s, 1), "abc")
        self.assertEqual(len(s.history), 0)


class PrivateModeTests(unittest.TestCase):
    def test_set_and_clear(self):
        s = Screen(4, 2)
        s.set_private_mode("1003", True)
        self.assertEqual(s.mouse_tracking, 1003)
        s.set_private_mode(1003, False)
        self.assertEqual(s.mouse_tracking, 0)

    def test_highest_tracking_mode_wins(self):
        s = Screen(4, 2)
        s.set_private_mode(1000, True)
        s.set_private_mode(1002, True)
        self.assertEqual(s.mouse_tracking, 1002)
        s.set_private_mode(1003, True)
        self.assertEqual(s.mouse_tracking, 1003)

    def test_sgr_coordinates_flag(self):
        s = Screen(4, 2)
        self.assertFalse(s.mouse_sgr)
        s.set_private_mode(1006, True)
        self.assertTrue(s.mouse_sgr)


class EraseTests(unittest.TestCase):
    def test_erase_display_to_end(self):
        s = Screen(4, 3)
        for y in range(3):
            _fill_row(s, y, "abcd")
        s.move_abs(1, 2)
        s.erase_display(0)
        self.assertEqual(_row_text(s, 0), "abcd")
        self.assertEqual(_row_text(s, 1), "ab" + BLANK * 2)
        self.assertEqual(_row_text(s, 2), BLANK * 4)
        self.assertEqual(s.attrs[1][2], 0)

    def test_erase_display_to_start(self):
        s = Screen(4, 3)
        for y in range(3):
            _fill_row(s, y, "abcd")
        s.move_abs(1, 1)
        s.erase_display(1)
        self.assertEqual(_row_text(s, 0), BLANK * 4)
        self.assertEqual(_row_text(s, 1), BLANK * 2 + "cd")
        self.assertEqual(_row_text(s, 2), "abcd")

    def test_erase_display_all_keeps_scrollback(self):
        s = Screen(4, 2, history_cap=5)
        s.history.append([("h", 0)])
        _fill_row(s, 0, "abcd")
        s.erase_display(2)
        self.assertEqual(_row_text(s, 0), BLANK * 4)
        self.assertEqual(len(s.history), 1)

    def test_erase_display_3_clears_scrollback(self):
        s = Screen(4, 2, history_cap=5)
        s.history.append([("h", 0)])
        s.erase_display(3)
        self.assertEqual(len(s.history), 0)

    def test_erase_display_unknown_mode_is_noop(self):
        s = Screen(4, 2)
        _fill_row(s, 0, "abcd")
        s.erase_display(9)
        self.assertEqual(_row_text(s, 0), "abcd")

    def test_erase_line_variants(self):
        s = Screen(4, 1)
        _fill_row(s, 0, "abcd")
        s.x = 2
        s.erase_line(0)
        self.assertEqual(_row_text(s, 0), "ab" + BLANK * 2)

        _fill_row(s, 0, "abcd")
        s.erase_line(1)
        self.assertEqual(_row_text(s, 0), BLANK * 3 + "d")

        _fill_row(s, 0, "abcd")
        s.erase_line(2)
        self.assertEqual(_row_text(s, 0), BLANK * 4)
        self.assertEqual(s.attrs[0], [0, 0, 0, 0])


class ResizeResetTests(unittest.TestCase):
    def test_growing_preserves_content_and_pads(self):
        s = Screen(3, 2)
        _fill_row(s, 0, "abc")
        s.resize(5, 4)
        self.assertEqual(_row_text(s, 0), "abc" + BLANK * 2)
        self.assertEqual(len(s.grid), 4)
        self.assertEqual(s.attrs[0][0], 7)

    def test_shrinking_drops_offscreen_cells(self):
        s = Screen(4, 3)
        _fill_row(s, 2, "abcd")
        s.resize(2, 2)
        self.assertEqual(len(s.grid), 2)
        self.assertEqual(_row_text(s, 0), BLANK * 2)

    def test_widening_leaves_history_untouched(self):
        s = Screen(4, 2, history_cap=5)
        s.history.append([("a", 0), ("b", 0)])
        s.resize(8, 2)
        self.assertEqual(len(s.history[0]), 2)

    def test_reset_restores_defaults(self):
        s = Screen(4, 2, history_cap=5)
        _fill_row(s, 0, "abcd")
        s.history.append([("h", 0)])
        s.move_abs(1, 3)
        s.set_private_mode(1000, True)
        s.cursor_visible = False
        s.cursor_shape = "bar"
        s.input_caret_x = 3
        s.reset()
        self.assertEqual(_row_text(s, 0), BLANK * 4)
        self.assertEqual((s.x, s.y), (0, 0))
        self.assertEqual(len(s.history), 0)
        self.assertEqual(s.private_modes, set())
        self.assertTrue(s.cursor_visible)
        self.assertEqual(s.cursor_shape, "block")
        self.assertIsNone(s.input_caret_x)


class SnapshotTests(unittest.TestCase):
    def test_live_lines_text_rstrips_each_row(self):
        s = Screen(6, 3)
        _fill_row(s, 0, "hi")
        _fill_row(s, 2, "bye")
        self.assertEqual(s.live_lines_text(), ["hi", "", "bye"])

    def test_render_cells_prepends_history(self):
        s = Screen(4, 2, history_cap=5)
        s.history.append([("h", 0)])
        _fill_row(s, 0, "ab")
        s.move_abs(1, 0)
        rows, cy, cx = s.render_cells()
        self.assertEqual(len(rows), 3)
        self.assertEqual("".join(ch for ch, _ in rows[0]), "h")
        self.assertEqual((cy, cx), (2, 0))

    def test_render_cells_keeps_spaces_left_of_cursor(self):
        s = Screen(6, 1)
        _fill_row(s, 0, "ab")
        s.x = 4  # two blank cells between text and the cursor column
        rows, _cy, cx = s.render_cells()
        self.assertEqual("".join(ch for ch, _ in rows[0]), "ab" + BLANK * 2)
        self.assertEqual(cx, 4)


if __name__ == "__main__":
    unittest.main()
