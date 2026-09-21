"""The shipped defaults must not subject a user to logging or recording.

Owner's rule (AGENTS.md rule 14): anything that records or logs is OFF in the repo's `ai_terminal.sublime-settings`. The owner turns these on
for his own installation in his User settings, never in the shipped defaults.
"""
import os
import re
import unittest

_SETTINGS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_terminal.sublime-settings"
)

# Top-level keys that must default to false in a release.
_MUST_DEFAULT_OFF = ("log_tab_text", "record_asciicast")


def _top_level_value(text, key):
    """Return the raw value of the first uncommented `"key": value,` line, or None."""
    pattern = re.compile(r'^\s{0,4}"%s"\s*:\s*([^,\s/]+)' % re.escape(key), re.MULTILINE)
    match = pattern.search(text)
    return match.group(1) if match else None


class ReleaseDefaultsTests(unittest.TestCase):
    def test_privacy_sensitive_settings_default_off(self):
        with open(_SETTINGS, encoding="utf-8") as handle:
            text = handle.read()
        for key in _MUST_DEFAULT_OFF:
            with self.subTest(setting=key):
                self.assertEqual(_top_level_value(text, key), "false")


if __name__ == "__main__":
    unittest.main()
