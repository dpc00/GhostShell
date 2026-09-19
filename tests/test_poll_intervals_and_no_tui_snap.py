"""Poll intervals and no hard-pin / pan→PTY on TUI clamp path.

Host should not burn ms-scale loops or snap the Sublime tab away from the
user. Keypad/touchpad tab motion is owned by Sublime unless a key event
explicitly routes to the PTY (combinational gates on the event path).
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.sublime_stub import install as _install_stubs  # noqa: E402

_install_stubs()

import ai_terminal  # noqa: E402
from tests.test_scroll_to_bottom import _FakeTerm, _FakeView  # noqa: E402


class PollIntervalTests(unittest.TestCase):
    def test_timers_are_half_second_or_slower(self):
        self.assertGreaterEqual(ai_terminal._CLAMP_POLL_MS, 500)
        self.assertGreaterEqual(ai_terminal._HOVER_POLL_MS, 500)
        self.assertGreaterEqual(ai_terminal._LayoutWatcher._POLL_MS, 1000)
        self.assertGreaterEqual(ai_terminal._RENDER_MS, 100)


class TuiClampNoSnapTests(unittest.TestCase):
    def _register(self, view, term):
        registry = ai_terminal._term_registry()
        key = object()
        registry[key] = term
        return registry, key

    def test_alt_screen_forward_scroll_is_not_snapped_back(self):
        # alt_screen ⇒ _tui_like. Old clamp hard-pinned every 8ms.
        view = _FakeView(lines=40, lh=20.0, ve=(800.0, 200.0), vp=(0.0, 80.0))
        term = _FakeTerm(auto_follow=False, alt_screen=True)
        term.view = view
        term.pty = type("P", (), {"is_alive": lambda self: True})()
        term.profile_name = "Vibe"

        registry, key = self._register(view, term)
        try:
            # Avoid height-change resync path acting on first ticks.
            term._last_ve_h = 200.0
            ai_terminal._clamp_vp_loop()
        finally:
            registry.pop(key, None)

        self.assertEqual(view.vp_writes, [])
        self.assertEqual(view.viewport_position()[1], 80.0)

    def test_alt_screen_negative_dip_still_corrected(self):
        view = _FakeView(lines=40, lh=20.0, ve=(800.0, 200.0), vp=(0.0, -20.0))
        term = _FakeTerm(auto_follow=False, alt_screen=True)
        term.view = view
        term.pty = type("P", (), {"is_alive": lambda self: True})()
        term.profile_name = "Vibe"
        term._last_ve_h = 200.0

        registry, key = self._register(view, term)
        try:
            ai_terminal._clamp_vp_loop()
        finally:
            registry.pop(key, None)

        self.assertEqual(view.vp_writes, [((0.0, 0.0), False)])

    def test_settle_tui_does_not_hard_pin_forward_scroll(self):
        view = _FakeView(lines=40, lh=20.0, ve=(800.0, 200.0), vp=(0.0, 60.0))
        term = _FakeTerm(auto_follow=False, alt_screen=True)
        ai_terminal._settle_viewport(
            view, term, rest=0.0, tui_owns_scroll=True,
            do_follow=False, content_fits=False,
        )
        self.assertEqual(view.vp_writes, [])
        self.assertEqual(term._last_vp_y, 60.0)


if __name__ == "__main__":
    unittest.main()
