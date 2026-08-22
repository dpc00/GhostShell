"""Isolated, deterministic repro attempt for the character-splatter bug
using the exact Codex-style full-transcript-replay pattern that produced a
real, confirmed splice live (ai/TODO.md, "SUPERSEDED by a real splatter
repro" section, 2026-08-21): tests/mock_agent_cli.py's ReplayAgent re-sends
the ENTIRE transcript (CSI ?2026h, CSI H, every turn, CSI ?2026l) on every
turn added. That live session found history[30] corrupted:

    30 '› user prompt TURN-01ent reply line 7011'

-- an already-retired line's tail overwritten with a fragment of a much
later turn's text. retired_total (2) vs. total history length (216) proved
almost every line came through _sync_scrollback's FULL REBUILD path
(history.clear() + rebuild every row via terminal_grid_ref), not the
incremental diff path -- ruling out a Python-level list-aliasing bug in
either retirement path (both build a fresh list per row, audited directly).
That leaves the native terminal_grid_ref/_cell_from_grid_ref call sequence
during a large rebuild as the remaining suspect.

This test replicates the exact byte pattern (not a live PTY -- feeds
GhosttyParser directly, single-threaded, no concurrency) and checks every
retired history line against its known source text. If this reproduces
the splice, the bug is confirmed inside the native call sequence or
libghostty-vt itself, not this plugin's Python code.

Run from repo root, as its own process:
    python -m pytest tests/test_splatter_replay_rebuild.py -v -s
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.terminal.ghostty_engine import GhosttyParser
from ai.terminal.screen import Screen

DLL_PATH = os.path.join(
    os.path.dirname(__file__), "..", "ai", "terminal", "bin", "ghostty-vt.dll"
)

_LINES_PER_TURN = 28


def make_turn_lines(n, body_lines=_LINES_PER_TURN):
    tag = "TURN-%02d" % n
    lines = ["› user prompt %s" % tag]
    for i in range(body_lines):
        lines.append("• %s L%02d mock agent reply line %d" % (tag, i, n * 1000 + i))
    return lines


def encode_replay_frame(lines):
    body = "\r\n".join(lines) + "\r\n"
    return "\x1b[?2026h\x1b[?25l\x1b[H" + body + "\x1b[?2026l"


@unittest.skipUnless(os.path.exists(DLL_PATH), "ghostty-vt.dll not present")
class SplatterReplayRebuildTests(unittest.TestCase):
    def test_growing_full_transcript_replay_does_not_corrupt_history(self):
        screen = Screen(80, 24, history_cap=2000)
        parser = GhosttyParser(screen, force_main_screen=True, dll_path=DLL_PATH)

        turns = []
        for n in range(3):
            turns.extend(make_turn_lines(n))
        parser.feed(encode_replay_frame(turns))

        # Match the live repro: one batch of 5 additional turns (TURN-03..07),
        # each re-sending the WHOLE growing transcript, exactly like
        # ReplayAgent._add_turn -> self._paint() in tests/mock_agent_cli.py.
        for n in range(3, 8):
            turns.extend(make_turn_lines(n))
            parser.feed(encode_replay_frame(turns))

        # Expected: every line ever in `turns` should eventually appear,
        # verbatim, exactly once, somewhere in screen.grid + screen.history
        # (rows still on-screen at the end were never retired -- both must
        # be checked; grid rows are checked as substrings since blank
        # padding cells surround real content).
        hist_lines = ["".join(ch for ch, _a in row) for row in screen.history]
        grid_lines = ["".join(row).rstrip() for row in screen.grid]
        hist_and_grid = set(hist_lines) | set(grid_lines)

        mismatches = [expected for expected in turns if expected not in hist_and_grid]

        # Also scan for the specific corruption signature: a retired history
        # line containing text from MORE THAN ONE turn's tag.
        import re
        tag_re = re.compile(r"TURN-(\d\d)")
        spliced = []
        for h in hist_lines:
            tags = set(tag_re.findall(h))
            if len(tags) > 1:
                spliced.append((h, tags))

        print("\n[splatter-replay] turns fed=%d total expected lines=%d "
              "history lines=%d grid lines=%d missing=%d spliced=%d"
              % (8, len(turns), len(hist_lines), len(grid_lines),
                 len(mismatches), len(spliced)))
        if spliced:
            for h, tags in spliced[:10]:
                print("[splatter-replay] SPLICED LINE:", repr(h), "tags:", tags)
        if mismatches:
            for m in mismatches[:10]:
                print("[splatter-replay] MISSING:", repr(m))

        self.assertEqual(spliced, [], "a retired history line contains text from more than one turn -- splatter reproduced")
        self.assertEqual(mismatches, [], "some fed lines never appear anywhere in grid+history -- content loss, not just splatter")


if __name__ == "__main__":
    unittest.main()
