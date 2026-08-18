"""Trailing-row trim used by _do_render.

A blank hardware cursor parked below the footer (Claude CUP-to-last-row
+ overflow \\n) must not keep those empty lines. An empty prompt on the
line immediately after content must stay (shell insertion point).

Would fail if trim_display_rows went back to last_real = cy.
"""
import unittest

from ai.terminal.render import trim_display_rows


def _rows(*lines):
    return [[(ch, 0) for ch in line] for line in lines]


class TrimDisplayRowsTests(unittest.TestCase):
    def test_blank_cursor_below_content_is_dropped(self):
        rows = _rows("❯ prompt", "footer", "     ", "     ")
        trimmed = trim_display_rows(rows, cy=3)
        self.assertEqual(len(trimmed), 2)
        self.assertEqual("".join(ch for ch, _ in trimmed[-1]).rstrip(), "footer")

    def test_empty_prompt_on_next_line_is_kept(self):
        rows = _rows("output", "     ")
        trimmed = trim_display_rows(rows, cy=1)
        self.assertEqual(len(trimmed), 2)

    def test_content_below_cursor_is_kept(self):
        rows = _rows("❯ prompt", "footer")
        trimmed = trim_display_rows(rows, cy=0)
        self.assertEqual(len(trimmed), 2)

    def test_empty_screen_keeps_the_cursor_row(self):
        rows = _rows("     ", "     ")
        trimmed = trim_display_rows(rows, cy=0)
        self.assertEqual(len(trimmed), 1)
