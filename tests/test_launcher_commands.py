"""Smoke tests for the launcher/history command call shapes against pure code.

`ai_terminal.py` cannot be imported outside Sublime (it touches ctypes/conpty at
import time in ways that vary by host), so these tests exercise the small pure
parts the commands depend on and assert the launcher/history_scan APIs are
called the way `ai_terminal.py` calls them. That keeps the signatures honest:
a rename in launcher.py or history_scan.py fails the suite rather than failing
silently at runtime inside Sublime, where the only symptom is a quiet console
traceback.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from terminal import launcher  # noqa: E402
from terminal import history_scan  # noqa: E402


def test_profile_items_call_shape():
    """Mirrors _profile_items in ai_terminal.py: alphabetical, no store."""
    names = ["Claude", "Codex", "Bash"]
    ordered = sorted(names, key=lambda n: n.lower())
    assert ordered == ["Bash", "Claude", "Codex"]
    for name in ordered:
        kind = launcher.profile_kind(name, available=True, exhausted=False)
        assert len(kind) == 3


def test_dir_items_call_shape():
    """Mirrors _dir_items in ai_terminal.py: sidebar folders + Browse…."""
    folders = [os.path.join("C:", os.sep, "proj", "alpha")]
    rows = [launcher.shorten_path(p) for p in folders]
    rows.append("Browse…")
    assert rows[-1] == "Browse…"
    for path in folders:
        launcher.dir_kind(is_git=os.path.isdir(os.path.join(path, ".git")))


def test_history_scan_call_shape(tmp_path, monkeypatch):
    """Mirrors AiTerminalHistoryCommand: scan_all() rows feed straight into
    _quick_panel_item(title, detail, annotation, kind) without extra lookups.
    """
    home = tmp_path / "home"
    (home / ".claude" / "projects" / "proj").mkdir(parents=True)
    (home / ".claude" / "projects" / "proj" / "abc.jsonl").write_text("{}")

    sessions = history_scan.scan_all(home=str(home), localappdata=str(tmp_path))
    assert sessions and sessions[0]["agent"] == "Claude Code"
    for sess in sessions:
        assert isinstance(sess["title"], str)
        assert isinstance(launcher.relative_age(sess["mtime"]), str)
        assert sess["kind"] in ("text", "sqlite")


def test_parse_iso_to_epoch_handles_t3s_formats():
    # 't' variant Sublime Text ships (3.8) has no datetime.fromisoformat('Z' suffix)
    # support, which is why history_scan rolls its own parser for T3's timestamps.
    assert history_scan._parse_iso_to_epoch("2026-08-07T21:25:38.197Z") > 0
    assert history_scan._parse_iso_to_epoch("2026-08-07T21:25:38Z") > 0
    assert history_scan._parse_iso_to_epoch("2026-08-07 21:25:34") > 0
    assert history_scan._parse_iso_to_epoch("") == 0.0
    assert history_scan._parse_iso_to_epoch(None, fallback=5.0) == 5.0
    assert history_scan._parse_iso_to_epoch("garbage", fallback=5.0) == 5.0


def test_scan_t3_reads_threads_joined_with_provider(tmp_path):
    import sqlite3

    db_path = tmp_path / "home" / ".t3" / "userdata" / "state.sqlite"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE projection_threads (thread_id TEXT, title TEXT, "
        "updated_at TEXT, deleted_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE projection_thread_sessions (thread_id TEXT, provider_name TEXT)"
    )
    conn.execute(
        "INSERT INTO projection_threads VALUES ('t1', 'Fix the bug', "
        "'2026-08-07T21:25:38.197Z', NULL)"
    )
    conn.execute(
        "INSERT INTO projection_thread_sessions VALUES ('t1', 'claude')"
    )
    conn.commit()
    conn.close()

    sessions = history_scan.scan_t3(str(tmp_path / "home"))
    assert len(sessions) == 1
    assert sessions[0]["agent"] == "T3 (claude)"
    assert sessions[0]["title"] == "Fix the bug"
    assert sessions[0]["kind"] == "sqlite"
    assert sessions[0]["mtime"] > 0
