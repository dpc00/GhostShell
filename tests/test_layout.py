"""Column-measure hysteresis: do not chase a 1-col grow.

The 32↔33 resize loop in ai/TODO.md (2026-08-17) happens because
_LayoutWatcher forwards every stable ±1 viewport change. Growing by
exactly one column is the H-scrollbar / line-number-gutter attractor;
shrinking by one is how we lock onto the size that actually fits.

Would fail if accepted_cols started returning last+1.
"""
import unittest

from ai.terminal.layout import accepted_cols, follow_line_count


class AcceptedColsTests(unittest.TestCase):
    def test_first_measurement_is_used(self):
        self.assertEqual(accepted_cols(None, 80), 80)

    def test_unchanged_measurement_stays(self):
        self.assertEqual(accepted_cols(32, 32), 32)

    def test_does_not_grow_by_one_column(self):
        # 32→33 is the live-cast oscillation. Stay on the applied size.
        self.assertEqual(accepted_cols(32, 33), 32)

    def test_shrink_by_one_column_is_applied(self):
        # 33→32 fits; applying it is what kills the H-scrollbar / gutter chase.
        self.assertEqual(accepted_cols(33, 32), 32)

    def test_grow_by_two_or_more_is_a_real_resize(self):
        self.assertEqual(accepted_cols(32, 34), 34)
        self.assertEqual(accepted_cols(32, 55), 55)

    def test_shrink_by_two_or_more_is_a_real_resize(self):
        self.assertEqual(accepted_cols(55, 32), 32)

    def test_32_33_attractor_locks_to_the_narrower_size(self):
        cols = 33
        for measured in (32, 33, 32, 33, 32, 33):
            cols = accepted_cols(cols, measured)
        self.assertEqual(cols, 32)


class FollowLineCountTests(unittest.TestCase):
    """Snap-to-bottom drops a configured number of trailing lines.

    The engine does not inspect line contents. A profile sets
    follow_ignore_trailing_lines; default 0 leaves every TUI alone.
    Would fail if follow_line_count still sniffed chrome text.
    """

    def test_default_keeps_the_full_count(self):
        self.assertEqual(follow_line_count(6), 6)
        self.assertEqual(follow_line_count(6, 0), 6)

    def test_drops_configured_trailing_lines(self):
        self.assertEqual(follow_line_count(6, 2), 4)
        self.assertEqual(follow_line_count(6, 1), 5)

    def test_does_not_go_below_zero(self):
        self.assertEqual(follow_line_count(1, 4), 0)
        self.assertEqual(follow_line_count(0, 2), 0)

    def test_negative_ignore_is_treated_as_zero(self):
        self.assertEqual(follow_line_count(6, -2), 6)
