"""The session .log is the Sublime tab, as text, without the 300-line cap.

The tab itself keeps `scrollback_history_size` (300) lines. The file keeps
every line that ever sat in that tab: when a paint loses lines off the top,
those lines stay in the file. The suffix of the file is the current tab, so
`tail` matches the bottom of the view.

No TUI reconstruction. In-place redraws replace the current tab suffix;
they do not invent extra frames. observe() is cheap and runs on Sublime's
main thread; disk writes wait 0.5s of quiet so a hot render loop cannot
freeze the UI. close() flushes immediately.
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

# Line up two paints by their tops. The tab's 300-line cap evicts from the
# top; that shift is what we must keep in the file.
_ALIGN_ROWS = 30
_MIN_ALIGN_ROWS = 5


def session_text_log_stats():
    with _stats_lock:
        return dict(_stats)


def _scrolled_off_count(prev, present):
    """How many lines left the top of the tab between two paints.

    0 when the top is unchanged or the paints cannot be lined up.
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
        if prev[k : k + m] == present[:m]:
            return k
    return 0


class SessionTextLog:
    def __init__(self):
        self.file = None
        self._path = None
        self._kept = []
        self._prev = []
        self._trailing_newline = True
        self._dirty = False
        self._lock = threading.Lock()
        self._write_timer = None

    def open(self, filename_stamp):
        makedirs_private(TEXT_LOG_DIR)
        path = os.path.join(TEXT_LOG_DIR, "ai_%s.log" % filename_stamp)
        handle = open_private(path, "w", encoding="utf-8", newline="\n")
        with self._lock:
            self.file = handle
            self._path = path
            self._kept = []
            self._prev = []
            self._trailing_newline = True
            self._dirty = False
            self._write_timer = None

    def write_line(self, text):
        """Unused. The file is the painted tab plus lines the cap evicted."""
        return

    def observe(self, lines, now=None, trailing_newline=True):
        """Remember the tab text just painted. ``now`` is ignored."""
        present = ["" if line is None else str(line) for line in (lines or ())]
        with _stats_lock:
            _stats["observe_calls"] += 1
        with self._lock:
            if self.file is None:
                return
            if present == self._prev and trailing_newline == self._trailing_newline:
                return
            gone = _scrolled_off_count(self._prev, present)
            if gone:
                self._kept.extend(self._prev[:gone])
            self._prev = present
            self._trailing_newline = bool(trailing_newline)
            self._dirty = True
            if self._write_timer is None:
                timer = threading.Timer(_WRITE_DEBOUNCE_S, self._flush_pending)
                timer.daemon = True
                self._write_timer = timer
                timer.start()

    def _flush_pending(self):
        with self._lock:
            self._write_timer = None
            if self.file is None or not self._dirty:
                return
            try:
                self._write_tab_locked()
            except OSError:
                pass  # already printed inside _write_tab_locked

    def flush_now(self):
        """Write kept-plus-tab immediately."""
        timer = None
        with self._lock:
            timer = self._write_timer
            self._write_timer = None
        if timer is not None:
            timer.cancel()
        with self._lock:
            if self.file is None or not self._dirty:
                return
            self._write_tab_locked()

    def _write_tab_locked(self):
        """Caller holds self._lock. File is evicted lines plus the current tab."""
        if self.file is None:
            return
        rows = self._kept + self._prev
        payload = "\n".join(rows)
        if rows and self._trailing_newline:
            payload += "\n"
        write_started = time.monotonic()
        try:
            self.file.seek(0)
            self.file.write(payload)
            self.file.truncate()
            self.file.flush()
        except OSError:
            print(
                "[ai_terminal] session text log: write failed:\n%s"
                % traceback.format_exc()
            )
            raise

        elapsed = time.monotonic() - write_started
        with _stats_lock:
            _stats["observe_writes"] += 1
            _stats["write_seconds_total"] += elapsed
            if elapsed > _stats["write_seconds_max"]:
                _stats["write_seconds_max"] = elapsed
        self._dirty = False

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
            try:
                if self._dirty or self._prev or self._kept:
                    self._dirty = True
                    self._write_tab_locked()
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
            self._kept = []
            self._prev = []
            self._trailing_newline = True
            self._dirty = False
