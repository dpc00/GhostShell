"""Tests for the launcher row-formatting helpers (pure, no Sublime)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai.terminal import launcher  # noqa: E402


NOW = 1_800_000_000.0
DAY = 86400.0


def test_profile_kind_distinguishes_states():
    assert launcher.profile_kind("Claude")[2] == "Agent"
    assert launcher.profile_kind("Bash")[2] == "Shell"
    assert launcher.profile_kind("Claude", available=False)[2] == "Not installed"
    assert launcher.profile_kind("Claude", exhausted=True)[2] == "Quota exhausted"


def test_unavailable_beats_exhausted_in_kind():
    kind = launcher.profile_kind("Claude", available=False, exhausted=True)
    assert kind[2] == "Not installed"


def test_shorten_path_uses_tilde():
    home = os.path.normpath("/home/d")
    assert launcher.shorten_path(home, home=home) == "~"
    child = os.path.join(home, "projects")
    assert launcher.shorten_path(child, home=home) == "~" + os.sep + "projects"
    assert launcher.shorten_path("/elsewhere/x", home=home) == "/elsewhere/x"


def test_shorten_path_empty():
    assert launcher.shorten_path("") == ""


def test_relative_age_buckets():
    assert launcher.relative_age(0) == "never"
    assert launcher.relative_age(NOW - 5, NOW) == "just now"
    assert launcher.relative_age(NOW - 300, NOW) == "5m ago"
    assert launcher.relative_age(NOW - 7200, NOW) == "2h ago"
    assert launcher.relative_age(NOW - 3 * DAY, NOW) == "3d ago"


def test_dir_kind_states():
    assert launcher.dir_kind(is_recent=True)[2] == "Recent"
    assert launcher.dir_kind(is_git=True)[2] == "Repository"
    assert launcher.dir_kind()[2] == "Folder"


def test_browse_kind_is_distinct_from_dir_kinds():
    assert launcher.BROWSE_KIND != launcher.dir_kind(is_recent=True)
    assert launcher.BROWSE_KIND != launcher.dir_kind()
