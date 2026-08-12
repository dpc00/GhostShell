"""Top-level plugin loader for the GhostShell package.

ST auto-loads only top-level .py as plugins. After loading a plugin module, ST scans
THAT MODULE'S OWN NAMESPACE for *Command / *EventListener subclasses and registers them
(it does not recurse into imported modules). So this file imports every command/listener
class from ai/ai_terminal.py into its own namespace, where ST's scan finds them.

If this package is symlinked into Packages/ under a name other than "GhostShell", update
the import prefix below to match the Packages/ folder name.
"""

from GhostShell.ai.ai_terminal import (
    AiTerminalOpenHereCommand,
    AiTerminalOpenInEditorCommand,
    AiTerminalSelectProfileCommand,
    AiTerminalLauncherCommand,
    AiTerminalHistoryCommand,
    AiTerminalSetWorkingDirectoryCommand,
    AiTerminalClearWorkingDirectoryCommand,
    AiTerminalRefreshUsageCommand,
    AiTerminalSyncAgentProfilesCommand,
    AiTerminalSendStringCommand,
    AiTerminalSendStringWindowCommand,
    AiTerminalKeypressCommand,
    AiTerminalToggleCopyModeCommand,
    AiTerminalRenderCommand,
    AiTerminalNukeCommand,
    AiTerminalNoopCommand,
    AiTerminalDumpScreenCommand,
    # Invoked from a .sublime-mousemap, not the palette. Registered here so the
    # command exists if a mousemap binds it; there is currently no mousemap in
    # the package, so it is inert until one is added.
    AiTerminalTrackpadScrollCommand,
    AiTerminalViewListener,
    AiTerminalKeyInterceptor,
)


# -- lifecycle -----------------------------------------------------------------
# ST only calls plugin_loaded()/plugin_unloaded() on the TOP-LEVEL plugin module
# (this file). ai_terminal.py's own hooks (resize poller start / ConPTY teardown)
# never fire after this reorg, so they must be invoked here by delegation.

def plugin_loaded():
    import importlib
    import traceback

    try:
        importlib.import_module("GhostShell.ai.ai_terminal").plugin_loaded()
    except Exception:
        # ST swallows an exception raised here, and the message alone leaves no
        # way to tell a missing setting from a broken import, so log the
        # traceback. Still caught: a failed load must not take out the host.
        print(
            "GhostShell PluginLoader: ai_terminal.plugin_loaded failed:\n%s"
            % traceback.format_exc()
        )


def plugin_unloaded():
    import importlib
    import traceback

    try:
        importlib.import_module("GhostShell.ai.ai_terminal").plugin_unloaded()
    except Exception:
        print(
            "GhostShell PluginLoader: ai_terminal.plugin_unloaded failed:\n%s"
            % traceback.format_exc()
        )
