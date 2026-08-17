"""Tests for tests/mock_agent_cli.py replay-frame bytes."""
import unittest

from tests.mock_agent_cli import encode_replay_frame, make_turn_lines


class EncodeReplayFrameTests(unittest.TestCase):
    def test_frame_is_2026_home_dump_not_erase(self):
        lines = make_turn_lines(0, body_lines=3) + make_turn_lines(1, body_lines=3)
        frame = encode_replay_frame(lines)
        self.assertTrue(frame.startswith("\x1b[?2026h"))
        self.assertIn("\x1b[H", frame)
        self.assertTrue(frame.endswith("\x1b[?2026l"))
        self.assertNotIn("\x1b[2J", frame)
        self.assertEqual(frame.count("TURN-00"), 4)
        self.assertEqual(frame.count("TURN-01"), 4)
