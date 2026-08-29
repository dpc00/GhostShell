"""A live text snapshot of what was last painted on the Sublime tab.

Each paint already contains the complete rendered tab (scrollback and live
screen).  Keep that snapshot verbatim instead of trying to turn successive
frames into an append-only transcript: appending records every input edit,
spinner frame, and status-line redraw that the user only saw temporarily.
"""
import os

from .log_paths import LOG_ROOT, makedirs_private, open_private

TEXT_LOG_DIR = os.path.join(LOG_ROOT, "ai_terminal_session_text_logs")


class SessionTextLog:
    def __init__(self):
        self.file = None
        self._prev = []
        self._last_written = None

    def open(self, filename_stamp):
        makedirs_private(TEXT_LOG_DIR)
        path = os.path.join(TEXT_LOG_DIR, "ai_%s.log" % filename_stamp)
        self.file = open_private(path, "a", encoding="utf-8", newline="\n")
        self._prev = []
        self._last_written = None

    def write_line(self, text):
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
        if self.file is None:
            return
        present = ["" if line is None else str(line) for line in (lines or ())]
        if present == self._prev:
            return
        self._prev = present
        self.file.seek(0)
        self.file.truncate()
        if present:
            self.file.write("\n".join(present) + "\n")
        self.file.flush()
        self._last_written = present[-1] if present else None

    def flush_live_lines(self, lines):
        self.observe(lines)

    def flush_held(self, force=True, now=None):
        return

    def close(self):
        if self.file is None:
            return
        try:
            self.file.close()
        except Exception:
            pass
        self.file = None
        self._prev = []
