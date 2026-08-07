"""Quick-panel row formatting for ai_terminal (pure, unit-testable).

No Sublime imports: ai_terminal supplies paths/settings and renders the rows.
"""

import os
import time


# ─── row presentation ────────────────────────────────────────────────────────

# Sublime kind tuples are (KIND_ID, letter, display-name). The numeric ids are
# inlined so this module stays importable without Sublime for tests.
KIND_ID_AMBIGUOUS = 0
KIND_ID_KEYWORD = 1
KIND_ID_TYPE = 2
KIND_ID_FUNCTION = 3
KIND_ID_NAMESPACE = 4
KIND_ID_NAVIGATION = 5
KIND_ID_MARKUP = 6
KIND_ID_VARIABLE = 7
KIND_ID_SNIPPET = 8

# Shells are deliberately a different colour/letter from agents so the two
# groups are separable at a glance even though they share one list.
SHELL_PROFILES = ("Bash", "PowerShell", "Dos Console", "WSL Bash")


def profile_kind(name, available=True, exhausted=False, shells=SHELL_PROFILES):
    """(kind_id, letter, label) describing one profile row."""
    if not available:
        return (KIND_ID_AMBIGUOUS, "x", "Not installed")
    if exhausted:
        return (KIND_ID_VARIABLE, "!", "Quota exhausted")
    if name in shells:
        return (KIND_ID_NAMESPACE, "$", "Shell")
    return (KIND_ID_FUNCTION, "A", "Agent")


def dir_kind(is_recent=False, is_git=False):
    if is_recent:
        return (KIND_ID_SNIPPET, "R", "Recent")
    if is_git:
        return (KIND_ID_NAVIGATION, "G", "Repository")
    return (KIND_ID_TYPE, "D", "Folder")


def shorten_path(path, home=None):
    """Display form for a path: ~ for home, and no trailing separator."""
    if not path:
        return ""
    home = os.path.expanduser("~") if home is None else home
    text = path.rstrip("\\/") or path
    if home:
        home_n = os.path.normcase(home.rstrip("\\/"))
        text_n = os.path.normcase(text)
        if text_n == home_n:
            return "~"
        if text_n.startswith(home_n + os.sep):
            return "~" + text[len(home):]
    return text


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


# The Browse… escape hatch reads as an action, not a folder, so it gets its own
# kind rather than borrowing dir_kind.
BROWSE_KIND = (KIND_ID_KEYWORD, "+", "Browse")
