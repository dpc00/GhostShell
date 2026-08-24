"""Viewport compensation for history eviction.

_compensate_trim_scroll shifts vp.y by evicted*line_height regardless of
auto_follow. A 2026-08-23 same-day attempt to skip the shift while
auto_follow was True (to stop a last-row-overflow overshoot) was reverted:
skipping it also skipped the ordinary case (any eviction while actively
following, i.e. most of the time once history is at cap), so the routine
one-line text-slide this function exists to cancel went uncorrected
continuously -- reported live as bigger/more frequent jumps than before.
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.sublime_stub import install as _install_stubs  # noqa: E402

_install_stubs()

from ai import ai_terminal  # noqa: E402
from ai.terminal.screen import Screen  # noqa: E402


class _FakeView:
    def __init__(self, lh=17.0, vp=(0.0, 4172.0)):
        self._lh = lh
        self._vp = vp
        self.vp_writes = []

    def line_height(self):
        return self._lh

    def set_viewport_position(self, pos, animate=True):
        self.vp_writes.append((tuple(pos), animate))
        self._vp = tuple(pos)

    def viewport_position(self):
        return self._vp


class _FakeTerm:
    def __init__(self, screen, auto_follow=True):
        self.screen = screen
        self._auto_follow = auto_follow
        self._last_retired_total = None
        self._last_history_len = None
        self._last_vp_y = 0.0


def _capped_screen(cap=3, filled=3):
    screen = Screen(8, 4, history_cap=cap)
    for i in range(filled):
        screen._retire_line([(ch, 0) for ch in ("L%d" % i)])
    return screen


class CompensateTrimScrollTests(unittest.TestCase):
    def test_following_the_prompt_still_shifts_viewport_by_evicted_line_heights(self):
        screen = _capped_screen()
        term = _FakeTerm(screen, auto_follow=True)
        view = _FakeView(lh=17.0)
        vp = (0.0, 4172.0)

        ai_terminal._compensate_trim_scroll(view, term, vp)
        screen._retire_line([(ch, 0) for ch in "NEW1"])
        screen._retire_line([(ch, 0) for ch in "NEW2"])
        out = ai_terminal._compensate_trim_scroll(view, term, vp)

        # 2 evictions * 17px. Hand-derived from lh, not from the helper.
        self.assertEqual(out, (0.0, 4138.0))
        self.assertEqual(view.vp_writes, [((0.0, 4138.0), False)])
        self.assertEqual(term._last_vp_y, 4138.0)

    def test_scrolled_back_still_shifts_viewport_by_evicted_line_heights(self):
        screen = _capped_screen()
        term = _FakeTerm(screen, auto_follow=False)
        view = _FakeView(lh=17.0)
        vp = (0.0, 4172.0)

        ai_terminal._compensate_trim_scroll(view, term, vp)
        screen._retire_line([(ch, 0) for ch in "NEW1"])
        screen._retire_line([(ch, 0) for ch in "NEW2"])
        out = ai_terminal._compensate_trim_scroll(view, term, vp)

        # 2 evictions * 17px. Hand-derived from lh, not from the helper.
        self.assertEqual(out, (0.0, 4138.0))
        self.assertEqual(view.vp_writes, [((0.0, 4138.0), False)])
        self.assertEqual(term._last_vp_y, 4138.0)

    def test_no_eviction_is_a_noop(self):
        screen = _capped_screen()
        term = _FakeTerm(screen, auto_follow=True)
        view = _FakeView()
        vp = (0.0, 4172.0)

        ai_terminal._compensate_trim_scroll(view, term, vp)
        out = ai_terminal._compensate_trim_scroll(view, term, vp)

        self.assertEqual(out, vp)
        self.assertEqual(view.vp_writes, [])
