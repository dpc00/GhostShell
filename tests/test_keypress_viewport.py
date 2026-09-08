"""Printable-key viewport behavior while following live terminal output."""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.sublime_stub import install as _install_stubs  # noqa: E402

_install_stubs()

import ai_terminal  # noqa: E402


class PrintableKeyViewportTests(unittest.TestCase):
    def test_already_following_does_not_force_pre_echo_scroll(self):
        source = open(ai_terminal.__file__, encoding="utf-8").read()
        start = source.index('elif kl not in _NO_SCROLL_KEYS:')
        end = source.index('            term.send_string(code)', start)
        branch = source[start:end]

        self.assertIn('was_following = bool(getattr(term, "_auto_follow", False))', branch)
        self.assertIn('elif not was_following:', branch)
        self.assertNotIn('                else:\n                    _scroll_to_bottom', branch)


class NearBottomAutoFollowTests(unittest.TestCase):
    """The render loop must never re-engage auto_follow just because the
    current (unmoved) scroll position happens to be within near_bottom's
    zone of the true content bottom.

    This loop runs on every redraw, including an idle spinner/timer redraw
    that changes no content and that the user did nothing to trigger (e.g.
    Claude Code's CLI keeps redrawing its footer roughly every half-second
    even while just sitting at a permission prompt). Re-checking
    near_bottom unconditionally on every one of those redraws meant landing
    inside a ~2-line-tall zone near the true bottom -- while deliberately
    scrolling up to review a permission prompt in full -- got pulled the
    rest of the way down by the very next idle redraw, with no further
    action from the user. Reported live and confirmed explicitly unwanted:
    resting anywhere the user chooses must never auto-resume on its own;
    only an explicit action (typing) may re-engage follow. Confirmed at the
    source level, not via a full render-loop harness -- AiTerminalRenderCommand
    ._run is too large/stateful to instantiate meaningfully in this test
    suite; see the "still some jiggling"/panel-toggle tests in
    test_scroll_to_bottom.py for the parts of this session's fixes that
    could be isolated into pure functions instead.
    """

    def test_near_bottom_no_longer_force_engages_auto_follow(self):
        source = open(ai_terminal.__file__, encoding="utf-8").read()
        start = source.index("near_bottom = (vp[1] + ve[1])")
        end = source.index("do_follow = (", start)
        block = source[start:end]

        self.assertIn(
            'if vp[1] < term._live_anchor_y - lh * 1.5:', block,
            "disengage-on-scroll-away must still run",
        )
        self.assertIn('_set_auto_follow(term, False)', block)
        self.assertNotIn(
            '_set_auto_follow(term, True)', block,
            "near_bottom must not, by itself, re-engage auto_follow -- "
            "that penalizes a user scrolling up to inspect content by "
            "snapping them back on the next idle redraw",
        )


if __name__ == "__main__":
    unittest.main()
