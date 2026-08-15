"""Diagnostic log for live settings-change propagation (_on_settings_change).

Gated on AI_TERMINAL_DEBUG -- this path is chatty by design (traces every
terminal touched by a settings edit), so it stays off unless asked for.
"""
from .log_paths import DEBUG, append_log_line


def settings_debug_log(message):
    if not DEBUG:
        return
    append_log_line("settings_debug.log", message)
