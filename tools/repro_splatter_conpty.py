"""Reproduce Testing Agent replay corruption through a real Windows ConPTY.

Unlike the parser-only tests, this preserves production's 8192-byte ReadFile
chunking and wall-clock delivery. Run directly from the repository root:

    python tools/repro_splatter_conpty.py
"""
import codecs
import collections
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from terminal.ghostty_engine import GhosttyParser
from terminal.screen import Screen
from tools.agent_broker import _Pty


PROMPT_RE = re.compile(r"^› user prompt TURN-(\d\d)(.*)$")
REPLY_RE = re.compile(r"^• TURN-(\d\d) L(\d\d) mock agent reply line (\d+)$")


def _row_text(row):
    if row and isinstance(row[0], tuple):
        return "".join(ch for ch, _attr in row).rstrip()
    return "".join(row).rstrip()


def _bad_rows(screen):
    rows = list(screen.history) + list(screen.grid)
    bad = []
    for index, row in enumerate(rows):
        text = _row_text(row)
        if not text:
            continue
        prompt = PROMPT_RE.match(text)
        if prompt:
            if prompt.group(2):
                bad.append((index, text, "prompt has stale tail"))
            continue
        reply = REPLY_RE.match(text)
        if reply:
            turn, line, value = (int(part) for part in reply.groups())
            if value != turn * 1000 + line:
                bad.append((index, text, "reply value is misaligned"))
    return bad


def _header_counts(screen):
    rows = list(screen.history) + list(screen.grid)
    counts = collections.Counter()
    for row in rows:
        prompt = PROMPT_RE.match(_row_text(row))
        if prompt:
            counts[prompt.group(1)] += 1
    return dict(sorted(counts.items()))


def run_once(cols=93, rows=47):
    screen = Screen(cols, rows, history_cap=300)
    parser = GhosttyParser(screen, force_main_screen=True)
    if os.environ.get("GHOSTSHELL_REPRO_DISABLE_FIX"):
        parser.force_main_screen = False
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    argv = [sys.executable, os.path.join("tests", "mock_agent_cli.py")]
    pty = _Pty(argv, os.getcwd(), cols, rows, os.environ.copy())
    lock = threading.Lock()
    chunks = []
    first_bad = []
    frames = 0

    def on_data(data):
        nonlocal frames
        text = decoder.decode(data)
        with lock:
            parser.feed(text)
            chunks.append(len(data))
            frames += text.count("\x1b[?2026l")
            bad = _bad_rows(screen)
            if bad and not first_bad:
                first_bad.extend((frames, len(chunks), list(chunks), bad[:20]))

    pty.start()
    reader = threading.Thread(target=pty.read, args=(on_data,), daemon=True)
    reader.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            with lock:
                if frames >= 1:
                    break
            time.sleep(0.01)
        pty.write(b"5\r")
        deadline = time.time() + 15
        while time.time() < deadline:
            with lock:
                if frames >= 7:
                    break
            time.sleep(0.01)
        pty.write(b"9\r")
        deadline = time.time() + 10
        while time.time() < deadline:
            with lock:
                if frames >= 8:
                    break
            time.sleep(0.01)
        with lock:
            final_bad = _bad_rows(screen)
            result = {
                "frames": frames,
                "chunks": list(chunks),
                "first_bad": list(first_bad),
                "final_bad": final_bad[:20],
                "history": len(screen.history),
                "headers": _header_counts(screen),
            }
        return result
    finally:
        try:
            pty.write(b"q")
        finally:
            pty.kill()
            reader.join(timeout=2)


def main():
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    failures = 0
    for trial in range(1, trials + 1):
        result = run_once()
        bad = bool(result["first_bad"] or result["final_bad"])
        failures += int(bad)
        print(
            "trial=%d frames=%d chunks=%d history=%d bad=%s sizes=%s"
            % (
                trial,
                result["frames"],
                len(result["chunks"]),
                result["history"],
                bad,
                result["chunks"],
            )
        )
        print("  headers=%s" % result["headers"])
        if result["first_bad"]:
            print("  first_bad=%r" % (result["first_bad"],))
        if result["final_bad"]:
            print("  final_bad=%r" % (result["final_bad"],))
    print("failures=%d/%d" % (failures, trials))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
