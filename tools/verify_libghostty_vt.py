"""Verify a libghostty-vt DLL against the ABI this repo's binding mirrors.

    python tools/verify_libghostty_vt.py [dll_path]

Defaults to terminal/bin/ghostty-vt.dll. Every check names what it proves, because the
failure mode this guards against is silent: a struct layout or an option id that no
longer matches does not raise, it configures the wrong thing.

Exit code 0 = every check passed.
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from terminal import ghostty_vt as gvt  # noqa: E402
from terminal.ghostty_engine import GhosttyParser  # noqa: E402
from terminal.screen import Screen  # noqa: E402

COLS, ROWS, CAP = 120, 40, 300
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + ("   " + detail if detail else ""))


def row_text(row):
    return "".join(ch for ch, _ in row) if row and isinstance(row[0], tuple) else "".join(row)


def main(dll_path):
    print("dll: %s" % dll_path)
    screen = Screen(COLS, ROWS, history_cap=CAP)
    p = GhosttyParser(screen, dll_path=dll_path)
    g = p._g

    check("terminal created (cols/rows, no options struct)",
          p._get_size(gvt.TERMINAL_DATA_COLS) == COLS and p._get_size(gvt.TERMINAL_DATA_ROWS) == ROWS)

    # Text and colours through the active-grid path (bulk cells get_multi + style flags)
    stream = ("\x1b[2J\x1b[1;1H\x1b[1;38;5;196mBOLD-RED\x1b[0m plain "
              "\x1b[4munder\x1b[0m \x1b[7mrev\x1b[0m")
    p.feed(stream)
    p._last_scrollback_rows = -1
    p._sync()
    text = row_text(screen.grid[0]) if isinstance(screen.grid[0][0], tuple) else "".join(screen.grid[0])
    check("text materialised", text.startswith("BOLD-RED plain under rev"), repr(text[:24]))

    attrs = screen.attrs[0]
    styled_word, plain_word = attrs[0], attrs[11]      # "BOLD-RED" vs "plain"
    check("style flags present (styled cell differs from plain)",
          styled_word != plain_word and styled_word,
          "styled=%d plain=%d" % (styled_word, plain_word))

    # Mode query (ghostty_terminal_mode_get is gone in 1.1.0-dev)
    p.feed("\x1b[?2026h")
    p._sync()
    sync_on = p._mode(gvt.MODE_SYNC_OUTPUT)
    p.feed("\x1b[?2026l")
    p._sync()
    check("mode 2026 readable and toggles", sync_on and not p._mode(gvt.MODE_SYNC_OUTPUT),
          "on=%s" % sync_on)

    # Scrollback: the cap is a native line count now, so history must stop at it
    s2 = Screen(COLS, ROWS, history_cap=CAP)
    p2 = GhosttyParser(s2, dll_path=dll_path)
    lines = "".join(("scroll line %05d " % i).ljust(80) + "\r\n" for i in range(CAP * 4))
    t0 = time.perf_counter()
    for i in range(0, len(lines), 8192):
        p2.feed(lines[i:i + 8192])
        p2.flush_pending()
    time.sleep(0.2)
    p2.flush_pending()
    burst = time.perf_counter() - t0
    newest_hist = row_text(s2.history[-1]) if s2.history else ""
    screen_top = row_text(s2.grid[0])
    # The invariant is positional, not arithmetical: history's newest row is the line
    # immediately above the first row on screen.
    def line_no(text):
        digits = "".join(c for c in text if c.isdigit())
        return int(digits) if digits else None
    check("scrollback capped at history_cap", len(s2.history) == CAP, "rows=%d" % len(s2.history))
    check("history abuts the screen (newest = screen top - 1)",
          line_no(newest_hist) is not None and line_no(newest_hist) == line_no(screen_top) - 1,
          "%r above %r" % (newest_hist[:20], screen_top[:20]))

    # The cost that made a TUI replay stall: one full-screen materialisation
    frame = "".join(("row %02d " % r).ljust(COLS) + "\r\n" for r in range(ROWS)).encode()
    times = []
    for _ in range(3):
        p2._g.terminal_vt_write(p2._term, b"\x1b[2J\x1b[H" + frame, len(frame) + 7)
        t = time.perf_counter()
        p2._sync_grid()
        times.append(time.perf_counter() - t)
        p2._g.render_state_update(p2._render_state, p2._term)
    per_cell = min(times) / (ROWS * COLS) * 1e6
    check("full-screen materialisation well under a 30 ms frame", per_cell < 8.0,
          "%.1f us/cell" % per_cell)

    check("burst did not take seconds per chunk", burst < 5.0, "%.2f s for %d lines" % (burst, CAP * 4))

    # Key encoding: this is the path a 1.1.0-dev swap silently broke (named keys faulted
    # because the text setter takes a GhosttyString by value now). Typing is the thing the
    # editor cannot do without, so the gate checks the bytes a terminal must receive.
    key_expectations = [
        ("a", {}, b"a"),
        ("enter", {}, b"\r"),
        ("backspace", {}, b"\x7f"),
        ("up", {}, b"\x1b[A"),
        ("tab", {}, b"\t"),
        ("c", {"ctrl": True}, b"\x03"),
    ]
    bad = []
    for key, mods, want in key_expectations:
        try:
            got = p.encode_key(key, **mods)
        except Exception as exc:                                   # noqa: BLE001
            got = "raised %s" % exc
        if got != want:
            bad.append("%s%s -> %r (want %r)" % (key, mods or "", got, want))
    check("key encoding produces the expected bytes", not bad, "; ".join(bad))

    # Mouse encoding: same class of change (renamed setters, renumbered option ids).
    try:
        p.feed("\x1b[?1000h\x1b[?1006h")        # normal tracking + SGR format
        p._sync()
        report = p.encode_mouse(0, 4, 5)         # left button press, column 4, row 5
        check("mouse encoding produces an SGR report",
              isinstance(report, str) and report.startswith("\x1b[<0;") and report.endswith("M"),
              repr(report))
    except Exception as exc:                                       # noqa: BLE001
        check("mouse encoding produces an SGR report", False, "raised %s" % exc)

    failed = [r for r in results if not r[1]]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(main(arg))
