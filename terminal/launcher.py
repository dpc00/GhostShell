"""Row helpers for the history list in ai_terminal (pure, unit-testable).

No Sublime imports: ai_terminal supplies the data and renders the rows.
"""

import time

# Sublime kind tuples are (KIND_ID, letter, display-name). The id is inlined so this
# module stays importable without Sublime for tests.
KIND_ID_NAVIGATION = 5

# Shells are listed apart from agents on the Ai Terminal menus.
SHELL_PROFILES = ("Bash", "PowerShell", "Dos Console", "WSL Bash")


def relative_age(seconds, now=None):
    """Compact age string for a timestamp, e.g. 'just now', '3m ago', '5d ago'."""
    if not seconds:
        return "never"
    now = time.time() if now is None else now
    delta = max(0, int(now - seconds))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return "%dm ago" % (delta // 60)
    if delta < 86400:
        return "%dh ago" % (delta // 3600)
    return "%dd ago" % (delta // 86400)
