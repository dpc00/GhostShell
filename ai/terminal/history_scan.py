"""Live, read-only sweep of local AI-agent history files (pure, unit-testable).

Nothing here is written to disk — every call walks the filesystem fresh, so
there is no local record for this module to leak or to keep in sync. Callers
pass in the base directories (home, %LOCALAPPDATA%) so this stays testable
without patching os.path.

Adding a new agent (T3, a reinstalled jcode, whatever comes next) should be a
registry entry, not new code:

  - Plain file-per-session agents (jsonl/json/log under a glob) go in
    _GLOB_SOURCES: one dict with a glob pattern and two tiny label functions.
  - Agents with their own on-disk format (a shared sqlite db holding many
    sessions, a custom index file, ...) get a small reader function and an
    entry in _CUSTOM_SCANNERS.

Either way, `scan_all` does not change.

No Sublime imports: ai_terminal renders the rows.
"""

import calendar
import glob
import os
import sqlite3
import time


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _parse_iso_to_epoch(text, fallback=0.0):
    """T3's timestamps are ISO 8601 UTC ('...Z' or space-separated, with or
    without fractional seconds); everything else here uses unix seconds, so
    normalize at the read site rather than carrying two time formats around.
    """
    if not text:
        return fallback
    text = text.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return calendar.timegm(time.strptime(text, fmt))
        except ValueError:
            continue
    return fallback


# ─── glob-based sources: one file per session ─────────────────────────────────
#
# "base" picks which root scan_all() hands to `pattern`: "home" (~) or
# "localappdata" (%LOCALAPPDATA%). `title`/`detail` take the matched path and
# return short label strings for the quick-panel row.

_GLOB_SOURCES = [
    {
        "agent": "Claude Code",
        "kind": "text",
        "base": "home",
        "pattern": lambda base: os.path.join(base, ".claude", "projects", "*", "*.jsonl"),
        "title": lambda path: os.path.basename(os.path.dirname(path)),
        "detail": lambda path: os.path.splitext(os.path.basename(path))[0],
    },
    {
        "agent": "Codex",
        "kind": "text",
        "base": "home",
        "pattern": lambda base: os.path.join(
            base, ".codex", "sessions", "*", "*", "*", "*.jsonl"
        ),
        "title": lambda path: os.path.basename(path),
        "detail": lambda path: "",
    },
    {
        "agent": "Gemini (antigravity)",
        "kind": "sqlite",
        "base": "home",
        "pattern": lambda base: os.path.join(
            base, ".gemini", "antigravity", "conversations", "*.db"
        ),
        "title": lambda path: os.path.splitext(os.path.basename(path))[0],
        "detail": lambda path: "",
    },
    {
        "agent": "Gemini (antigravity-cli)",
        "kind": "sqlite",
        "base": "home",
        "pattern": lambda base: os.path.join(
            base, ".gemini", "antigravity-cli", "conversations", "*.db"
        ),
        "title": lambda path: os.path.splitext(os.path.basename(path))[0],
        "detail": lambda path: "",
    },
]


def _scan_glob_source(source, base):
    sessions = []
    for path in glob.glob(source["pattern"](base)):
        sessions.append({
            "agent": source["agent"],
            "title": source["title"](path),
            "detail": source["detail"](path),
            "path": path,
            "mtime": _mtime(path),
            "kind": source["kind"],
        })
    return sessions


# ─── custom sources: one file/db holding many sessions ────────────────────────


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


def _read_t3_threads(db_path):
    conn = sqlite3.connect(
        "file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True
    )
    try:
        return conn.execute(
            """
            SELECT t.thread_id, t.title, t.updated_at, s.provider_name
            FROM projection_threads t
            LEFT JOIN projection_thread_sessions s ON s.thread_id = t.thread_id
            WHERE t.deleted_at IS NULL
            ORDER BY t.updated_at DESC
            """
        ).fetchall()
    finally:
        conn.close()


def scan_t3(home):
    """~/.t3/userdata/state.sqlite — T3 Code's own multi-provider threads."""
    db_path = os.path.join(home, ".t3", "userdata", "state.sqlite")
    if not os.path.isfile(db_path):
        return []
    try:
        rows = _read_t3_threads(db_path)
    except sqlite3.Error:
        return []
    sessions = []
    for thread_id, title, updated_at, provider_name in rows:
        sessions.append({
            "agent": "T3 (%s)" % provider_name if provider_name else "T3",
            "title": title or "(untitled thread)",
            "detail": thread_id,
            "path": db_path,
            "mtime": _parse_iso_to_epoch(updated_at, fallback=_mtime(db_path)),
            "kind": "sqlite",
        })
    return sessions


# base is "home" or "localappdata", matching the keys scan_all() builds below.
_CUSTOM_SCANNERS = [
    ("localappdata", scan_ollama),
    ("home", scan_t3),
]


def scan_all(home=None, localappdata=None, limit=60):
    """Merge every registered agent's history, newest first, capped at *limit*."""
    bases = {
        "home": home or os.path.expanduser("~"),
        "localappdata": localappdata or os.environ.get("LOCALAPPDATA", ""),
    }
    sessions = []
    for source in _GLOB_SOURCES:
        base = bases.get(source["base"])
        if not base:
            continue
        try:
            sessions.extend(_scan_glob_source(source, base))
        except OSError:
            pass
    for base_key, scanner in _CUSTOM_SCANNERS:
        base = bases.get(base_key)
        if not base:
            continue
        try:
            sessions.extend(scanner(base))
        except OSError:
            pass
    sessions.sort(key=lambda s: s.get("mtime") or 0.0, reverse=True)
    return sessions[:limit]
