"""Regression tests for the layout_extent() vs text_to_layout() height bug.

_scroll_to_bottom was fixed (2026-09-06/07) to derive real content height from
view.text_to_layout(view.size()) + line_height(), because view.layout_extent()
is inflated by exactly one line_height() on a view with scroll_past_end
enabled (the default here). _real_content_height -- used by the render loop's
content_fits/near_bottom checks and by _pin_terminal_viewport's near-fit
check -- still used the inflated view.layout_extent() measurement, so those
checks disagreed with _scroll_to_bottom's own follow target by exactly one
line right at the fit boundary. Reported live as the viewport jiggling up a
line on some keystrokes: one frame's content_fits/near_bottom reads the
inflated height and picks the pin-to-rest/hold-still branch, the next frame's
_scroll_to_bottom call (still correct) computes a target one line lower, and
the two branches fight every time a keystroke lands right on that boundary.

_real_content_height was changed to derive from the same
text_to_layout(view.size()) + line_height() measurement, so it can never
diverge from _scroll_to_bottom's target again. These tests hard-code a fake
view where layout_extent() and text_to_layout() disagree by one line_height
(mirroring the real scroll_past_end inflation) so a regression back to
layout_extent() fails them immediately.
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.sublime_stub import install as _install_stubs  # noqa: E402

_install_stubs()

import ai_terminal  # noqa: E402


class _FakeView:
    """Models a scroll_past_end view: layout_extent() reports one extra
    line_height() beyond the true bottom edge of the last content line, the
    same inflation documented live for Sublime's scroll_past_end setting."""

    def __init__(self, lines, lh=20.0, ve=(800.0, 200.0), vp=(0.0, 0.0)):
        self.lines = lines
        self._lh = lh
        self._ve = ve
        self._vp = vp
        self.vp_writes = []

    def id(self):
        return 1

    def line_height(self):
        return self._lh

    def viewport_extent(self):
        return self._ve

    def viewport_position(self):
        return self._vp

    def set_viewport_position(self, pos, animate=False):
        self.vp_writes.append((tuple(pos), animate))
        self._vp = tuple(pos)

    def size(self):
        return self.lines * 10

    def text_to_layout(self, pt):
        # Top of the row containing the last character.
        return (0.0, (self.lines - 1) * self._lh)

    def layout_extent(self):
        # scroll_past_end padding: one full line_height past the true
        # bottom edge (self.lines * self._lh).
        return (self._ve[0], self.lines * self._lh + self._lh)

    def is_valid(self):
        return True

    def settings(self):
        return self

    def set(self, k, v):
        pass


class RealContentHeightTests(unittest.TestCase):
    def test_matches_text_to_layout_not_inflated_layout_extent(self):
        view = _FakeView(lines=10, lh=20.0)
        true_bottom = 10 * 20.0  # what _scroll_to_bottom targets
        inflated = view.layout_extent()[1]  # what the old bug measured
        self.assertNotEqual(true_bottom, inflated)  # sanity: fixture models a real gap

        self.assertEqual(ai_terminal._real_content_height(view), true_bottom)

    def test_content_fits_reads_true_when_content_exactly_fills_viewport(self):
        # Content's true bottom edge lands exactly on the viewport height --
        # the boundary case where the old inflated measurement flipped
        # content_fits to False one line early.
        view = _FakeView(lines=10, lh=20.0, ve=(800.0, 200.0))
        real_h = ai_terminal._real_content_height(view)
        content_fits = real_h <= view.viewport_extent()[1] + 0.5
        self.assertTrue(content_fits)


class ScrollToBottomAgreementTests(unittest.TestCase):
    def test_target_matches_real_content_height(self):
        view = _FakeView(lines=10, lh=20.0, ve=(800.0, 150.0), vp=(0.0, 999.0))
        ai_terminal._scroll_to_bottom(view)

        true_bottom = 10 * 20.0
        expected_target = true_bottom - view.viewport_extent()[1]
        self.assertEqual(view.vp_writes[-1][0], (0.0, expected_target))
        # And it must agree with the shared helper other call sites use.
        self.assertEqual(ai_terminal._real_content_height(view), true_bottom)

    def test_noop_within_one_pixel_deadband(self):
        true_bottom = 10 * 20.0
        target = true_bottom - 150.0
        view = _FakeView(lines=10, lh=20.0, ve=(800.0, 150.0), vp=(0.0, target + 0.5))
        ai_terminal._scroll_to_bottom(view)
        self.assertEqual(view.vp_writes, [])


class PageScrollTests(unittest.TestCase):
    """_page_scroll: PageUp/PageDown moving the viewport directly.

    Previously PageUp/PageDown paged via ST's native "move" command ("by":
    "pages"), which moves relative to the CURRENT CARET -- but the render
    loop unconditionally re-pins the caret to the live PTY cursor row (the
    bottom of the buffer) every frame, regardless of where the user has
    scrolled the viewport to. Reported live: mouse wheel up (scrolling well
    into scrollback, more than one page), then PageUp, moved the view DOWN
    instead of up -- because "page up from the bottom-pinned caret" can
    still land below wherever the mouse wheel had already scrolled to, and
    Sublime auto-scrolls to reveal that caret.

    _page_scroll computes the new viewport position directly from the
    CURRENT VIEWPORT position, never from a caret -- these tests don't even
    give the fake view a caret/selection concept, which is the point: the
    result cannot depend on one.
    """

    def _view(self, vp_y, lines=200, lh=20.0, ve=(800.0, 300.0)):
        return _FakeView(lines=lines, lh=lh, ve=ve, vp=(0.0, vp_y))

    def test_page_up_moves_viewport_up_after_scrolling_past_one_page(self):
        # Scrolled well up into scrollback (vp_y=1000) -- far more than one
        # page (page height = ve[1]-lh = 280) below the very top. The old
        # caret-relative bug would have jumped this DOWN toward the
        # bottom-pinned caret; this must move further UP instead.
        view = self._view(vp_y=1000.0)
        ai_terminal._page_scroll(view, None, forward=False)

        new_y = view.vp_writes[-1][0][1]
        self.assertLess(new_y, 1000.0)
        self.assertEqual(new_y, 1000.0 - (300.0 - 20.0))

    def test_page_down_moves_viewport_down(self):
        view = self._view(vp_y=1000.0)
        ai_terminal._page_scroll(view, None, forward=True)

        new_y = view.vp_writes[-1][0][1]
        self.assertGreater(new_y, 1000.0)
        self.assertEqual(new_y, 1000.0 + (300.0 - 20.0))

    def test_page_up_clamps_at_top(self):
        view = self._view(vp_y=100.0)
        ai_terminal._page_scroll(view, None, forward=False)
        self.assertEqual(view.vp_writes[-1][0][1], 0.0)

    def test_page_down_clamps_at_bottom(self):
        # true bottom = 200*20 = 4000; max scroll = 4000 - 300 = 3700
        view = self._view(vp_y=3690.0)
        ai_terminal._page_scroll(view, None, forward=True)
        self.assertEqual(view.vp_writes[-1][0][1], 3700.0)


class _FakePty:
    def is_alive(self):
        return True


class _FakeScreen:
    def __init__(self, alt_screen=False, mouse_tracking=False):
        self.alt_screen = alt_screen
        self.mouse_tracking = mouse_tracking


class _FakeTerm:
    def __init__(self, auto_follow, alt_screen=False, mouse_tracking=False):
        self._auto_follow = auto_follow
        self.screen = _FakeScreen(alt_screen, mouse_tracking)
        self.pty = _FakePty()
        self.profile_name = None
        self._last_vp_y = None
        self._live_anchor_y = None


class HeightChangeResyncTests(unittest.TestCase):
    """_resync_viewport_after_height_change: any change to a view's
    viewport_extent() height -- a panel opening/closing, a window resize, a
    sidebar/minimap toggle, a tab-group sash drag -- with no PTY resize and
    no keystroke/PTY-output event, so the render loop's usual follow/pin
    recompute never runs on its own. Reported live: opening the Sublime
    Python console hid the last several lines of an ai_terminal tab that
    was following the tail, because the old viewport y stayed put against
    the new, shorter viewport height. _clamp_vp_loop detects the height
    change itself (generically, not via an enumerated list of commands) and
    calls this to fix it up -- these tests exercise the fix-up function
    directly.
    """

    def test_following_view_rescrolls_to_reveal_the_tail_under_new_height(self):
        # Scrolled exactly to the bottom at the OLD (taller) viewport height.
        true_bottom = 20 * 20.0  # 400
        view = _FakeView(lines=20, lh=20.0, ve=(800.0, 300.0), vp=(0.0, true_bottom - 300.0))
        term = _FakeTerm(auto_follow=True)

        # Panel opens: viewport shrinks, viewport position is untouched by
        # Sublime itself (this is exactly the bug -- nothing moves it).
        view._ve = (800.0, 150.0)

        ai_terminal._resync_viewport_after_height_change(view, term)

        new_y = view.vp_writes[-1][0][1]
        self.assertEqual(new_y, true_bottom - 150.0)
        # The new range [new_y, new_y+150] must reach true_bottom -- the
        # tail is visible again, not still hidden below the shrunk viewport.
        self.assertEqual(new_y + 150.0, true_bottom)

    def test_scrolled_back_reading_history_is_left_alone(self):
        # User deliberately scrolled away (not following) -- a panel
        # toggling must never yank their read position back to the tail.
        view = _FakeView(lines=20, lh=20.0, ve=(800.0, 300.0), vp=(0.0, 20.0))
        term = _FakeTerm(auto_follow=False, alt_screen=False, mouse_tracking=False)

        view._ve = (800.0, 150.0)
        ai_terminal._resync_viewport_after_height_change(view, term)

        self.assertEqual(view.vp_writes, [])

    def test_tui_like_view_is_repinned_to_rest(self):
        view = _FakeView(lines=20, lh=20.0, ve=(800.0, 300.0), vp=(0.0, 40.0))
        term = _FakeTerm(auto_follow=False, alt_screen=True)  # alt-screen -> tui_like

        ai_terminal._resync_viewport_after_height_change(view, term)

        self.assertEqual(view.vp_writes[-1][0], (0.0, 0.0))

    def test_dead_pty_is_a_noop(self):
        view = _FakeView(lines=20, lh=20.0, ve=(800.0, 300.0), vp=(0.0, 100.0))
        term = _FakeTerm(auto_follow=True)
        term.pty = type("DeadPty", (), {"is_alive": lambda self: False})()

        ai_terminal._resync_viewport_after_height_change(view, term)

        self.assertEqual(view.vp_writes, [])


class ClampLoopHeightDetectionTests(unittest.TestCase):
    """_clamp_vp_loop itself must notice a viewport_extent() height change
    and call _resync_viewport_after_height_change -- not just that the
    fix-up function works when called directly. This is the generic
    detector: it doesn't matter WHY the height changed (panel, window
    resize, sidebar toggle, sash drag, ...), only that it did.
    """

    def test_single_tick_height_blip_is_ignored(self):
        # A one-tick difference that reverts on the very next tick (the
        # exact shape of transient layout jitter during active typing/
        # streaming, not a real panel/layout change) must never trigger a
        # viewport write -- that was the cause of live-reported jiggling
        # and slow key response while typing.
        view = _FakeView(lines=20, lh=20.0, ve=(800.0, 300.0), vp=(0.0, 100.0))
        term = _FakeTerm(auto_follow=True)
        term.view = view

        registry = ai_terminal._term_registry()
        key = object()
        registry[key] = term
        try:
            ai_terminal._clamp_vp_loop()  # baseline: last_ve_h = 300.0
            view._ve = (800.0, 150.0)
            ai_terminal._clamp_vp_loop()  # candidate 150.0, count 1 -- not acted on yet
            view._ve = (800.0, 300.0)
            ai_terminal._clamp_vp_loop()  # back to confirmed height -> candidate dropped
        finally:
            registry.pop(key, None)

        self.assertEqual(view.vp_writes, [])

    def test_height_shrink_confirmed_on_two_ticks_triggers_resync(self):
        true_bottom = 20 * 20.0  # 400
        view = _FakeView(lines=20, lh=20.0, ve=(800.0, 300.0), vp=(0.0, true_bottom - 300.0))
        term = _FakeTerm(auto_follow=True)
        term.view = view

        registry = ai_terminal._term_registry()
        key = object()
        registry[key] = term
        try:
            # First tick: establishes the baseline height, no change yet ->
            # no resync write.
            ai_terminal._clamp_vp_loop()
            self.assertEqual(view.vp_writes, [])

            # Panel opens between ticks: viewport shrinks, nothing else
            # touches the viewport position (that's the bug being guarded
            # against). Requires the SAME new height on 2 consecutive ticks
            # before it's treated as real, not transient noise.
            view._ve = (800.0, 150.0)
            ai_terminal._clamp_vp_loop()
            self.assertEqual(view.vp_writes, [])  # not yet confirmed
            ai_terminal._clamp_vp_loop()
        finally:
            registry.pop(key, None)

        self.assertEqual(len(view.vp_writes), 1)
        new_y = view.vp_writes[-1][0][1]
        self.assertEqual(new_y, true_bottom - 150.0)


class NearFitDipVsDeliberateScrollTests(unittest.TestCase):
    """_clamp_vp_loop's near_fit / tall-scrollback branches exist to kill a
    specific glitch: ST's view.show() briefly parking vp[1] BELOW rest (a
    negative overshoot, e.g. -20) when content already fits the viewport --
    never a deliberate user scroll, since there is nothing below rest to
    scroll into when content fits. The old `abs(dy_rest) >= 0.5` check fired
    on drift in EITHER direction, so a user scrolling the other way -- past
    rest, toward the tail, e.g. pushing a short conversation's permission
    prompt up to read it in full -- got silently reverted on every 8ms tick
    regardless of typing. Only the negative direction should be corrected.
    """

    def _register(self, view, term):
        registry = ai_terminal._term_registry()
        key = object()
        registry[key] = term
        return registry, key

    def test_deliberate_forward_scroll_within_near_fit_content_is_preserved(self):
        # true bottom = 10*20 = 200; ve height 200 -> near_fit (within 2*lh).
        view = _FakeView(lines=10, lh=20.0, ve=(800.0, 200.0), vp=(0.0, 30.0))
        term = _FakeTerm(auto_follow=True)
        term.view = view

        registry, key = self._register(view, term)
        try:
            ai_terminal._clamp_vp_loop()
        finally:
            registry.pop(key, None)

        self.assertEqual(view.vp_writes, [])

    def test_negative_overshoot_dip_is_still_corrected(self):
        view = _FakeView(lines=10, lh=20.0, ve=(800.0, 200.0), vp=(0.0, -20.0))
        term = _FakeTerm(auto_follow=True)
        term.view = view

        registry, key = self._register(view, term)
        try:
            ai_terminal._clamp_vp_loop()
        finally:
            registry.pop(key, None)

        self.assertEqual(view.vp_writes, [((0.0, 0.0), False)])


if __name__ == "__main__":
    unittest.main()
