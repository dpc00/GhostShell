"""Isolated concurrency stress test for the character-splatter bug
(ai/TODO.md, "Character-splatter bug — NOT RESOLVED").

MUST run as a separate `python` process (subprocess), never injected into
the live Sublime plugin_host via eval_python -- doing that once already
leaked 39 threads into the real session and plausibly triggered the
symptom via self-inflicted resource contention instead of isolating the
real cause. This file replicates the actual production locking pattern
(ai/ai_terminal.py's _on_data / AiTerminalRenderCommand._run) faithfully:
one writer thread feeding text under a lock, one reader thread calling
render_cells() under the SAME lock, exactly as the live code does -- so a
failure here means the bug reproduces under correct locking discipline,
not despite a bug in this test's own synchronization.

Run from repo root, as its own process, with a hard wall-clock timeout
(the prior in-process attempt is suspected to hang under sustained load):
    python -m pytest tests/test_splatter_stress.py -v --timeout=60
or, without pytest-timeout installed:
    python tests/test_splatter_stress.py
(the __main__ block below enforces its own timeout via a watchdog thread
and os._exit, since this must never hang the calling process either).
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.terminal.ghostty_engine import GhosttyParser
from ai.terminal.screen import Screen

DLL_PATH = os.path.join(
    os.path.dirname(__file__), "..", "ai", "terminal", "bin", "ghostty-vt.dll"
)


def _make_parser(cols=80, rows=24):
    screen = Screen(cols, rows, history_cap=300)
    parser = GhosttyParser(screen, force_main_screen=True, dll_path=DLL_PATH)
    return screen, parser


@unittest.skipUnless(os.path.exists(DLL_PATH), "ghostty-vt.dll not present")
class SplatterStressTests(unittest.TestCase):
    def test_concurrent_feed_and_render_under_shared_lock(self):
        """Writer feeds known text; reader reads render_cells() concurrently,
        both under one shared lock (matching term._lock in production).
        Fails (via self.fail from the reader thread) if any rendered row
        contains a character that isn't blank and isn't part of the known
        alphabet being fed -- that's what "splatter" looks like: stray
        characters or long runs of U+2500 that don't belong to the input.
        """
        screen, parser = _make_parser(cols=40, rows=20)
        lock = threading.Lock()

        stop = threading.Event()
        errors = []
        iterations = {"feed": 0, "render": 0}

        ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
        ALLOWED = set(ALPHABET) | {" ", "\r", "\n"}

        def writer():
            i = 0
            try:
                while not stop.is_set():
                    ch = ALPHABET[i % len(ALPHABET)]
                    text = (ch * 39) + "\r\n"
                    with lock:
                        parser.feed(text)
                    iterations["feed"] += 1
                    i += 1
            except Exception as e:  # pragma: no cover - diagnostic path
                errors.append(("writer", repr(e)))

        def reader():
            try:
                while not stop.is_set():
                    with lock:
                        rows, cy, cx = screen.render_cells()
                    for row in rows:
                        text = "".join(c for c, _a in row) if row and isinstance(row[0], tuple) else "".join(row)
                        bad = [c for c in text if c not in ALLOWED]
                        if bad:
                            errors.append(("splatter", text, bad))
                            stop.set()
                            return
                    iterations["render"] += 1
            except Exception as e:  # pragma: no cover - diagnostic path
                errors.append(("reader", repr(e)))
                stop.set()

        wt = threading.Thread(target=writer, daemon=True)
        rt = threading.Thread(target=reader, daemon=True)
        wt.start()
        rt.start()

        DURATION_S = 10
        deadline = time.time() + DURATION_S
        while time.time() < deadline and not stop.is_set():
            time.sleep(0.1)
        stop.set()
        wt.join(timeout=5)
        rt.join(timeout=5)

        hang = wt.is_alive() or rt.is_alive()

        print(
            "\n[splatter-stress] feed iterations=%d render iterations=%d "
            "writer_alive=%s reader_alive=%s errors=%d"
            % (iterations["feed"], iterations["render"], wt.is_alive(),
               rt.is_alive(), len(errors))
        )
        if errors:
            for e in errors[:5]:
                print("[splatter-stress] error:", e)

        self.assertFalse(hang, "writer or reader thread did not exit within 5s of stop() -- possible hang under sustained concurrent load")
        self.assertEqual(errors, [], "splatter or exception reproduced under correct Python-level locking")


if __name__ == "__main__":
    def _watchdog():
        time.sleep(90)
        print("[splatter-stress] WATCHDOG: process did not exit in 90s, forcing exit")
        os._exit(2)

    threading.Thread(target=_watchdog, daemon=True).start()
    unittest.main()
