#!/usr/bin/env python3
"""mock_agent_cli.py -- fake agent for ai_terminal. Default is Codex replay.

Usage:
    python tests/mock_agent_cli.py            # replay flood (Testing Agent)
    python tests/mock_agent_cli.py --box      # caret-tracking Ink box
    python tests/mock_agent_cli.py --mouse    # SGR mouse-tracking harness
    python tests/mock_agent_cli.py --legacy-resize

The Testing Agent profile launches this with no flags. Default mode
replays the whole transcript on every Enter, the same way Codex does:
CSI ?2026h, ESC[H, then every prior turn as new lines (taller than the
PTY), then CSI ?2026l. Open the profile, press Enter a few times, and
search the ST tab for TURN-00. One hit = scrollback is clean. N hits =
the four-month flood is still there.

    Keys (replay):
        Enter / s  -> add a turn and dump the full transcript
        5          -> do that five times
        q / ^C     -> quit
"""
import argparse
import os
import signal
import sys
import time
import traceback

out = sys.stdout.buffer


def _log(msg):
    try:
        log_path = os.path.expanduser("~/data/logs/ai_terminal/mock_agent_cli.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        print("[mock_agent_cli] _log write failed:\n%s" % traceback.format_exc(),
              file=sys.stderr)


def get_size():
    try:
        ts = os.get_terminal_size(sys.stdout.fileno())
        return ts.columns, ts.lines
    except OSError:
        _log("get_size failed:\n%s" % traceback.format_exc())
        return 80, 24


def _write(data):
    out.write(data.encode("utf-8", "replace"))
    out.flush()


# ─── Replay mode (default): Codex home+dump of the whole transcript ─────────

_LINES_PER_TURN = 28


def make_turn_lines(n, body_lines=_LINES_PER_TURN):
    """One unique turn. TURN-%02d is the search key in the ST tab."""
    tag = "TURN-%02d" % n
    lines = ["› user prompt %s" % tag]
    for i in range(body_lines):
        lines.append("• %s L%02d mock agent reply line %d" % (tag, i, n * 1000 + i))
    return lines


def encode_replay_frame(lines):
    """Exact Codex paint: synchronized output, home, full dump, end sync."""
    body = "\r\n".join(lines) + "\r\n"
    return "\x1b[?2026h\x1b[?25l\x1b[H" + body + "\x1b[?2026l"


class ReplayAgent:
    """Grows a transcript and re-emits all of it on every turn."""

    def __init__(self, seed_turns=3):
        self.turns = []
        for n in range(seed_turns):
            self.turns.extend(make_turn_lines(n))
        self._next = seed_turns
        self._running = True

    def _paint(self):
        _write(encode_replay_frame(self.turns))
        _log("replay turns=%d lines=%d" % (self._next, len(self.turns)))

    def _add_turn(self):
        self.turns.extend(make_turn_lines(self._next))
        self._next += 1
        self._paint()

    def run(self):
        cols, rows = get_size()
        _log("replay-mode start cols=%s rows=%s seed_lines=%d" % (
            cols, rows, len(self.turns)))
        self._paint()
        while self._running:
            try:
                ch = sys.stdin.read(1)
            except (OSError, ValueError):
                _log("stdin.read(1) failed:\n%s" % traceback.format_exc())
                ch = ""
            if not ch:
                time.sleep(0.02)
                continue
            if ch in ("q", "Q", "\x03"):
                self._running = False
                break
            if ch in ("s", "S", "\r", "\n"):
                self._add_turn()
            elif ch == "5":
                for _ in range(5):
                    self._add_turn()
        _write("\x1b[?25h\r\n[mock_agent exited]\r\n")
        _log("replay-mode exit")


# ─── Box mode: deterministic Ink-style prompt, self-reporting ground truth ──

_PROMPT = "❯ "  # "❯ " -- same marker terminal/caret.py's find_prompt_row expects
_BANNER_ROWS = 2   # rows 0-1: title + blank
_BOX_TOP_ROW = 2   # row 2: box top border
_BOX_INPUT_ROW = 3  # row 3: "│ ❯ <input> │"
_BOX_BOTTOM_ROW = 4  # row 4: box bottom border
_STATUS_ROW = 6    # row 6: "[expect] ..." ground truth
_HELP_ROW = 8      # row 8: key legend


class MockInkAgent:
    """Fixed-layout bordered prompt. Never scrolls -- every row index this
    prints is a compile-time constant, so a ground-truth (row, col) needs no
    live recomputation, only the constants below plus len(input)/cursor."""

    def __init__(self, cols, rows):
        self.cols = cols
        self.rows = rows
        self.input = []
        self.cursor = 0
        self.cursor_visible = True
        self._running = True

    def _clear(self):
        _write("\x1b[2J\x1b[H")

    def _goto(self, row, col):
        # CUP is 1-based.
        _write(f"\x1b[{row + 1};{col + 1}H")

    def _expected(self):
        """(row, col) the real hardware cursor sits at -- ground truth."""
        return _BOX_INPUT_ROW, 2 + len(_PROMPT) + self.cursor

    def _draw(self):
        self._clear()
        width = max(20, min(self.cols, 78))
        inner = width - 2
        _write("Mock Ink Agent -- caret tracking test harness\r\n\r\n")

        top = "╭" + "─" * inner + "╮"
        bot = "╰" + "─" * inner + "╯"

        text = _PROMPT + "".join(self.input)
        content = text[: inner - 2]
        pad = " " * (inner - 2 - len(content))
        mid = "│ " + content + pad + " │"

        _write(top + "\r\n")
        if self.cursor_visible:
            _write(mid + "\r\n")
        else:
            # Ink hides the real cursor (DECTCEM off) and reverse-videos the
            # cell itself instead -- reproduce that exactly so pad_row_for_caret
            # / paint_host_cursor's "cursor_visible False" branch gets a real
            # workout, not just the common hardware-cursor path.
            idx = 2 + len(_PROMPT) + self.cursor  # column within `mid`
            if idx < len(mid):
                ch = mid[idx]
                mid_reversed = mid[:idx] + f"\x1b[7m{ch}\x1b[0m" + mid[idx + 1 :]
            else:
                mid_reversed = mid + "\x1b[7m \x1b[0m"
            _write(mid_reversed + "\r\n")
        _write(bot + "\r\n")

        _write("\r\n")
        row, col = self._expected()
        abs_input_len = len(self.input)
        _write(
            f"[expect] row={row} col={col} cursor_idx={self.cursor} "
            f"input_len={abs_input_len} visible={self.cursor_visible}"
            + "\x1b[K\r\n"
        )
        _write("\r\n")
        _write(
            "keys: type to insert | left/right move | backspace delete | "
            "home/end jump | v toggle cursor | enter clear | q quit" + "\x1b[K\r\n"
        )

        if self.cursor_visible:
            self._goto(row, col)
            _write("\x1b[?25h")
        else:
            _write("\x1b[?25l")
            # Park hardware cursor somewhere harmless-but-valid; Ink itself
            # does not bother placing it meaningfully once DECTCEM is off.
            self._goto(row, col)

    def run(self):
        self.cols, self.rows = get_size()
        _log(f"box-mode start cols={self.cols} rows={self.rows}")
        self._draw()

        while self._running:
            try:
                ch = sys.stdin.read(1)
            except (OSError, ValueError):
                _log("stdin.read(1) failed:\n%s" % traceback.format_exc())
                ch = ""
            if not ch:
                time.sleep(0.02)
                continue
            if ch in ("q", "\x03"):
                self._running = False
                break
            elif ch in ("\r", "\n"):
                self.input = []
                self.cursor = 0
            elif ch in ("\x7f", "\x08"):  # backspace
                if self.cursor > 0:
                    del self.input[self.cursor - 1]
                    self.cursor -= 1
            elif ch == "\x1b":
                # Escape sequence: read the rest of a CSI arrow/home/end.
                seq = ch
                seq += sys.stdin.read(1)
                if len(seq) == 2 and seq[1] == "[":
                    seq += sys.stdin.read(1)
                tail = seq[-1] if seq else ""
                if tail == "D":  # left
                    self.cursor = max(0, self.cursor - 1)
                elif tail == "C":  # right
                    self.cursor = min(len(self.input), self.cursor + 1)
                elif tail == "H":  # home
                    self.cursor = 0
                elif tail == "F":  # end
                    self.cursor = len(self.input)
                elif seq == "\x1b":
                    pass  # bare escape, ignore
            elif ch == "v":
                self.cursor_visible = not self.cursor_visible
            elif ch.isprintable():
                self.input.insert(self.cursor, ch)
                self.cursor += 1
            self._draw()
            _log(
                f"key={ch!r} cursor={self.cursor} input={''.join(self.input)!r} "
                f"visible={self.cursor_visible}"
            )

        _write("\x1b[?25h\r\n[mock_agent exited]\r\n")
        _log("box-mode exit")


# ─── Mouse-tracking mode: local, free harness for the Shift/Ctrl-drag ───────
# question (2026-09-09) -- does ai_terminal's mousemap ever actually deliver
# modifier info to a mouse-tracking-enabled app, or does Sublime's own
# drag_select absorb Shift/Ctrl before a PTY sequence is ever generated?
# Real answer only obtainable by watching what this app actually receives,
# not by reading ai_terminal.py's own comments about what it intends to send.

_MOUSE_BUTTON_ROW = 8
_MOUSE_BUTTON_COL = 4
_MOUSE_BUTTON_LABEL = "[ Click / Drag Me ]"
_MOUSE_LOG_ROWS = 10


class MockMouseAgent:
    """Fullscreen (alt-screen) app with DEC/SGR mouse tracking enabled and a
    fixed on-screen "button" target. Every raw mouse report received is
    parsed (button, Shift/Meta/Ctrl bits, motion flag, 1-based col/row) and
    appended to an on-screen log -- so a live test shows exactly what
    reaches this process, not what ai_terminal.py's comments claim it sends.

    ROOT-CAUSED 2026-09-09 (independently reconfirmed live by Vibe/Mistral
    reading this same codebase): a live test through this harness showed
    the enable sequence's bytes visibly arriving (the screen content this
    class writes right after it renders fine) yet ai_terminal's
    screen.private_modes never picks up 1000/1002/1003/1006, while feeding
    the identical text directly into the same parser does set them. Cause
    is Windows ConPTY, not this file or ai_terminal.py: ConPTY's mouse
    passthrough is keyed to the child calling the Win32 console API
    `SetConsoleMode(stdin, ENABLE_MOUSE_INPUT)` (microsoft/terminal#376,
    fixed by #9970), not to it writing an xterm-style escape sequence to
    its own stdout the way this class (and most cross-platform CLI agents)
    does -- conhost swallows that convention internally and never re-emits
    it. RE-SCOPED same day: not a universal Windows ceiling -- the
    "GitHub Copilot" profile is a live counter-example whose real mouse
    tracking genuinely works, plausibly because it calls the real Win32
    API too. This harness demonstrates the swallowed-escape path
    specifically (still has diagnostic value: proves whether a *future*
    fix changes this, and exercises the tap/multi-click fallback path
    that isn't affected) -- it just can't demonstrate real click/drag
    through *this* stdout convention on Windows today.
    """

    def __init__(self, cols, rows):
        self.cols = cols
        self.rows = rows
        self._running = True
        self._log_lines = []
        self._clicks = 0

    def _enable_mouse(self):
        # ?1000 (click) + ?1002 (cell-motion drag) + ?1003 (any-motion) +
        # ?1006 (SGR extended coords) -- same combination Vibe's profile
        # comment documents (ai_terminal.sublime-settings).
        _write("\x1b[?1049h\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h")

    def _disable_mouse(self):
        _write("\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l\x1b[?1049l")

    def _goto(self, row, col):
        _write(f"\x1b[{row + 1};{col + 1}H")

    def _draw(self):
        _write("\x1b[2J\x1b[H")
        _write("Mock Mouse Agent -- SGR mouse-tracking test harness\r\n")
        _write("Reports every raw mouse event this process receives.\r\n\r\n")
        self._goto(_MOUSE_BUTTON_ROW, _MOUSE_BUTTON_COL)
        _write(_MOUSE_BUTTON_LABEL)
        self._goto(_MOUSE_BUTTON_ROW + 1, _MOUSE_BUTTON_COL)
        _write("clicks on button: %d" % self._clicks)
        self._goto(_MOUSE_BUTTON_ROW + 3, 0)
        _write("last %d raw event(s):\x1b[K" % _MOUSE_LOG_ROWS)
        for i in range(_MOUSE_LOG_ROWS):
            self._goto(_MOUSE_BUTTON_ROW + 4 + i, 0)
            line = self._log_lines[-(i + 1)] if i < len(self._log_lines) else ""
            _write(line + "\x1b[K")
        self._goto(self.rows - 1, 0)
        _write("q / ^C quits and disables mouse tracking\x1b[K")

    def _on_sgr_event(self, cb, col, row, is_release):
        # SGR (CSI < Cb ; Cx ; Cy M/m): bit flags on Cb --
        # 4=Shift, 8=Meta/Alt, 16=Ctrl, 32=motion (drag/move, not a click).
        button_num = cb & 0x03
        shift = bool(cb & 4)
        meta = bool(cb & 8)
        ctrl = bool(cb & 16)
        motion = bool(cb & 32)
        kind = "release" if is_release else ("drag" if motion else "press")
        mods = "+".join(
            m for m, on in (("shift", shift), ("ctrl", ctrl), ("meta", meta)) if on
        ) or "none"
        line = "cb=%d btn=%d %s mods=%s col=%d row=%d" % (
            cb, button_num, kind, mods, col, row,
        )
        self._log_lines.append(line)
        _log("mouse " + line)
        in_button = (
            row - 1 == _MOUSE_BUTTON_ROW
            and _MOUSE_BUTTON_COL <= col - 1 < _MOUSE_BUTTON_COL + len(_MOUSE_BUTTON_LABEL)
        )
        if in_button and kind == "press" and button_num == 0:
            self._clicks += 1

    def _read_sgr_tail(self):
        """After CSI '<' already consumed: read 'Cb;Cx;Cy' + terminator."""
        buf = ""
        while True:
            ch = sys.stdin.read(1)
            if not ch:
                return
            if ch in ("M", "m"):
                parts = buf.split(";")
                if len(parts) == 3:
                    try:
                        cb, col, row = (int(p) for p in parts)
                    except ValueError:
                        _log("mouse: bad SGR body %r" % buf)
                        return
                    self._on_sgr_event(cb, col, row, is_release=(ch == "m"))
                return
            buf += ch

    def run(self):
        self.cols, self.rows = get_size()
        _log(f"mouse-mode start cols={self.cols} rows={self.rows}")
        # Diagnostic (2026-09-09): does a delayed/repeated enable behave
        # differently from an immediate one, i.e. is the PTY/ConPTY link not
        # yet ready to carry mode-toggle sequences the instant this process
        # starts writing? See ai_terminal's private_modes investigation.
        time.sleep(0.5)
        self._enable_mouse()
        time.sleep(0.5)
        self._enable_mouse()
        self._draw()
        try:
            while self._running:
                try:
                    ch = sys.stdin.read(1)
                except (OSError, ValueError):
                    _log("stdin.read(1) failed:\n%s" % traceback.format_exc())
                    ch = ""
                if not ch:
                    time.sleep(0.02)
                    continue
                if ch in ("q", "\x03"):
                    self._running = False
                    break
                if ch == "\x1b":
                    seq = sys.stdin.read(1)
                    if seq == "[":
                        marker = sys.stdin.read(1)
                        if marker == "<":
                            self._read_sgr_tail()
                            self._draw()
                            continue
                        # Non-mouse CSI (arrow keys etc. if terminal falls
                        # back to normal-tracking coords) -- ignore the rest.
                    continue
                # Any other keystroke: ignore, just redraw so the log stays
                # visible (helps confirm plain typing still reaches this
                # process independent of mouse mode).
                self._draw()
        finally:
            self._disable_mouse()
            _write("\r\n[mock_agent exited]\r\n")
            _log("mouse-mode exit")


# ─── Legacy resize-stress mode (original behaviour, unrelated to caret work) ─

colors = ["31", "32", "33", "34", "35", "36"]


class MockAgent:
    def __init__(self, initial_cols=80, initial_rows=24):
        self.cols, self.rows = initial_cols, initial_rows
        self.frame = 0
        self.turn = 0
        self.lines = []
        self._resize_count = 0
        self._last_cols, self._last_rows = 0, 0
        self._running = True

    def _write(self, data):
        _write(data)

    def _clear(self):
        self._write("\x1b[2J\x1b[H")

    def _make_paragraph(self, n):
        filler = (
            "mock agent response line"
            if n % 2 == 0
            else "user prompt replay stress test"
        )
        body = " ".join([f"{filler} #{n*1000+i}" for i in range(40)])
        return body

    def _banner(self):
        color = colors[self.frame % len(colors)]
        banner = (
            f"\x1b[1;{color}m[MockAgent]\x1b[0m "
            f"cols={self.cols} rows={self.rows} "
            f"frame={self.frame} resizes={self._resize_count} "
            f"turn={self.turn} | press s= spew, r=redraw, q=quit"
        )
        return banner[: self.cols]

    def _redraw(self, reason=""):
        self.frame += 1
        self._clear()
        self._write(self._banner() + "\r\n")
        visible = self.lines[-(self.rows - 2) :]
        for line in visible:
            self._write(line + "\r\n")
        self._write("\x1b[1;32m>\x1b[0m ")
        _log(f"redraw reason={reason} cols={self.cols} rows={self.rows} frame={self.frame}")

    def _spew(self):
        self.turn += 1
        para = self._make_paragraph(self.turn)
        header = f"\x1b[1;{colors[self.turn % len(colors)]}mTurn {self.turn}:\x1b[0m"
        self.lines.append(header + " " + para)
        self.lines = self.lines[-20:]
        self._redraw(reason="spew")

    def _on_resize(self, signum=None, frame=None):
        old = (self._last_cols, self._last_rows)
        self.cols, self.rows = get_size()
        self._last_cols, self._last_rows = self.cols, self.rows
        if (self.cols, self.rows) != old:
            self._resize_count += 1
            self._redraw(reason="sigwinch")

    def run(self):
        self.cols, self.rows = get_size()
        self._last_cols, self._last_rows = self.cols, self.rows
        _log(f"start cols={self.cols} rows={self.rows}")

        self._write("\x1b[?25l")
        self._clear()
        self._redraw(reason="startup")

        for _ in range(3):
            self._spew()

        if hasattr(signal, "SIGWINCH"):
            signal.signal(signal.SIGWINCH, self._on_resize)

        while self._running:
            try:
                ch = sys.stdin.read(1)
            except (OSError, ValueError):
                _log("stdin.read(1) failed:\n%s" % traceback.format_exc())
                ch = ""
            if not ch:
                time.sleep(0.05)
                continue
            if ch in ("q", "Q", "\x03"):
                self._running = False
                break
            if ch in ("s", "S", "\r", "\n"):
                self._spew()
            elif ch in ("r", "R"):
                self._on_resize(reason="manual_r")
            else:
                self._write(f"\x1b[{self.rows};1H\x1b[K key={repr(ch)} ")
                out.flush()

        self._write("\x1b[?25h\r\n[mock_agent exited]\r\n")
        _log("exit")


def main():
    parser = argparse.ArgumentParser(description="Mock agent CLI for ai_terminal testing")
    parser.add_argument("--cols", type=int, default=0, help="Initial cols hint")
    parser.add_argument("--rows", type=int, default=0, help="Initial rows hint")
    parser.add_argument(
        "--legacy-resize",
        action="store_true",
        help="Original scrolling/resize-stress workload.",
    )
    parser.add_argument(
        "--box",
        action="store_true",
        help="Caret-tracking Ink box (the old default).",
    )
    parser.add_argument(
        "--mouse",
        action="store_true",
        help="Fullscreen SGR mouse-tracking harness with a clickable target.",
    )
    args = parser.parse_args()

    cols, rows = get_size()
    if args.cols:
        cols = args.cols
    if args.rows:
        rows = args.rows

    if args.legacy_resize:
        MockAgent(initial_cols=cols, initial_rows=rows).run()
    elif args.box:
        MockInkAgent(cols, rows).run()
    elif args.mouse:
        MockMouseAgent(cols, rows).run()
    else:
        ReplayAgent().run()


if __name__ == "__main__":
    main()
