"""Raw pre-decode PTY byte log, gated on AI_TERMINAL_DEBUG.

Distinct from the .cast recording: this captures exactly what read() handed
back before any UTF-8 decode or VT parsing, for diagnosing rendering bugs
where the parser/decoder pipeline itself is under suspicion.
"""
import os
import threading

from .log_paths import LOG_ROOT, makedirs_private, open_private

DEBUG_PATH = os.path.join(LOG_ROOT, "ai_terminal_raw_ansi_stream_debug_logs")
_debug_lock = threading.Lock()


def debug_log(data):
    try:
        makedirs_private(DEBUG_PATH)
        with open_private(os.path.join(DEBUG_PATH, "raw.log"), "ab") as f:
            with _debug_lock:
                f.write(data)
    except Exception:
        pass
