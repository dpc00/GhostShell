"""Pure validation for ai_terminal profile dictionaries.

Profiles are intentionally data, not subclasses.  Keep their small contract
explicit here so settings mistakes fail visibly instead of turning into a
different terminal behaviour several call sites later.
"""


BOOLEAN_KEYS = frozenset({
    "caret_footer_pinning_enabled",
    "click_to_cursor_fallback_enabled",
    "close_tab_on_exit",
    "debug_status_bar_enabled",
    "detachable",
    "drag_forwards_by_default",
    "fast_caret_patch_enabled",
    "force_main_screen",
    "home_end_native",
    "host_cursor_paint_enabled",
    "log_tab_text",
    "mouse_handling",
    "osc_title_updates_tab",
    "page_keys_to_pty",
    "pin_viewport",
    "record_asciicast",
    "user_owns_caret_enabled",
    "wheel_to_pty",
})

NUMBER_KEYS = frozenset({
    "follow_ignore_trailing_lines",
    "max_columns",
    "min_columns",
    "min_rows",
    "scrollback_history_size",
})

STRING_KEYS = frozenset({"tab_close_input"})

PROFILE_KEYS = frozenset(
    {"launch_command", "spawn_env"} | BOOLEAN_KEYS | NUMBER_KEYS | STRING_KEYS
)


def validate_profiles(profiles):
    """Return human-readable errors and warnings for a profiles mapping."""
    errors = []
    warnings = []
    legacy_logging_profiles = []
    if not isinstance(profiles, dict):
        return ["profiles must be an object"], warnings

    for name, profile in profiles.items():
        label = "profile %r" % name
        if not isinstance(name, str) or not name.strip():
            errors.append("profile names must be non-empty strings")
            continue
        if not isinstance(profile, dict):
            errors.append("%s must be an object" % label)
            continue

        for key in sorted(set(profile) - PROFILE_KEYS):
            errors.append("%s has unknown setting %r" % (label, key))

        command = profile.get("launch_command")
        if command is not None:
            platform_command = isinstance(command, dict) and all(
                isinstance(key, str)
                and isinstance(value, list)
                and value
                and all(isinstance(arg, str) for arg in value)
                for key, value in command.items()
            )
            argv_command = (
                isinstance(command, list)
                and bool(command)
                and all(isinstance(arg, str) for arg in command)
            )
            if not (platform_command or argv_command):
                errors.append(
                    "%s launch_command must be a non-empty string array or "
                    "a platform-to-string-array object" % label
                )

        env = profile.get("spawn_env")
        if env is not None:
            if not isinstance(env, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in env.items()
            ):
                errors.append("%s spawn_env must contain only string values" % label)
            elif "AI_TERMINAL_LOG_LINES" in env:
                legacy_logging_profiles.append(name)

        for key in BOOLEAN_KEYS & set(profile):
            if not isinstance(profile[key], bool):
                errors.append("%s %s must be boolean" % (label, key))
        for key in NUMBER_KEYS & set(profile):
            value = profile[key]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                errors.append("%s %s must be numeric or null" % (label, key))
        for key in STRING_KEYS & set(profile):
            if not isinstance(profile[key], str):
                errors.append("%s %s must be a string" % (label, key))

    if legacy_logging_profiles:
        warnings.append(
            "legacy AI_TERMINAL_LOG_LINES is set by %d profile(s) (%s); "
            "prefer the profile/global record_asciicast setting"
            % (
                len(legacy_logging_profiles),
                ", ".join(sorted(legacy_logging_profiles)),
            )
        )
    return errors, warnings
