"""Distribution invariants, independent of a developer's installed Sublime."""
import ast
import json
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = [ROOT / "ai_terminal.py"] + sorted(
    (ROOT / "terminal").glob("*.py")
) + sorted((ROOT / "tools").glob("*.py"))


def test_unpacked_install_and_python_host_are_declared():
    assert (ROOT / ".no-sublime-package").is_file()
    assert (ROOT / ".python-version").read_text().strip() == "3.8"


@pytest.mark.parametrize("path", RUNTIME_PYTHON, ids=lambda p: p.name)
def test_runtime_python_has_sublime_python_38_syntax(path):
    # Syntax compatibility only. Actual Sublime API behavior needs a live test.
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 8))


def test_channel_entry_uses_semver_tags_and_supported_platform():
    entry = json.loads((ROOT / "docs/package-control-entry.json").read_text())
    assert entry["name"] == "GhostShell"
    assert entry["details"] == "https://github.com/dpc00/GhostShell"
    assert entry["releases"] == [{
        "sublime_text": ">=4107", "platforms": ["windows-x64"], "tags": True,
    }]


def _flatten(items):
    for item in items:
        yield item
        yield from _flatten(item.get("children", []))


def test_settings_palette_and_preferences_menu_use_same_user_editor():
    commands = json.loads((ROOT / "Default.sublime-commands").read_text(encoding="utf-8"))
    menus = json.loads((ROOT / "Main.sublime-menu").read_text(encoding="utf-8"))
    palette = next(c for c in commands if c["caption"] == "GhostShell: Settings")
    preferences = next(m for m in menus if m.get("id") == "preferences")
    menu = next(m for m in _flatten([preferences]) if m.get("command") == "edit_settings")
    assert palette["command"] == menu["command"] == "edit_settings"
    assert palette["args"] == menu["args"]
    assert palette["args"]["base_file"] == "${packages}/GhostShell/ai_terminal.sublime-settings"
    assert json.loads(palette["args"]["default"].replace("$0", "")) == {}


def test_readme_relative_links_exist():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for link in re.findall(r"\]\(([^)]+)\)", readme):
        if "://" in link or link.startswith("#"):
            continue
        assert (ROOT / link.split("#", 1)[0]).is_file(), link


def test_archive_attributes_exclude_notes_but_preserve_runtime():
    excluded = [
        "ai", "tests", "GhostShell-debug.sublime-workspace",
        "GhostShell-debug.sublime-project", "temp_note.md", "ai_terminal_notes.md",
    ]
    included = [
        ".python-version", ".no-sublime-package", "ai_terminal.py",
        "ai_terminal.sublime-settings", "README.md", "LICENSE",
        "terminal/ghostty_vt.py", "terminal/GHOSTTY_LICENSE",
        "tools/agent_broker.py", "tools/agent_broker_client.py",
        "tools/recover_console.py", "tools/spawn_outside_job.ps1",
        "docs/DETACHABLE_SESSIONS.md", "docs/screenshots/supported-clis.png",
    ]
    result = subprocess.run(
        ["git", "check-attr", "export-ignore", "--"] + excluded + included,
        cwd=str(ROOT), check=True, capture_output=True, text=True,
    )
    attributes = dict(line.split(": export-ignore: ", 1) for line in result.stdout.splitlines())
    for path in excluded:
        assert attributes[path] == "set", path
    for path in included:
        assert attributes[path] == "unspecified", path


def test_native_license_is_shipped_with_pinned_provenance():
    license_text = (ROOT / "terminal/GHOSTTY_LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2024 Mitchell Hashimoto, Ghostty contributors" in license_text
    assert "The above copyright notice" in license_text
    assert "GHOSTTY_LICENSE" in (ROOT / "terminal/GHOSTTY_VT_PROVENANCE.md").read_text()
