from terminal.profile_schema import validate_profiles


def test_valid_profile_schema():
    errors, warnings = validate_profiles({
        "Codex": {
            "launch_command": ["codex"],
            "spawn_env": {"RUST_LOG": "warn"},
            "detachable": True,
            "broker_scrollback_bytes": 2097152,
            "scrollback_history_size": 300,
            "record_asciicast": False,
        }
    })
    assert errors == []
    assert warnings == []


def test_tui_routing_keys_are_valid_profile_settings():
    errors, warnings = validate_profiles({
        "Grok Build": {
            "launch_command": ["grok"],
            "page_keys_to_pty": True,
            "mouse_handling": True,
            "wheel_to_pty": True,
            "force_tui_like": False,
            "pin_viewport": True,
            "home_end_native": False,
        }
    })
    assert errors == []
    assert warnings == []


def test_profile_schema_reports_typos_and_wrong_types():
    errors, _warnings = validate_profiles({
        "Codex": {
            "launch_comand": ["codex"],
            "detachable": "yes",
            "spawn_env": {"COUNT": 3},
        }
    })
    assert any("unknown setting 'launch_comand'" in error for error in errors)
    assert any("detachable must be boolean" in error for error in errors)
    assert any("spawn_env must contain only string values" in error for error in errors)


def test_profile_schema_marks_old_logging_environment_switch_legacy():
    errors, warnings = validate_profiles({
        "Claude": {
            "launch_command": ["claude"],
            "spawn_env": {"AI_TERMINAL_LOG_LINES": "1"},
        }
    })
    assert errors == []
    assert len(warnings) == 1
    assert "prefer the profile/global record_asciicast setting" in warnings[0]
