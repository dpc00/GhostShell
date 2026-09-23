"""Shared on-disk log-file primitives for ai_terminal's various loggers.

Everything under LOG_ROOT is a verbatim record of an agent session: raw ANSI,
rendered transcripts, keystrokes. That routinely contains API keys, OAuth
codes and source code, so the tree is created owner-only and every log file
is opened 0600 (a no-op on Windows ACLs, load-bearing on POSIX).
"""
import os
import re
import threading
import time
import traceback

_log_root_override = None
DEBUG = bool(os.environ.get("AI_TERMINAL_DEBUG"))


def log_root():
    """Where GhostShell's own logs live. Set entirely by the "log_root"
    setting in ai_terminal.sublime-settings -- no fallback path is picked
    in code. Raises if the setting is empty or was never applied, so a
    blank setting fails loudly instead of writing somewhere unreviewed."""
    if not _log_root_override:
        raise RuntimeError(
            'The "log_root" setting in ai_terminal.sublime-settings is '
            "empty -- GhostShell does not choose a log location on its "
            "own. Set it to a real path."
        )
    return _log_root_override


def configure_log_root(path):
    """Apply the "log_root" setting's current value. Called at plugin load
    and on every settings change; an empty/missing path leaves log_root()
    raising until a real one is set."""
    global _log_root_override
    _log_root_override = (
        os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
        if path else None
    )


def makedirs_private(path):
    os.makedirs(path, mode=0o700, exist_ok=True)


def open_private(path, mode="w", **kwargs):
    """open() that creates the file readable/writable by its owner only."""
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_APPEND if "a" in mode else os.O_TRUNC
    return os.fdopen(os.open(path, flags, 0o600), mode, **kwargs)


def append_log_line(filename, message):
    """Append one timestamped, thread-tagged line to a diagnostic log.

    Diagnostics only: every failure (unwritable path, disk full) is swallowed,
    since losing a log line must never break a render or a settings reload.
    """
    try:
        path = os.path.join(log_root(), filename)
        makedirs_private(os.path.dirname(path))
        with open_private(path, "a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            t_name = threading.current_thread().name
            f.write(f"[{ts}] [{t_name}] {message}\n")
    except OSError:
        # Can't write to our own log file -- fall back to stdout so the
        # failure is still visible instead of silently vanishing.
        print("[ai_terminal] append_log_line failed:\n%s" % traceback.format_exc())


_SECRET_NAME = r"[A-Za-z0-9_\-]*(?:key|token|secret|password)[A-Za-z0-9_\-]*"
_SECRET_SUBSTITUTIONS = (
    # Bare key material (OpenAI/Anthropic/OpenRouter shapes).
    (re.compile(r"(?i)\bsk-[A-Za-z0-9_\-]{8,}"), "***"),
    # NAME=value / NAME: value.
    (re.compile(r"(?i)(%s\s*[=:]\s*)\S+" % _SECRET_NAME), r"\1***"),
    # --api-key value.
    (re.compile(r"(?i)(--?%s\s+)\S+" % _SECRET_NAME), r"\1***"),
)


def redact_secrets(text):
    """Blank out key-shaped substrings so they are not persisted to a log."""
    text = text or ""
    for pattern, replacement in _SECRET_SUBSTITUTIONS:
        text = pattern.sub(replacement, text)
    return text
