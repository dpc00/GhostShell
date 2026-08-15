"""Diagnostic log for the dynamic color-scheme init/save/repair pipeline."""
from .log_paths import append_log_line


def color_scheme_log(message):
    append_log_line("color_scheme.log", message)
