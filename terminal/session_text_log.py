"""A live text snapshot of what was last painted on the Sublime tab.

Each paint already contains the complete rendered tab (scrollback and live
screen).  Keep that snapshot verbatim instead of trying to turn successive
frames into an append-only transcript: appending records every input edit,
spinner frame, and status-line redraw that the user only saw temporarily.
"""
import os
import threading
import time
import traceback

from .log_paths import LOG_ROOT, makedirs_private, open_private

TEXT_LOG_DIR = os.path.join(LOG_ROOT, "ai_terminal_session_text_logs")

# How long to coalesce a burst of rapid changed-paint calls into a single
# disk write. observe() itself stays cheap and runs on ST's main thread (it
# only compares/stores the pending snapshot); the actual temp-file write +
# os.replace() + close + reopen happens on a background threading.Timer
# after this many seconds of quiet, so a high-throughput session (many
# changed paints per second) does at most one real write per window instead
# of one per paint. Found live 2026-09-14: a continuously-streaming terminal
# tab was driving this write on every ~30ms render tick, each one blocking
# ST's main thread for real disk I/O, causing multi-second UI freezes.
# close() flushes any still-pending snapshot synchronously so nothing is
# lost when a session ends mid-window.
_WRITE_DEBOUNCE_S = 0.5

# Instrumentation only -- these counters answer "how often, how expensive"
# without needing external stack sampling. Read via session_text_log_stats();
# process-lifetime, not per-instance, since a terminal's SessionTextLog is
# recreated per session.
_stats_lock = threading.Lock()
_stats = {"observe_calls": 0, "observe_writes": 0, "write_seconds_total": 0.0, "write_seconds_max": 0.0}


def session_text_log_stats():
    with _stats_lock:
        return dict(_stats)


class SessionTextLog:
    def __init__(self):
        self.file = None
        self._path = None
        self._prev = []
        self._prev_trailing_newline = None
        self._last_written = None
        self._lock = threading.Lock()
        # Debounced-write state: the most recent snapshot observe() has seen
        # but not yet written to disk, and the armed timer that will write it
        # (or None if no write is currently scheduled). Both guarded by
        # self._lock, same as every other field here.
        self._pending = None
        self._write_timer = None

    def open(self, filename_stamp):
        makedirs_private(TEXT_LOG_DIR)
        path = os.path.join(TEXT_LOG_DIR, "ai_%s.log" % filename_stamp)
        handle = open_private(path, "a", encoding="utf-8", newline="\n")
        with self._lock:
            self.file = handle
            self._path = path
            self._prev = []
            self._prev_trailing_newline = None
            self._last_written = None
            self._pending = None
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
        """Record the current tab paint as the pending snapshot.

        ``now`` remains accepted for compatibility with older callers.
        Blank lines and horizontal spacing are significant parts of the paint.

        Cheap and safe to call from ST's main thread on every render tick:
        this only compares against the last-seen paint and stores the result
        for the debounced background writer in _flush_pending -- it never
        touches disk itself. See _WRITE_DEBOUNCE_S for why.
        """
        present = ["" if line is None else str(line) for line in (lines or ())]
        with _stats_lock:
            _stats["observe_calls"] += 1
        with self._lock:
            if (
                self.file is None
                or (
                    present == self._prev
                    and trailing_newline == self._prev_trailing_newline
                )
            ):
                return
            # Deliberately NOT updated here: self._prev/_last_written only
            # advance once _write_snapshot_locked actually succeeds (see
            # there), same as the old synchronous code -- so a failed write
            # leaves _prev stale and the identical content is treated as
            # "changed" again on the next observe(), letting it retry.
            self._pending = (present, trailing_newline)
            if self._write_timer is None:
                timer = threading.Timer(_WRITE_DEBOUNCE_S, self._flush_pending)
                timer.daemon = True
                self._write_timer = timer
                timer.start()

    def _flush_pending(self):
        """Write the most recent pending snapshot to disk. Runs on a
        background threading.Timer thread, never on ST's main thread --
        so unlike flush_now(), a write failure here is caught and printed
        (already done inside _write_snapshot_locked) rather than raised,
        since there is no synchronous caller left to hand it to."""
        with self._lock:
            self._write_timer = None
            if self.file is None or self._pending is None:
                return
            present, trailing_newline = self._pending
            self._pending = None
            try:
                self._write_snapshot_locked(present, trailing_newline)
            except OSError:
                pass  # already printed inside _write_snapshot_locked

    def flush_now(self):
        """Force any pending debounced write to happen immediately,
        synchronously, on the calling thread -- for callers (tests, close())
        that need the on-disk file to reflect the latest observe() call
        right away. Unlike the background debounced path, a write failure
        here propagates to the caller, matching observe()'s old synchronous
        contract for whoever still wants to see it."""
        timer = None
        with self._lock:
            timer = self._write_timer
            self._write_timer = None
        if timer is not None:
            timer.cancel()
        with self._lock:
            if self.file is None or self._pending is None:
                return
            present, trailing_newline = self._pending
            self._pending = None
            self._write_snapshot_locked(present, trailing_newline)

    def _write_snapshot_locked(self, present, trailing_newline):
        """Do the actual temp-file write + os.replace() + close + reopen.
        Caller must already hold self._lock."""
        write_started = time.monotonic()
        path = self._path
        temp_path = path + ".tmp"
        snapshot = "\n".join(present)
        if present and trailing_newline:
            snapshot += "\n"
        replacement = None
        fallback = None
        try:
            replacement = open_private(
                temp_path, "w", encoding="utf-8", newline="\n"
            )
            replacement.write(snapshot)
            replacement.flush()
            replacement.close()
            replacement = None

            self.file.close()
            self.file = None
            try:
                os.replace(temp_path, path)
            except PermissionError:
                # Windows refuses os.replace() while some readers keep
                # the destination open without FILE_SHARE_DELETE. Only
                # that operation gets the in-place fallback; a permission
                # failure creating/writing the temporary file is a real
                # logging failure and must not truncate the old snapshot.
                fallback = open_private(path, "w", encoding="utf-8", newline="\n")
                fallback.write(snapshot)
                fallback.flush()
                fallback.close()
                fallback = None
                os.remove(temp_path)
            finally:
                self.file = open_private(
                    path, "a", encoding="utf-8", newline="\n"
                )
        except OSError:
            print("[ai_terminal] session text log: snapshot write failed:\n%s"
                  % traceback.format_exc())
            if replacement is not None:
                try:
                    replacement.close()
                except OSError:
                    print("[ai_terminal] session text log cleanup: "
                          "replacement.close() failed:\n%s" % traceback.format_exc())
            if fallback is not None:
                try:
                    fallback.close()
                except OSError:
                    print("[ai_terminal] session text log cleanup: "
                          "fallback.close() failed:\n%s" % traceback.format_exc())
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                print("[ai_terminal] session text log cleanup: "
                      "remove temp_path failed:\n%s" % traceback.format_exc())
            if self.file is None:
                try:
                    self.file = open_private(
                        path, "a", encoding="utf-8", newline="\n"
                    )
                except OSError:
                    print("[ai_terminal] session text log: reopen after "
                          "failure also failed:\n%s" % traceback.format_exc())
            raise
        elapsed = time.monotonic() - write_started
        with _stats_lock:
            _stats["observe_writes"] += 1
            _stats["write_seconds_total"] += elapsed
            if elapsed > _stats["write_seconds_max"]:
                _stats["write_seconds_max"] = elapsed
        # Only advance on success (an exception above returns before this
        # point) -- see the comment in observe() for why that matters.
        self._prev = present
        self._prev_trailing_newline = trailing_newline
        self._last_written = present[-1] if present else None

    def flush_live_lines(self, lines):
        self.observe(lines)

    def flush_held(self, force=True, now=None):
        return

    def close(self):
        try:
            # A background write may have been scheduled but not fired yet --
            # flush it now, synchronously, so the log reflects the true final
            # state instead of whatever the last completed write happened to
            # catch mid-burst.
            self.flush_now()
        except OSError:
            pass  # already logged inside _write_snapshot_locked
        with self._lock:
            if self.file is None:
                return
            try:
                self.file.close()
            except OSError:
                print("[ai_terminal] session text log: close failed:\n%s"
                      % traceback.format_exc())
            self.file = None
            self._path = None
            self._prev = []
            self._prev_trailing_newline = None
