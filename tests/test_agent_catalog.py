"""Generated profiles retain the catalog's learned terminal behavior."""

from terminal.agent_catalog import CATALOG, profile_from_entry
from terminal.profile_schema import validate_profiles


def test_every_catalog_agent_generates_a_detachable_valid_profile():
    for command, entry in CATALOG.items():
        profile = profile_from_entry(entry)
        assert profile["detachable"] is True, command
        errors, _warnings = validate_profiles({entry["display_name"]: profile})
        assert errors == [], (command, errors)


def test_page_key_owners_keep_their_keys_in_generated_profiles():
    expected = {"agy", "grok", "jcode", "mimo", "opencode", "qwen"}
    actual = {
        command for command, entry in CATALOG.items()
        if profile_from_entry(entry).get("page_keys_to_pty")
    }
    assert actual == expected


def test_catalog_metadata_and_legacy_log_env_are_not_generated_settings():
    profile = profile_from_entry(CATALOG["claude"])
    assert "display_name" not in profile
    assert "notes" not in profile
    assert "AI_TERMINAL_LOG_LINES" not in profile["spawn_env"]
