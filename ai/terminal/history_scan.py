"""Live, read-only sweep of local AI-agent history files (pure, unit-testable).

Unlike the old launcher frecency store, nothing here is written to disk —
every call walks the filesystem fresh, so there is no local record for this
module to leak or to keep in sync. Callers pass in the base directories
(home, %LOCALAPPDATA%) so this stays testable without patching os.path.

No Sublime imports: ai_terminal renders the rows.
"""

import glob
import os
import sqlite3
import time


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def scan_claude_code(home):
    """~/.claude/projects/<encoded-path>/<session-id>.jsonl, newest first."""
    base = os.path.join(home, ".claude", "projects")
    sessions = []
    for path in glob.glob(os.path.join(base, "*", "*.jsonl")):
        project = os.path.basename(os.path.dirname(path))
        sessions.append({
            "agent": "Claude Code",
            "title": project,
            "detail": os.path.splitext(os.path.basename(path))[0],
            "path": path,
            "mtime": _mtime(path),
            "kind": "text",
        })
    return sessions


def scan_codex(home):
    """~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl, newest first."""
    base = os.path.join(home, ".codex", "sessions")
    sessions = []
    for path in glob.glob(os.path.join(base, "*", "*", "*", "*.jsonl")):
        sessions.append({
            "agent": "Codex",
            "title": os.path.basename(path),
            "detail": "",
            "path": path,
            "mtime": _mtime(path),
            "kind": "text",
        })
    return sessions


def scan_gemini(home):
    """~/.gemini/*/conversations/*.db (Antigravity + Antigravity CLI)."""
    sessions = []
    for variant in ("antigravity", "antigravity-cli"):
        base = os.path.join(home, ".gemini", variant, "conversations")
        for path in glob.glob(os.path.join(base, "*.db")):
            sessions.append({
                "agent": "Gemini (%s)" % variant,
                "title": os.path.splitext(os.path.basename(path))[0],
                "detail": "",
                "path": path,
                "mtime": _mtime(path),
                "kind": "sqlite",
            })
    return sessions


def _read_ollama_chats(db_path):
    conn = sqlite3.connect(
        "file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True
    )
    try:
        return conn.execute(
            "SELECT id, title, created_at FROM chats ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()


def scan_ollama(localappdata):
    """%LOCALAPPDATA%\\Ollama\\db.sqlite chats table."""
    db_path = os.path.join(localappdata, "Ollama", "db.sqlite")
    if not os.path.isfile(db_path):
        return []
    try:
        rows = _read_ollama_chats(db_path)
    except sqlite3.Error:
        return []
    sessions = []
    for chat_id, title, created_at in rows:
        try:
            mtime = float(created_at)
        except (TypeError, ValueError):
            mtime = _mtime(db_path)
        sessions.append({
            "agent": "Ollama",
            "title": title or "(untitled chat)",
            "detail": str(chat_id),
            "path": db_path,
            "mtime": mtime,
            "kind": "sqlite",
        })
    return sessions


def scan_all(home=None, localappdata=None, limit=60):
    """Merge every known agent's history, newest first, capped at *limit*."""
    home = home or os.path.expanduser("~")
    localappdata = localappdata or os.environ.get("LOCALAPPDATA", "")
    sessions = []
    for scanner, base in (
        (scan_claude_code, home),
        (scan_codex, home),
        (scan_gemini, home),
    ):
        try:
            sessions.extend(scanner(base))
        except OSError:
            pass
    if localappdata:
        try:
            sessions.extend(scan_ollama(localappdata))
        except OSError:
            pass
    sessions.sort(key=lambda s: s.get("mtime") or 0.0, reverse=True)
    return sessions[:limit]
