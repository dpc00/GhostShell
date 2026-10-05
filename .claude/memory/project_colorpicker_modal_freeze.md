---
name: colorpicker-modal-freeze
description: "ColorPicker package's native dialog can freeze all ai_terminal/GhostShell tabs in Sublime by blocking the main thread"
metadata: 
  node_type: memory
  type: project
  originSessionId: 60db3134-9c1f-4280-ba68-a79c6f284209
  modified: 2026-09-14T20:05:10.107Z
---

On 2026-09-14, all `ai_terminal`/GhostShell terminal tabs in a shared Sublime Text instance appeared to crash/freeze simultaneously (Claude tab and omp tab both unresponsive). Root cause: the ColorPicker package's real native color-picker dialog spawns a separate `win_colorpicker.exe` process, and Sublime's main thread blocks synchronously waiting on it. Since `ai_terminal`'s renderer also needs the main thread, every terminal tab in that Sublime instance freezes while the dialog is open — not a PTY/ANSI rendering bug, not a crash.

**Why:** confirmed via sublime-mcp's `get_console` showing `_on_main TIMEOUT` / `slow dispatch ... took 161.11s` entries lining up exactly with how long the dialog sat open, and via cross-session message from `ghostshell-21` (a peer Claude session sharing the same Sublime instance) which had itself triggered the dialog as a deliberate driveability test and then killed `win_colorpicker.exe` to unblock things.

**How to apply:** if ST/ai_terminal tabs look frozen or "crashed," check `get_console(mode="captured")` for `_on_main TIMEOUT` entries around `view_run_command` before assuming a PTY/rendering bug — look for a recently opened native dialog (ColorPicker, file pickers, etc.) as the blocker first. Also check `ListAgents` for other live sessions sharing the same Sublime instance (e.g. `ghostshell-*`) — they may be the ones who triggered it and can confirm/fix it directly.
