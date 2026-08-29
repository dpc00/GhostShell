"""A live text snapshot of what was last painted on the Sublime tab.

Each paint already contains the complete rendered tab (scrollback and live
screen).  Keep that snapshot verbatim instead of trying to turn successive
frames into an append-only transcript: appending records every input edit,
spinner frame, and status-line redraw that the user only saw temporarily.
"""
import os
import threading

from .log_paths import LOG_ROOT, makedirs_private, open_private

TEXT_LOG_DIR = os.path.join(LOG_ROOT, "ai_terminal_session_text_logs")


class SessionTextLog:
    def __init__(self):
        self.file = None
        self._path = None
        self._prev = []
        self._last_written = None
        self._lock = threading.Lock()

    def open(self, filename_stamp):
        makedirs_private(TEXT_LOG_DIR)
        path = os.path.join(TEXT_LOG_DIR, "ai_%s.log" % filename_stamp)
        handle = open_private(path, "a", encoding="utf-8", newline="\n")
        with self._lock:
            self.file = handle
            self._path = path
            self._prev = []
            self._last_written = None

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

    def observe(self, lines, now=None):
        """Replace the log with the current tab paint.

        ``now`` remains accepted for compatibility with older callers.
        Blank lines and horizontal spacing are significant parts of the paint.
        """
        present = ["" if line is None else str(line) for line in (lines or ())]
        with self._lock:
            if self.file is None or present == self._prev:
                return
            path = self._path
            temp_path = path + ".tmp"
            snapshot = "\n".join(present) + "\n" if present else ""
            replacement = None
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
                os.replace(temp_path, path)
                self.file = open_private(path, "a", encoding="utf-8", newline="\n")
            except PermissionError:
                # Windows refuses os.replace() while some readers keep the
                # destination open without FILE_SHARE_DELETE. Keep logging
                # usable for tailers by falling back to the old in-place
                # rewrite in that specific case.
                fallback = None
                try:
                    fallback = open_private(
                        path, "w", encoding="utf-8", newline="\n"
                    )
                    fallback.write(snapshot)
                    fallback.flush()
                    fallback.close()
                    fallback = None
                    os.remove(temp_path)
                    self.file = open_private(
                        path, "a", encoding="utf-8", newline="\n"
                    )
                except Exception:
                    if fallback is not None:
                        try:
                            fallback.close()
                        except Exception:
                            pass
                    try:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                    except Exception:
                        pass
                    raise
            except Exception:
                if replacement is not None:
                    try:
                        replacement.close()
                    except Exception:
                        pass
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass
                if self.file is None:
                    try:
                        self.file = open_private(
                            path, "a", encoding="utf-8", newline="\n"
                        )
                    except Exception:
                        pass
                raise
            self._prev = present
            self._last_written = present[-1] if present else None

    def flush_live_lines(self, lines):
        self.observe(lines)

    def flush_held(self, force=True, now=None):
        return

    def close(self):
        with self._lock:
            if self.file is None:
                return
            try:
                self.file.close()
            except Exception:
                pass
            self.file = None
            self._path = None
            self._prev = []
