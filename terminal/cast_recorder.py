"""Per-session asciicast v3 recorder.

Recording at the stream layer (not scroll-off) means it is faithful to what
the child process emitted and does NOT duplicate on resume (a resume is a
new session = new .cast). One file per session (timestamped filename), not
per day, so a resume's replay is a separate recording rather than appended
duplicates.
"""
import json
import os
import queue
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
        self._queue = None
        self._writer = None
        self._accepting = False

    def open(self, cols, rows, argv, filename_stamp=None):
        """Create the .cast file and write its v3 header. Raises on failure
        so the caller can report/roll back -- mirrors the old inline
        try/except in _Terminal.prepare()."""
        makedirs_private(CAST_DIR)
        self._t0 = time.time()
        self._last = self._t0
        stamp = filename_stamp or time.strftime("%Y-%m-%d_%H%M%S")
        fname = f"ai_{stamp}.cast"
        path = os.path.join(CAST_DIR, fname)
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
        except Exception:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
            try:
                if os.path.exists(path) and os.path.getsize(path) == 0:
                    os.remove(path)
            except Exception:
                pass
            raise
        work_queue = queue.Queue()
        writer = threading.Thread(
            target=self._write_loop,
            args=(handle, work_queue),
            name="ai-terminal-cast-writer",
            daemon=True,
        )
        self.file = handle
        self._queue = work_queue
        self._writer = writer
        self._accepting = True
        writer.start()

    def _write_loop(self, handle, work_queue):
        """Serialize and flush queued events without blocking the PTY reader."""
        failed = None
        stop = False
        try:
            while not stop:
                item = work_queue.get()
                batch = [item]
                # Coalesce a burst into one flush. Queue order is event order,
                # and the close sentinel is ordered behind the final x event.
                while True:
                    try:
                        batch.append(work_queue.get_nowait())
                    except queue.Empty:
                        break
                for item in batch:
                    if item is None:
                        stop = True
                        break
                    delta, code, data = item
                    handle.write(json.dumps([delta, code, data]) + "\n")
                handle.flush()
        except Exception as error:
            failed = error
        finally:
            if failed is not None:
                print(f"[ai_terminal] cast write failed, recording stopped: {failed}")
                try:
                    handle.close()
                except Exception:
                    pass
                with self._lock:
                    if self.file is handle:
                        self.file = None
                        self._accepting = False
                if self._notify:
                    self._notify("recording stopped after a write failure: %s" % failed)

    def write(self, code, data):
        """Asciicast v3 event: [delta, code, data]. delta is seconds since
        the previous event (relative timing -- v3 change from v2's absolute
        timestamps). The PTY thread only timestamps and enqueues; a dedicated
        writer serializes events and coalesces burst flushes. No-op when
        recording is off."""
        with self._lock:
            if self.file is None or not self._accepting:
                return
            now = time.time()
            delta = now - self._last
            self._last = now
            # Round to ms (v3 recommends error-diffusion for drift; simple
            # rounding is fine for sessions of realistic length).
            self._queue.put((round(delta, 3), code, data))

    def close(self):
        with self._lock:
            if self.file is None or not self._accepting:
                return
            handle = self.file
            writer = self._writer
            work_queue = self._queue
            self._accepting = False
            now = time.time()
            delta = now - self._last
            self._last = now
            work_queue.put((round(delta, 3), "x", "0"))
            work_queue.put(None)
        # Closing a session is the durability boundary: drain every event
        # before returning, but never make ordinary PTY reads wait on disk.
        writer.join()
        with self._lock:
            try:
                if self.file is handle:
                    handle.close()
            except Exception:
                pass
            if self.file is handle:
                self.file = None
            self._writer = None
            self._queue = None
