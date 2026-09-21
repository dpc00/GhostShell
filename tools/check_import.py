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
        import ai_terminal
    except Exception as e:
        import traceback

        traceback.print_exc()
        print("\ncheck_import: FAILED to import ai_terminal: %r" % (e,))
        return 1

    expected = [
        "AiTerminalOpenHereCommand",
        "AiTerminalOpenInEditorCommand",
        "AiTerminalLauncherCommand",
        "AiTerminalHistoryCommand",
        "AiTerminalSetWorkingDirectoryCommand",
        "AiTerminalClearWorkingDirectoryCommand",
        "AiTerminalSendStringCommand",
        "AiTerminalSendStringWindowCommand",
        "AiTerminalKeypressCommand",
        "AiTerminalRenderCommand",
        "AiTerminalNukeCommand",
        "AiTerminalNoopCommand",
        "AiTerminalTrackpadScrollCommand",
        "AiTerminalDumpScreenCommand",
        "AiTerminalToggleCopyModeCommand",
        "AiTerminalTogglePanelCommand",
        "AiTerminalSwitchPanelCommand",
        "AiTerminalSyncAgentProfilesCommand",
        "AiTerminalViewListener",
        "AiTerminalKeyInterceptor",
    ]
    missing = [n for n in expected if not hasattr(ai_terminal, n)]
    if missing:
        print("check_import: missing classes: %s" % ", ".join(missing))
        return 1

    print("check_import: ai_terminal imports cleanly; %d classes present" % len(expected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
