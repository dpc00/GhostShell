"""Raw pre-decode PTY byte log, gated on AI_TERMINAL_DEBUG.

Distinct from the .cast recording: this captures exactly what read() handed
back before any UTF-8 decode or VT parsing, for diagnosing rendering bugs
where the parser/decoder pipeline itself is under suspicion.
"""
import os
import threading
import traceback

from .log_paths import log_root, makedirs_private, open_private

_debug_lock = threading.Lock()


def _debug_path():
    return os.path.join(log_root(), "raw_ansi_stream_debug_logs")


def debug_log(data):
    try:
        debug_path = _debug_path()
        makedirs_private(debug_path)
        with open_private(os.path.join(debug_path, "raw.log"), "ab") as f:
            with _debug_lock:
                f.write(data)
    except OSError:
        print("[ai_terminal] raw debug_log write failed:\n%s" % traceback.format_exc())
