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

from .log_paths import LOG_ROOT, makedirs_private, open_private, redact_secrets

CAST_DIR = os.path.join(
    LOG_ROOT, "ai_terminal_asciinema_casts_for_troubleshooting_rendering"
)


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

    def open(self, cols, rows, argv):
        """Create the .cast file and write its v3 header. Raises on failure
        so the caller can report/roll back -- mirrors the old inline
        try/except in _Terminal.prepare()."""
        makedirs_private(CAST_DIR)
        self._t0 = time.time()
        self._last = self._t0
        fname = f"ai_{time.strftime('%Y-%m-%d_%H%M%S')}.cast"
        path = os.path.join(CAST_DIR, fname)
        self.file = open_private(path, "w", encoding="utf-8", newline="")
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
        self.file.write(json.dumps(header) + "\n")
        self.file.flush()

    def write(self, code, data):
        """Asciicast v3 event: [delta, code, data]. delta is seconds since
        the previous event (relative timing -- v3 change from v2's absolute
        timestamps). One write per event under the lock; flush so a crash
        doesn't truncate the file. No-op when recording is off."""
        if self.file is None:
            return
        now = time.time()
        delta = now - self._last
        self._last = now
        # Round to ms (v3 spec recommends error-diffusion for drift; simple
        # rounding is fine for sessions of realistic length).
        line = json.dumps([round(delta, 3), code, data])
        with self._lock:
            try:
                self.file.write(line + "\n")
                self.file.flush()
            except Exception as e:
                # A recorder that keeps dropping events writes a .cast that
                # replays as a shorter, wrong session; stop and say so
                # instead.
                print(f"[ai_terminal] cast write failed, recording stopped: {e}")
                try:
                    self.file.close()
                except Exception:
                    pass
                self.file = None
                if self._notify:
                    self._notify("recording stopped after a write failure: %s" % e)

    def close(self):
        if self.file is None:
            return
        self.write("x", "0")
        with self._lock:
            try:
                if self.file is not None:
                    self.file.close()
            except Exception:
                pass
        self.file = None
