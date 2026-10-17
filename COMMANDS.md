# Command reference

Every `sublime_plugin` class GhostShell registers, and every surface it's
exposed on. All classes live in `ai_terminal.py` at the repo root, which
Sublime auto-loads and scans directly -- no loader, nothing to forget to
register.

Sublime derives the ST command name from the class name automatically:
`AiTerminalOpenHereCommand` -> `ai_terminal_open_here` (`Command` suffix
stripped, CamelCase -> snake_case).

## Commands

**GhostShell: Settings** uses Sublime's built-in `edit_settings` command to
open package defaults beside `User/ai_terminal.sublime-settings`. It is also
available under Preferences > Package Settings > GhostShell > Settings.
Keep customizations in the User file so package updates do not overwrite them.

| Class | ST command | Type | Command palette | Menu(s) | Keybinding |
|---|---|---|---|---|---|
| `AiTerminalOpenHereCommand` | `ai_terminal_open_here` | WindowCommand | "Ai Terminal: Open Here" | Tools > Ai Terminal > Default Profile; Side Bar: "Open Ai Terminal here…" (`{"paths": [...]}`, default profile). Also the target every per-profile launch (menu or otherwise) resolves to via `{"profile": "<name>"}` -- no menu currently lists one entry per profile; that's what `ai_terminal_launcher`'s quick panel replaced. | — |
| `AiTerminalOpenInEditorCommand` | `ai_terminal_open_in_editor` | WindowCommand | "Ai Terminal: Open in Editor" | Context.sublime-menu and Tab Context.sublime-menu: "Open Ai Terminal here…" (Tab Context passes `{"group": -1, "index": -1}`, substituted by Sublime with the right-clicked tab's real coordinates -- see `_tab_menu_target_view`) | — |
| `AiTerminalSetWorkingDirectoryCommand` | `ai_terminal_set_working_directory` | WindowCommand | "Ai Terminal: Set Working Directory" | Tools > Ai Terminal > Set Working Directory; Side Bar: "Set Ai Terminal Working Directory" | — |
| `AiTerminalClearWorkingDirectoryCommand` | `ai_terminal_clear_working_directory` | WindowCommand | "Ai Terminal: Clear Working Directory" | Tools > Ai Terminal > Clear Working Directory; Side Bar: "Clear Ai Terminal Working Directory" | — |
| `AiTerminalRecoverSessionCommand` | `ai_terminal_recover_session` | WindowCommand | "Ai Terminal: Recover Session..." | — | — |
| `AiTerminalOpenInWindowsTerminalCommand` | `ai_terminal_open_in_windows_terminal` | TextCommand | "Ai Terminal: Open in Windows Terminal" (the release notes call it "Move to Windows Terminal") | — | — |
| `AiTerminalListSessionsCommand` | `ai_terminal_list_sessions` | WindowCommand | "Ai Terminal: List Sessions…" | Tools > Ai Terminal > Sessions > List Sessions… | — |
| `AiTerminalDetachAllToWindowsTerminalCommand` | `ai_terminal_detach_all_to_windows_terminal` | WindowCommand | "Ai Terminal: Detach All to Windows Terminal (before risky plugin edits)" | Tools > Ai Terminal > Sessions > Detach All to Windows Terminal | — |
| `AiTerminalReattachAllFromWindowsTerminalCommand` | `ai_terminal_reattach_all_from_windows_terminal` | WindowCommand | "Ai Terminal: Reattach All from Windows Terminal" | Tools > Ai Terminal > Sessions > Reattach All from Windows Terminal | — |
| `AiTerminalTuneProfileCommand` | `ai_terminal_tune_profile` | WindowCommand | "Ai Terminal: Tune Profile…" | Tab Context.sublime-menu: "Tune Profile Settings…" | — |
| `AiTerminalToggleCopyModeCommand` | `ai_terminal_toggle_copy_mode` | TextCommand | "Ai Terminal: Toggle Text Edit Mode (keys and mouse to Sublime)" | Tools > Ai Terminal > Toggle Text Edit Mode | `ctrl+alt+c` (context: `setting.ai_terminal_view`) |
| `AiTerminalSwitchPanelCommand` | `ai_terminal_switch_panel` | WindowCommand | "Ai Terminal: Switch Panel..." | — | — |
| `AiTerminalTogglePanelCommand` | `ai_terminal_toggle_panel` | TextCommand | "Ai Terminal: Toggle Tab ⇄ Panel" | — | `ctrl+alt+m` (context: `setting.ai_terminal_view`) |
| `AiTerminalKillSessionCommand` | `ai_terminal_kill_session` | WindowCommand | "Ai Terminal: Kill Session (Keep Tab Open)" | Tab Context.sublime-menu > "Ai Terminal: Session" submenu (`{"group": -1, "index": -1}`, see above) | — |
| `AiTerminalCloseKeepAliveCommand` | `ai_terminal_close_keep_alive` | WindowCommand | "Ai Terminal: Close Tab (Keep Session Alive)" | Tab Context.sublime-menu > "Ai Terminal: Session" submenu (`{"group": -1, "index": -1}`, see above) | — |
| `AiTerminalEndSessionCommand` | `ai_terminal_end_session` | WindowCommand | "Ai Terminal: End Session (Kill + Close)" | Tab Context.sublime-menu > "Ai Terminal: Session" submenu (`{"group": -1, "index": -1}`, see above) | — |
| `AiTerminalRelaunchCommand` | `ai_terminal_relaunch` | WindowCommand | "Ai Terminal: New Session (Relaunch)" | Tab Context.sublime-menu > "Ai Terminal: Session" submenu (`{"group": -1, "index": -1}`, see above); toolbar phantom's "🔄 New Session" link | — |
| `AiTerminalSessionInfoCommand` | `ai_terminal_session_info` | WindowCommand | "Ai Terminal: Session Info..." | Tab Context.sublime-menu > "Ai Terminal: Session" submenu (`{"group": -1, "index": -1}`, see above) | — |
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
| `AiTerminalTrackpadScrollCommand` | `ai_terminal_trackpad_scroll` | TextCommand | `Default.sublime-mousemap`: mouse wheel up/down in terminal views (see "Mouse" below) |
| `AiTerminalNativeClickCommand` | `ai_terminal_native_click` | TextCommand | `Default.sublime-mousemap`: left press (single, double, triple) on a Tern Surface Protocol native view (`setting.ai_tsp_native`); it does nothing, so Sublime's own press handler cannot move the caret and scroll the view (the panels are phantoms with their own links) |
| `AiTerminalToggleTuneProfilePanelCommand` / `AiTerminalTuneProfileSetCommand` | `ai_terminal_toggle_tune_profile_panel` / `ai_terminal_tune_profile_set` | TextCommand | The settings toolbar's links in the tab, and **Tune Profile** |
| `AiTerminalQueueInputCommand` / `AiTerminalQueueInputToCommand` | `ai_terminal_queue_input` / `ai_terminal_queue_input_to` | WindowCommand | Programmatic: queue a string to land in the terminal once the session has been idle for a few seconds (the second form addresses a tab by name or index) |
| `AiTerminalNoopWindowCommand` | `ai_terminal_noop_window` | WindowCommand | Sink used when a terminal consumes the native tab-close command |
| `AiTerminalDumpScreenCommand` | `ai_terminal_dump_screen` | TextCommand | Manual invocation from the console (`view.run_command("ai_terminal_dump_screen")`) for debugging; prints the screen grid/cursor to the ST console |

## Listeners (not commands — no ST command name, nothing to bind)

| Class | Base | Role |
|---|---|---|
| `AiTerminalViewListener` | `ViewEventListener` | Per-view lifecycle (activation, modification, resize) for views with `ai_terminal_view` set; scoped via `is_applicable` |
| `AiTerminalKeyInterceptor` | `EventListener` | `on_text_command`: redirects clipboard/mouse commands to the PTY when the focused view is a terminal and the app enabled DEC mouse tracking |
| `AiTerminalWindowCloseListener` | `EventListener` | Marks detachable-broker views so the tab-close interceptor can tell a window closing from a deliberate single-tab close |
| `AiTerminalTabCloseInterceptor` | `EventListener` | `on_window_command`: handles closing a terminal tab (plain Ctrl+W detaches a detachable session and keeps it running, 0.2.10; **End Session** stops it) |

## Mouse

`Default.sublime-mousemap` ships: drag-select by words and lines, `ai_terminal_native_click` on native (Tern Surface Protocol) views, and wheel scrolling through `ai_terminal_trackpad_scroll` while a program has not asked for the wheel (`ai_terminal_wheel_to_pty`).

## Non-command config surfaces

- `Default.sublime-keymap` — the 259-entry keypress passthrough (all commands are `ai_terminal_keypress`, gated by `setting.ai_terminal_view`), plus the three chord bindings above.
- `Side Bar.sublime-menu` / `Context.sublime-menu` / `Tab Context.sublime-menu` — folder/file/tab right-click entries, listed inline in the table above.
