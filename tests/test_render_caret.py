"""Unit tests for the render/caret/colour helpers' edge cases.

Complements tests/test_terminal_core.py, which covers the happy paths; these
target the guard clauses and shape/style variants.

Run from repo root:
    python -m unittest tests.test_render_caret -v
"""
import unittest

from terminal.caret import (
    adjust_display_caret,
    content_end_col,
    field_right_limit,
    find_prompt_row,
    input_start_col,
    pad_row_for_caret,
)
from terminal.colors import (
    BOLD,
    ITALIC,
    UNDERLINE,
    font_style_for,
    hex_channel_sum,
    hex_luma,
    pack_attr,
    rstrip_cells,
    scheme_colors_for,
    scope_name_for,
    style_id_for,
    xterm_hex,
)
from terminal.render import (
    HOST_CURSOR_SCOPE,
    build_text_and_regions,
    cell_needs_host_cursor,
    cursor_text_offset,
    paint_host_cursor,
    punch_host_cursor_region,
)
from terminal.screen import Screen


def _screen_with(lines, cols=40, rows=None):
    screen = Screen(cols, rows or max(len(lines), 1))
    for y, line in enumerate(lines):
        for x, ch in enumerate(line[:cols]):
            screen.grid[y][x] = ch
    return screen


class HostCursorGuardTests(unittest.TestCase):
    def test_out_of_range_positions_are_refused(self):
        rows = [[("a", 0)]]
        for cy, cx in ((None, 0), (0, None), (-1, 0), (0, -1), (5, 0)):
            self.assertFalse(cell_needs_host_cursor(rows, cy, cx))
            self.assertEqual(paint_host_cursor(rows, cy, cx), (rows, False))
            self.assertIsNone(cursor_text_offset(rows, cy, cx))
        self.assertFalse(cell_needs_host_cursor(None, 0, 0))
        self.assertEqual(paint_host_cursor(None, 0, 0), (None, False))
        self.assertIsNone(cursor_text_offset(None, 0, 0))

    def test_offset_past_row_end_is_none(self):
        self.assertIsNone(cursor_text_offset([[("a", 0)]], 0, 4))

    def test_offset_accounts_for_newlines(self):
        rows = [[("a", 0), ("b", 0)], [("c", 0)]]
        self.assertEqual(cursor_text_offset(rows, 1, 0), 3)


class HostCursorShapeTests(unittest.TestCase):
    def _glyph(self, shape):
        rows, painted = paint_host_cursor([[(" ", 0)]], 0, 0, shape=shape)
        self.assertTrue(painted)
        return rows[0][0][0]

    def test_decscusr_shapes(self):
        self.assertEqual(self._glyph("block"), "\u2588")
        self.assertEqual(self._glyph("bar"), "\u258f")
        self.assertEqual(self._glyph("underline"), "\u2581")
        self.assertEqual(self._glyph("hollow"), "\u25af")

    def test_unknown_shape_falls_back_to_block(self):
        self.assertEqual(self._glyph("beam-o-matic"), "\u2588")

    def test_nbsp_counts_as_blank(self):
        rows, painted = paint_host_cursor([[("\u00a0", 0)]], 0, 0)
        self.assertTrue(painted)
        self.assertEqual(rows[0][0][0], "\u2588")

    def test_source_rows_are_not_mutated(self):
        original = [[("a", 0)]]
        paint_host_cursor(original, 0, 3)
        self.assertEqual(original, [[("a", 0)]])


class PunchRegionTests(unittest.TestCase):
    def test_none_offset_and_empty_span_pass_through(self):
        regions = [[0, 3, "ai.fb.2.0"]]
        self.assertIs(punch_host_cursor_region(regions, None), regions)
        self.assertIs(punch_host_cursor_region(regions, 2, end=2), regions)

    def test_non_overlapping_regions_are_kept(self):
        punched = punch_host_cursor_region([[0, 2, "a"], [5, 7, "b"]], 3)
        self.assertEqual(punched, [[0, 2, "a"], [5, 7, "b"], [3, 4, HOST_CURSOR_SCOPE]])

    def test_covering_region_is_split_on_both_sides(self):
        punched = punch_host_cursor_region([[0, 6, "a"]], 2, end=4)
        self.assertEqual(punched, [[0, 2, "a"], [4, 6, "a"], [2, 4, HOST_CURSOR_SCOPE]])

    def test_empty_region_list(self):
        self.assertEqual(punch_host_cursor_region(None, 0), [[0, 1, HOST_CURSOR_SCOPE]])


class BuildTextTests(unittest.TestCase):
    def test_rows_are_newline_joined_without_a_trailing_newline(self):
        text, regs = build_text_and_regions([[("a", 0)], [("b", 0)]])
        self.assertEqual(text, "a\nb")
        self.assertEqual(regs, [])

    def test_scope_for_override_is_used(self):
        text, regs = build_text_and_regions(
            [[("a", 5), ("b", 5)]], scope_for=lambda _attr: "custom"
        )
        self.assertEqual(text, "ab")
        self.assertEqual(regs, [[0, 2, "custom"]])

    def test_runs_do_not_coalesce_across_rows(self):
        red = pack_attr(fg=2)
        _text, regs = build_text_and_regions([[("a", red)], [("b", red)]])
        self.assertEqual([r[:2] for r in regs], [[0, 1], [2, 3]])


class ColorHelperTests(unittest.TestCase):
    def test_malformed_hex_is_zero(self):
        for bad in (None, "", "#FFF", "abcdefg", "#GGGGGG"):
            self.assertEqual(hex_channel_sum(bad), 0)
            self.assertEqual(hex_luma(bad), 0)

    def test_xterm_hex_cube_and_greyscale(self):
        self.assertEqual(xterm_hex(1), "#FF0000")
        self.assertEqual(xterm_hex(16), "#000000")
        self.assertEqual(xterm_hex(21), "#0000FF")
        self.assertEqual(xterm_hex(255), "#EEEEEE")

    def test_out_of_range_palette_ids_use_defaults(self):
        fg, bg = scheme_colors_for(999, 999)
        self.assertEqual(bg, "#000001")
        self.assertEqual(fg, "#FFFFFF")

    def test_style_ids_and_font_style_names(self):
        self.assertEqual(style_id_for(0), 0)
        self.assertEqual(style_id_for(BOLD | ITALIC | UNDERLINE), 7)
        self.assertEqual(font_style_for(0), "")
        self.assertEqual(font_style_for(style_id_for(BOLD)), "bold")
        self.assertEqual(
            set(font_style_for(style_id_for(ITALIC | UNDERLINE)).split()),
            {"italic", "underline"},
        )

    def test_styled_scope_name_carries_the_style_id(self):
        self.assertEqual(scope_name_for(pack_attr(fg=2) | BOLD), "ai.fb.2.0.s1")

    def test_style_without_colour_has_no_region(self):
        # Default fg/bg means no ai.fb.* scope at all, style bits or not.
        self.assertIsNone(scope_name_for(BOLD))

    def test_rstrip_cells_only_drops_default_blanks(self):
        self.assertEqual(rstrip_cells([("a", 0), (" ", 0)]), [("a", 0)])
        self.assertEqual(
            rstrip_cells([("a", 0), (" ", pack_attr(bg=2))]),
            [("a", 0), (" ", pack_attr(bg=2))],
        )
        self.assertEqual(rstrip_cells([(" ", 0)]), [])


class CaretEdgeTests(unittest.TestCase):
    def test_no_prompt_row_leaves_the_caret_alone(self):
        screen = _screen_with(["plain output", "more output"])
        self.assertIsNone(find_prompt_row(screen))
        self.assertEqual(adjust_display_caret(screen, 1, 4), (1, 4))

    def test_blank_row_has_no_prompt_marker(self):
        screen = _screen_with(["", "> hi"])
        self.assertEqual(find_prompt_row(screen), 1)

    def test_marker_buried_in_text_is_not_a_prompt(self):
        screen = _screen_with(["a > b"])
        self.assertIsNone(find_prompt_row(screen))
        # Without a marker the whole row is treated as the field.
        self.assertEqual(input_start_col(screen, 0), 0)
        self.assertEqual(content_end_col(screen, 0), 5)

    def test_claude_chevron_marker(self):
        screen = _screen_with(["\u276f hi"])
        self.assertEqual(find_prompt_row(screen), 0)
        self.assertEqual(input_start_col(screen, 0), 2)

    def test_marker_at_end_of_row_has_no_input_column(self):
        screen = _screen_with([">"], cols=1)
        self.assertEqual(input_start_col(screen, 0), 1)

    def test_field_right_limit_stops_before_the_box_border(self):
        screen = _screen_with(["  \u2502 > hi   \u2502"], cols=20)
        self.assertEqual(field_right_limit(screen, 0), 10)

    def test_field_right_limit_without_a_border_is_the_row_end(self):
        screen = _screen_with(["> hi"], cols=10)
        self.assertEqual(field_right_limit(screen, 0), 9)

    def test_row_below_the_prompt_is_left_as_hardware_reported(self):
        screen = _screen_with(["> hi", "\u2570\u2500\u2500\u256f"])
        screen.y, screen.x = 1, 0
        self.assertEqual(adjust_display_caret(screen, 1, 0), (1, 0))

    def test_footer_park_without_a_remembered_column_uses_content_end(self):
        screen = _screen_with(["> hello", "", "tokens: 123"])
        screen.y, screen.x = 2, 5
        self.assertEqual(adjust_display_caret(screen, 2, 5), (0, 7))

    def test_footer_park_offsets_by_scrollback_length(self):
        screen = _screen_with(["> hello", "", "tokens: 123"])
        screen.history.append([("h", 0)])
        screen.y, screen.x = 2, 0
        cy, _cx = adjust_display_caret(screen, 2, 0)
        self.assertEqual(cy, 1)

    def test_pad_row_for_caret_reaches_the_cursor_column(self):
        rows = [[("a", 0)]]
        padded = pad_row_for_caret(rows, 0, 3)
        self.assertEqual(len(padded[0]), 4)
        self.assertEqual(rows, [[("a", 0)]])  # input untouched
        self.assertIsNotNone(cursor_text_offset(padded, 0, 3))

    def test_pad_row_for_caret_ignores_missing_rows(self):
        rows = [[("a", 0)]]
        self.assertIs(pad_row_for_caret(rows, 5, 0), rows)
        self.assertIs(pad_row_for_caret(rows, -1, 0), rows)


if __name__ == "__main__":
    unittest.main()
