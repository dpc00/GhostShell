"""Append-only log of what has appeared on the Sublime tab.

The file only grows. Each paint is compared to the previous paint. Lines
that have moved off the live last row (new stable rows, or a row that
changed in place) are appended. The last row is live — typing, spinner,
status — and is written when it becomes stable or when the session closes.

observe() is cheap and runs on Sublime's main thread. Disk writes happen
on a background timer after 0.5s of quiet so a hot render loop cannot
freeze the UI. close() flushes immediately, including the live row.
"""
import os
import threading
import time
import traceback

from .log_paths import LOG_ROOT, makedirs_private, open_private

TEXT_LOG_DIR = os.path.join(LOG_ROOT, "ai_terminal_session_text_logs")

# observe() only queues; the timer writes. Found live 2026-09-14: a
# streaming tab was writing on every ~30ms render tick and freezing ST.
_WRITE_DEBOUNCE_S = 0.5

_stats_lock = threading.Lock()
_stats = {
    "observe_calls": 0,
    "observe_writes": 0,
    "write_seconds_total": 0.0,
    "write_seconds_max": 0.0,
}


def session_text_log_stats():
    with _stats_lock:
        return dict(_stats)


# Only the top of a paint (scrollback) is stable enough to line up two
# paints; the bottom is the live screen and changes every frame.
_ALIGN_ROWS = 30
_MIN_ALIGN_ROWS = 5


def _scrolled_off_count(prev, present):
    """How many lines left the top of the tab between two paints.

    0 when the top is unchanged or the paints cannot be lined up
    (a full redraw): callers must not guess.
    """
    if not prev or not present or not present[0].strip():
        return 0
    rows = min(_ALIGN_ROWS, len(present))
    if prev[:rows] == present[:rows]:
        return 0
    for k in range(1, len(prev)):
        m = min(rows, len(prev) - k)
        if m < min(rows, _MIN_ALIGN_ROWS):
            break
        if prev[k:k + m] == present[:m]:
            return k
    return 0


def _stable(lines):
    if not lines:
        return []
    return lines[:-1]


def _is_last_line_edit(prev, present):
    return (
        bool(prev)
        and bool(present)
        and len(prev) == len(present)
        and prev[:-1] == present[:-1]
    )


def _is_prefix_growth(old, new):
    return bool(old) and (new.startswith(old) or old.startswith(new))


def _new_stable_lines(prev, present):
    """Lines that are now off the live last row and not already implied by prev."""
    if not prev:
        return list(_stable(present))
    if _is_last_line_edit(prev, present):
        if not _is_prefix_growth(prev[-1], present[-1]):
            return [prev[-1]]
        return []
    gone = _scrolled_off_count(prev, present)
    present_stable = _stable(present)
    prev_stable = _stable(prev[gone:])
    out = []
    if (
        gone == 0
        and prev_stable != present_stable
        and (not present_stable or prev_stable[:1] != present_stable[:1])
    ):
        # Full redraw: the previous live row was on screen and is gone.
        out.append(prev[-1])
    for i, line in enumerate(present_stable):
        if i >= len(prev_stable) or prev_stable[i] != line:
            out.append(line)
    return out


class SessionTextLog:
    def __init__(self):
        self.file = None
        self._path = None
        self._prev = []
        self._pending = []
        self._last_written = None
        self._lock = threading.Lock()
        self._write_timer = None

    def open(self, filename_stamp):
        makedirs_private(TEXT_LOG_DIR)
        path = os.path.join(TEXT_LOG_DIR, "ai_%s.log" % filename_stamp)
        handle = open_private(path, "a", encoding="utf-8", newline="\n")
        with self._lock:
            self.file = handle
            self._path = path
            self._prev = []
            self._pending = []
            self._last_written = None
            self._write_timer = None

    def write_line(self, text):
        with self._lock:
            if self.file is None:
                return
            text = text or ""
            if not text.strip() or text == self._last_written:
                return
            self.file.write(text + "\n")
            self.file.flush()
            self._last_written = text

    def observe(self, lines, now=None, trailing_newline=True):
        """Queue newly stable tab lines. ``now`` / ``trailing_newline``
        remain accepted for older callers; the file is line-oriented."""
        present = ["" if line is None else str(line) for line in (lines or ())]
        with _stats_lock:
            _stats["observe_calls"] += 1
        with self._lock:
            if self.file is None or present == self._prev:
                return
            self._pending.extend(_new_stable_lines(self._prev, present))
            self._prev = present
            if self._write_timer is None:
                timer = threading.Timer(_WRITE_DEBOUNCE_S, self._flush_pending)
                timer.daemon = True
                self._write_timer = timer
                timer.start()

    def _flush_pending(self):
        with self._lock:
            self._write_timer = None
            if self.file is None or not self._pending:
                return
            try:
                self._write_pending_locked()
            except OSError:
                pass  # already printed inside _write_pending_locked

    def flush_now(self):
        """Write queued stable lines immediately. Does not write the live
        last row; close() does that."""
        timer = None
        with self._lock:
            timer = self._write_timer
            self._write_timer = None
        if timer is not None:
            timer.cancel()
        with self._lock:
            if self.file is None or not self._pending:
                return
            self._write_pending_locked()

    def _write_pending_locked(self):
        """Caller holds self._lock. Append pending lines; never rewrite."""
        if self.file is None or not self._pending:
            return
        write_started = time.monotonic()
        payload = "".join(line + "\n" for line in self._pending)
        try:
            self.file.write(payload)
            self.file.flush()
        except OSError:
            print(
                "[ai_terminal] session text log: append failed:\n%s"
                % traceback.format_exc()
            )
            raise
        elapsed = time.monotonic() - write_started
        with _stats_lock:
            _stats["observe_writes"] += 1
            _stats["write_seconds_total"] += elapsed
            if elapsed > _stats["write_seconds_max"]:
                _stats["write_seconds_max"] = elapsed
        self._last_written = self._pending[-1]
        self._pending = []

    def flush_live_lines(self, lines):
        self.observe(lines)

    def flush_held(self, force=True, now=None):
        return

    def close(self):
        timer = None
        with self._lock:
            timer = self._write_timer
            self._write_timer = None
        if timer is not None:
            timer.cancel()
        with self._lock:
            if self.file is None:
                return
            if self._prev:
                live = self._prev[-1]
                if live != self._last_written and not (
                    self._pending and self._pending[-1] == live
                ):
                    self._pending.append(live)
            try:
                self._write_pending_locked()
            except OSError:
                pass
            try:
                self.file.close()
            except OSError:
                print(
                    "[ai_terminal] session text log: close failed:\n%s"
                    % traceback.format_exc()
                )
            self.file = None
            self._path = None
            self._prev = []
            self._pending = []
            self._last_written = None
