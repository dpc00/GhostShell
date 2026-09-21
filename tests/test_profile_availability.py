import os
import tempfile
import unittest

from terminal.profile_availability import (
    command_exists,
    menu_caption,
    profile_is_available,
)


class ProfileAvailabilityTests(unittest.TestCase):
    def test_existing_absolute_command_enables_any_configured_profile(self):
        profile = {"launch_command": [os.path.abspath(__file__)]}
        self.assertTrue(profile_is_available("Kimi", profile))

    def test_missing_command_disables_profile(self):
        profile = {"launch_command": ["definitely-not-an-installed-command-xyz"]}
        self.assertFalse(profile_is_available("Missing", profile, path=""))

    def test_relative_path_command_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = os.path.join(tmp, "agent.exe")
            with open(existing, "wb"):
                pass
            self.assertTrue(command_exists([existing]))
            self.assertFalse(command_exists([os.path.join(tmp, "absent.exe")]))

    def test_menu_caption_plain_when_nothing_observed(self):
        self.assertEqual(menu_caption("Claude"), "Claude")

    def test_menu_caption_missing_executable(self):
        self.assertEqual(
            menu_caption("Vibe", executable_ok=False),
            "Vibe — not installed",
        )


if __name__ == "__main__":
    unittest.main()
