# Command reference

Every `sublime_plugin` class GhostShell registers, and every surface it's
exposed on. All classes live in `ai_terminal.py` at the repo root, which
Sublime auto-loads and scans directly -- no loader, nothing to forget to
register.

Sublime derives the ST command name from the class name automatically:
`AiTerminalOpenHereCommand` -> `ai_terminal_open_here` (`Command` suffix
stripped, CamelCase -> snake_case).

## Commands

| Class | ST command | Type | Command palette | Menu(s) | Keybinding |
|---|---|---|---|---|---|
| `AiTerminalLauncherCommand` | `ai_terminal_launcher` | WindowCommand | "Ai Terminal: Launch Agent…" | Tools > Ai Terminal > Launch Agent… | `ctrl+alt+n` |
| `AiTerminalHistoryCommand` | `ai_terminal_history` | WindowCommand | "Ai Terminal: All Agent History…" | Tools > Ai Terminal > All Agent History… | `ctrl+alt+h` |
| `AiTerminalRefreshUsageCommand` | `ai_terminal_refresh_usage` | WindowCommand | "Ai Terminal: Refresh Usage & Quota" | Tools > Ai Terminal > Refresh Usage & Quota | — |
| `AiTerminalSyncAgentProfilesCommand` | `ai_terminal_sync_agent_profiles` | ApplicationCommand | "Ai Terminal: Sync Detected Agent Profiles" | — | — |
| `AiTerminalOpenHereCommand` | `ai_terminal_open_here` | WindowCommand | "Ai Terminal: Open Here" | Tools > Ai Terminal > Default Profile, and once per configured profile (Shells/Claude/OpenCode/Codex submenus plus ~14 flat entries), each via `{"profile": "<name>"}`; Side Bar: "Open Ai Terminal here…" (`{"paths": [...]}` , default profile) | — |
| `AiTerminalOpenInEditorCommand` | `ai_terminal_open_in_editor` | TextCommand | "Ai Terminal: Open in Editor" | Context.sublime-menu and Tab Context.sublime-menu: "Open Ai Terminal here…" | — |
| `AiTerminalSelectProfileCommand` | `ai_terminal_select_profile` | WindowCommand | "Ai Terminal: Open Profile..." | Tools > Ai Terminal > Open Profile…; Side Bar: "Open Ai Terminal Profile…" | — |
| `AiTerminalSetWorkingDirectoryCommand` | `ai_terminal_set_working_directory` | WindowCommand | "Ai Terminal: Set Working Directory" | Tools > Ai Terminal > Set Working Directory; Side Bar: "Set Ai Terminal Working Directory" | — |
| `AiTerminalClearWorkingDirectoryCommand` | `ai_terminal_clear_working_directory` | WindowCommand | "Ai Terminal: Clear Working Directory" | Tools > Ai Terminal > Clear Working Directory; Side Bar: "Clear Ai Terminal Working Directory" | — |
| `AiTerminalReattachSessionCommand` | `ai_terminal_reattach_session` | WindowCommand | "Ai Terminal: Recover Orphaned Session..." | — | — |
| `AiTerminalReviveFrozenTabCommand` | `ai_terminal_revive_frozen_tab` | TextCommand | "Ai Terminal: Revive Frozen Tab" | — | — |
| `AiTerminalOpenInWindowsTerminalCommand` | `ai_terminal_open_in_windows_terminal` | TextCommand | "Ai Terminal: Open in Windows Terminal" | — | — |
| `AiTerminalNukeCommand` | `ai_terminal_nuke` | TextCommand | "Ai Terminal: Nuke" | Tools > Ai Terminal > Nuke Ai Terminal | `ctrl+alt+k` (context: `setting.ai_terminal_view`) |
| `AiTerminalKeypressCommand` | `ai_terminal_keypress` | TextCommand | — | — | ~259 bindings in `Default.sublime-keymap`, all gated on `setting.ai_terminal_view`; this is the keystroke → PTY forwarding path |

## Internal-only (no palette/menu/keymap entry — invoked programmatically)

These are documented as such directly in their class docstrings; listed here
so the "why isn't this in a menu" question has one place to be answered.

| Class | ST command | Type | Invoked from |
|---|---|---|---|
| `AiTerminalSendStringCommand` | `ai_terminal_send_string` | TextCommand | Programmatic API (terminus_send_string equivalent) for other plugins/scripts to inject text into a terminal view |
| `AiTerminalSendStringWindowCommand` | `ai_terminal_send_string_window` | WindowCommand | Same, but resolves the target terminal view within the window without it needing focus |
| `AiTerminalRenderCommand` | `ai_terminal_render` | TextCommand | `ai_terminal.py`'s own PTY-output loop (`view.run_command("ai_terminal_render", ...)`) on every screen update |
| `AiTerminalNoopCommand` | `ai_terminal_noop` | TextCommand | `AiTerminalKeyInterceptor.on_text_command` returns this to swallow a command it intercepted (e.g. blocking a default ST binding while a terminal view is focused) |
| `AiTerminalTrackpadScrollCommand` | `ai_terminal_trackpad_scroll` | TextCommand | Meant for a `.sublime-mousemap`; no mousemap ships in this package, so it's currently inert until a user adds one |
| `AiTerminalDumpScreenCommand` | `ai_terminal_dump_screen` | TextCommand | Manual invocation from the console (`view.run_command("ai_terminal_dump_screen")`) for debugging; prints the screen grid/cursor to the ST console |

## Listeners (not commands — no ST command name, nothing to bind)

| Class | Base | Role |
|---|---|---|
| `AiTerminalViewListener` | `ViewEventListener` | Per-view lifecycle (activation, modification, resize) for views with `ai_terminal_view` set; scoped via `is_applicable` |
| `AiTerminalKeyInterceptor` | `EventListener` | `on_text_command`: redirects clipboard/mouse commands to the PTY when the focused view is a terminal and the app enabled DEC mouse tracking |

## Non-command config surfaces

- `Default.sublime-keymap` — the 259-entry keypress passthrough (all commands are `ai_terminal_keypress`, gated by `setting.ai_terminal_view`), plus the three chord bindings above.
- `Side Bar.sublime-menu` / `Context.sublime-menu` / `Tab Context.sublime-menu` — folder/file/tab right-click entries, listed inline in the table above.
