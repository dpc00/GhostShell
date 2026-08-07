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

from ai.terminal import launcher  # noqa: E402
from ai.terminal import history_scan  # noqa: E402


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
