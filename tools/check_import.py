"""Import ai_terminal the way Sublime does, with a stub sublime API.

Purpose: catch import-time and class-definition-time errors that would make
Sublime silently fail to register commands. Run standalone, not under pytest,
because it installs fake `sublime` / `sublime_plugin` modules into sys.modules
(tests.sublime_stub, the same ones the flow tests use).

    python tools/check_import.py
"""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.sublime_stub import install as install_sublime_stubs  # noqa: E402


def main():
    install_sublime_stubs()

    try:
        from ai import ai_terminal
    except Exception as e:
        import traceback

        traceback.print_exc()
        print("\ncheck_import: FAILED to import ai.ai_terminal: %r" % (e,))
        return 1

    expected = [
        "AiTerminalOpenHereCommand",
        "AiTerminalOpenInEditorCommand",
        "AiTerminalSelectProfileCommand",
        "AiTerminalLauncherCommand",
        "AiTerminalHistoryCommand",
        "AiTerminalSetWorkingDirectoryCommand",
        "AiTerminalClearWorkingDirectoryCommand",
        "AiTerminalRefreshUsageCommand",
        "AiTerminalSendStringCommand",
        "AiTerminalKeypressCommand",
        "AiTerminalRenderCommand",
        "AiTerminalNukeCommand",
        "AiTerminalTrackpadScrollCommand",
        "AiTerminalViewListener",
        "AiTerminalKeyInterceptor",
    ]
    missing = [n for n in expected if not hasattr(ai_terminal, n)]
    if missing:
        print("check_import: missing classes: %s" % ", ".join(missing))
        return 1

    # The periodic-usage interval is plugin-level code the pure tests cannot
    # reach, and getting it wrong means either no refresh at all or a loop that
    # hammers provider endpoints. Exercise the parsing here.
    import sublime as _sub

    cases = [
        ({}, 20 * 60 * 1000, "default"),
        ({"usage_refresh_minutes": 5}, 5 * 60 * 1000, "explicit"),
        ({"usage_refresh_minutes": 0}, 0, "disabled"),
        ({"usage_refresh_minutes": -3}, 0, "negative disables"),
        ({"usage_refresh_minutes": 0.1}, 60 * 1000, "clamped to 1 minute"),
        ({"usage_refresh_minutes": "nonsense"}, 20 * 60 * 1000, "bad value"),
    ]
    failures = []
    for values, want, label in cases:
        settings = _sub.Settings()
        settings.update(values)
        ai_terminal._settings = settings
        got = ai_terminal._usage_refresh_interval_ms()
        if got != want:
            failures.append("  %s: got %r, want %r" % (label, got, want))
    ai_terminal._settings = None
    if failures:
        print("check_import: usage refresh interval wrong:\n" + "\n".join(failures))
        return 1

    print("check_import: ai.ai_terminal imports cleanly; %d classes present; "
          "usage refresh interval OK" % len(expected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
