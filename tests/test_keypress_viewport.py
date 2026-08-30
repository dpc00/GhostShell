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


if __name__ == "__main__":
    unittest.main()
