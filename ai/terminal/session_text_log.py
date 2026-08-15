"""Plain-text, agent-readable transcript of one terminal tab.

Counterpart to CastRecorder's .cast file -- same timestamped-per-session
naming so a .cast and its .log pair up, but content is clean rendered text,
not raw ANSI events. One line is appended the instant it permanently retires
from the live viewport (never a duplicate, never a raw ANSI byte); whatever's
still on-screen is flushed on close.
"""
import os

from .log_paths import LOG_ROOT, makedirs_private, open_private

TEXT_LOG_DIR = os.path.join(LOG_ROOT, "ai_terminal_session_text_logs")


class SessionTextLog:
    """Owns one text-log file for one terminal session.

    Raises on write failure rather than swallowing it -- callers (which know
    about the screen callback and notify path) decide how to recover.
    """

    def __init__(self):
        self.file = None

    def open(self, filename_stamp):
        makedirs_private(TEXT_LOG_DIR)
        path = os.path.join(TEXT_LOG_DIR, f"ai_{filename_stamp}.log")
        self.file = open_private(path, "a", encoding="utf-8", newline="\n")

    def write_line(self, text):
        """Append-and-flush so a crash loses at most the current in-flight
        write, not the session."""
        self.file.write(text + "\n")
        self.file.flush()

    def flush_live_lines(self, lines):
        """Write whatever never scrolled off (the final live screen)."""
        for line in lines:
            if line:
                self.file.write(line + "\n")
        self.file.flush()

    def close(self):
        if self.file is None:
            return
        try:
            self.file.close()
        except Exception:
            pass
        self.file = None
