"""Per-session asciicast v3 recorder.

Recording at the stream layer (not scroll-off) means it is faithful to what
the child process emitted and does NOT duplicate on resume (a resume is a
new session = new .cast). One file per session (timestamped filename), not
per day, so a resume's replay is a separate recording rather than appended
duplicates.
"""
import json
import os
import threading
import time
import traceback

from .log_paths import log_root, makedirs_private, open_private, redact_secrets

_FLUSH_DELAY_S = 0.05   # events queued within this window are written together


def _cast_dir():
    return os.path.join(log_root(), "asciinema_casts_for_troubleshooting_rendering")


class CastRecorder:
    """Owns one .cast file for one terminal session.

    ``write()`` is a no-op before ``open()`` and after ``close()`` (or after
    any write failure), so callers never need to guard every call site.
    """

    def __init__(self, notify=None):
        self.file = None
        self._lock = threading.Lock()
        self._t0 = 0.0
        self._last = 0.0
        self._notify = notify
        self._pending = []
        self._flush_armed = False
        self._accepting = False

    def open(self, cols, rows, argv, filename_stamp=None):
        """Create the .cast file and write its v3 header. Raises on failure
        so the caller can report/roll back -- mirrors the old inline
        try/except in _Terminal.prepare()."""
        cast_dir = _cast_dir()
        makedirs_private(cast_dir)
        self._t0 = time.time()
        self._last = self._t0
        stamp = filename_stamp or time.strftime("%Y-%m-%d_%H%M%S")
        fname = f"ai_{stamp}.cast"
        path = os.path.join(cast_dir, fname)
        header = {
            "version": 3,
            "term": {
                "cols": int(cols),
                "rows": int(rows),
                "type": "xterm-256color",
            },
            "timestamp": int(self._t0),
            "title": "ai_terminal",
            # argv can carry an inline API key (a profile flag or a $secret:
            # value resolved at spawn), and this header is persisted
            # verbatim, so key-shaped words are redacted.
            "command": redact_secrets(" ".join(argv) if argv else ""),
        }
        # Do not publish the handle until a complete header is durable.  The
        # old ordering assigned self.file immediately after O_TRUNC; an
        # interrupted/failed reattach could therefore leave a zero-byte file
        # that looked like recording had started successfully.
        handle = None
        try:
            handle = open_private(path, "w", encoding="utf-8", newline="")
            handle.write(json.dumps(header) + "\n")
            handle.flush()
            # A valid header is the minimum useful cast. Make it visible and
            # durable before the terminal reader can enqueue a large broker
            # replay event; otherwise an external observer can briefly see a
            # zero-byte reattach file while that first event is being encoded.
            os.fsync(handle.fileno())
        except (OSError, TypeError, ValueError):
            print("[ai_terminal] cast open failed:\n%s" % traceback.format_exc())
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    print("[ai_terminal] cast open cleanup: close failed:\n%s"
                          % traceback.format_exc())
            try:
                if os.path.exists(path) and os.path.getsize(path) == 0:
                    os.remove(path)
            except OSError:
                print("[ai_terminal] cast open cleanup: remove failed:\n%s"
                      % traceback.format_exc())
            raise
        self.file = handle
        self._pending = []
        self._flush_armed = False
        self._accepting = True

    def _flush_pending(self):
        """The loop's flush (main thread): write every queued event in order, in one flush."""
        with self._lock:
            self._flush_armed = False
            self._drain_locked()

    def _drain_locked(self):
        """Caller holds self._lock. Event order is queue order; a burst becomes one flush."""
        handle = self.file
        items, self._pending = self._pending, []
        if handle is None or not items:
            return
        try:
            for delta, code, data in items:
                handle.write(json.dumps([delta, code, data]) + "\n")
            handle.flush()
        except Exception as failed:
            print(f"[ai_terminal] cast write failed, recording stopped: {failed}")
            try:
                handle.close()
            except OSError:
                print("[ai_terminal] cast write cleanup: close failed:\n%s"
                      % traceback.format_exc())
            self.file = None
            self._accepting = False
            if self._notify:
                self._notify("recording stopped after a write failure: %s" % failed)

    def write(self, code, data):
        """Asciicast v3 event: [delta, code, data]. delta is seconds since
        the previous event (relative timing -- v3 change from v2's absolute
        timestamps). The caller only timestamps and queues; the plugin's loop
        writes the queue out a short moment later in one flush. No-op when
        recording is off."""
        with self._lock:
            if self.file is None or not self._accepting:
                return
            now = time.time()
            delta = now - self._last
            self._last = now
            # Round to ms (v3 recommends error-diffusion for drift; simple
            # rounding is fine for sessions of realistic length).
            self._pending.append((round(delta, 3), code, data))
            if not self._flush_armed:
                self._flush_armed = True
                import sublime      # imported here so this module loads outside Sublime too
                # set_timeout is thread-safe and survives a plugin reload (the plugin's own loop does not).
                sublime.set_timeout(self._flush_pending, int(_FLUSH_DELAY_S * 1000))

    def close(self):
        with self._lock:
            if self.file is None or not self._accepting:
                return
            handle = self.file
            self._accepting = False
            now = time.time()
            delta = now - self._last
            self._last = now
            self._pending.append((round(delta, 3), "x", "0"))
            # Closing a session is the durability boundary: write every event now.
            self._drain_locked()
            try:
                if self.file is handle:
                    handle.close()
            except OSError:
                print("[ai_terminal] cast close: handle.close() failed:\n%s"
                      % traceback.format_exc())
            if self.file is handle:
                self.file = None
