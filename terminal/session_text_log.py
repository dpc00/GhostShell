"""Tab text log: what was just painted on the Sublime tab.

Only input is the lines of that paint. The logger does not open or read
any other file. Unchanged paints are ignored. A line is written when it
is newly present on the tab compared with the previous paint.
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
        """`lines` is the current tab paint. `now` is ignored (kept so old calls work)."""
        if self.file is None:
            return
        present = []
        for ln in lines or ():
            if not ln:
                continue
            text = ln.rstrip()
            if text.strip():
                present.append(text)
        if present == self._prev:
            return
        seen = set(self._prev)
        self._prev = present
        for line in present:
            if line not in seen:
                self.write_line(line)

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
