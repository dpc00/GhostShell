"""Most recently launched agents: remembered, deduplicated, capped, and ordered first."""
import json

from terminal import recent_profiles


def test_missing_file_gives_an_empty_list(tmp_path):
    assert recent_profiles.load(str(tmp_path / "absent.json")) == []


def test_damaged_or_wrong_shape_file_gives_an_empty_list(tmp_path):
    for content in ("not json", '{"a": 1}', "[1, 2]", "null"):
        path = tmp_path / "recent.json"
        path.write_text(content, encoding="utf-8")
        assert recent_profiles.load(str(path)) == []


def test_record_puts_the_newest_first_and_creates_the_folder(tmp_path):
    path = str(tmp_path / "cache" / "GhostShell" / "recent.json")
    assert recent_profiles.record(path, "Claude") == ["Claude"]
    assert recent_profiles.record(path, "Codex") == ["Codex", "Claude"]
    assert recent_profiles.load(path) == ["Codex", "Claude"]


def test_relaunching_moves_an_agent_to_the_front_without_duplicating_it(tmp_path):
    path = str(tmp_path / "recent.json")
    for name in ("A", "B", "C", "B"):
        recent_profiles.record(path, name)
    assert recent_profiles.load(path) == ["B", "C", "A"]


def test_the_list_and_the_file_are_capped(tmp_path):
    path = str(tmp_path / "recent.json")
    for number in range(20):
        recent_profiles.record(path, "agent%d" % number)
    names = recent_profiles.load(path)
    assert len(names) == recent_profiles.DEFAULT_LIMIT
    assert names[0] == "agent19"
    assert (tmp_path / "recent.json").stat().st_size < 1024


def test_no_temporary_files_are_left_behind(tmp_path):
    path = str(tmp_path / "recent.json")
    recent_profiles.record(path, "Claude")
    assert [p.name for p in tmp_path.iterdir()] == ["recent.json"]
    assert json.loads((tmp_path / "recent.json").read_text(encoding="utf-8")) == ["Claude"]


def test_order_puts_recent_first_then_the_rest_alphabetically():
    names = ["Bash", "claude", "Codex", "Amp"]
    assert recent_profiles.order(names, ["Codex", "Bash"]) == ["Codex", "Bash", "Amp", "claude"]


def test_order_skips_a_recent_agent_that_is_no_longer_offered():
    assert recent_profiles.order(["Amp", "Bash"], ["Cody", "Bash"]) == ["Bash", "Amp"]


def test_order_with_nothing_recent_is_plain_alphabetical():
    assert recent_profiles.order(["b", "A", "c"], []) == ["A", "b", "c"]
