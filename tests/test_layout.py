"""Column-measure hysteresis: do not chase a 1-col grow.

The 32↔33 resize loop in ai/TODO.md (2026-08-17) happens because
_LayoutWatcher forwards every stable ±1 viewport change. Growing by
exactly one column is the H-scrollbar / line-number-gutter attractor;
shrinking by one is how we lock onto the size that actually fits.

Would fail if accepted_cols started returning last+1.
"""
import unittest

from ai.terminal.layout import accepted_cols, follow_line_count, gutter_digit_delta


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


class GutterDigitDeltaTests(unittest.TestCase):
    """Cancel ST's real gutter-digit fluctuation with a fixed reservation.

    ai/TODO.md "4-digit gutter reserve": ST's native line-number gutter
    widens by a digit at every 10**n line-count boundary, so during an
    active full-history replay that crosses one, usable_w -- and
    therefore cols -- moves too, retriggering a PTY resize that restarts
    the replay at the new width, crossing the boundary again. Reserving
    at the profile's scrollback_history_size digit width instead of the
    buffer's current, moving line count cancels that.

    Would fail if gutter_digit_delta used a bare constant a bigger cap
    could still outgrow, or stopped tracking scrollback_cap entirely.
    """

    def test_same_digit_width_as_cap_needs_no_correction(self):
        self.assertEqual(gutter_digit_delta(150, 300), 0)

    def test_reserves_up_to_the_cap_digit_width(self):
        # cap=300 -> 3 digits; a 1-line buffer still gets reserved as 3.
        self.assertEqual(gutter_digit_delta(1, 300), 2)

    def test_boundary_crossing_cancels_out(self):
        # The real ST gutter widens by one digit at 999->1000; the
        # compensation must shrink by exactly one to net to zero, keeping
        # cols pinned at the same value on both sides of the crossing.
        cap = 2000
        before = gutter_digit_delta(999, cap)
        after = gutter_digit_delta(1000, cap)
        self.assertEqual(before - after, 1)

    def test_net_reservation_is_pinned_to_cap_digit_width(self):
        # actual_digits + delta must always equal the cap's own digit
        # width, for any line count -- that invariant is what stops the
        # oscillation, not any single delta value in isolation.
        cap = 2000
        for total_lines in (1, 9, 10, 99, 100, 999, 1000, 1999, 2000):
            delta = gutter_digit_delta(total_lines, cap)
            actual_digits = len(str(total_lines))
            self.assertEqual(actual_digits + delta, len(str(cap)))

    def test_scrollback_cap_of_zero_is_treated_as_one_digit(self):
        self.assertEqual(gutter_digit_delta(5, 0), 0)


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
