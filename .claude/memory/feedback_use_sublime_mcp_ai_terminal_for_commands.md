---
name: use-sublime-mcp-ai-terminal-for-commands
description: "Run shell commands in a GhostShell ai_terminal tab launched through sublime-mcp (Bash, PowerShell, Dos Console or WSL Bash profile) instead of Claude Code's Bash tool; how to do it"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 20d54abe-2f32-4daf-93ee-a1da9540a1db
  modified: 2026-10-05T01:54:24.881Z
---

2026-09-29 Donald, shouting: "WHEN WILL YOU LEARN TO USE SUBLIME-MCP TO LAUNCH A DOS, BASH, OR POWERSHELL AI_TERMINAL AND JUST RUN COMMANDS IN IT?" He wants commands run in a terminal tab inside his real Sublime (visible, no Claude Code permission prompts full of code), not through Claude Code's own Bash/PowerShell tools.

**How (worked live 2026-09-29):**
1. `mcp__sublime-mcp__batch` (allow-listed in .claude/settings.json) with `run_command` `ai_terminal_open_here` args `{"paths": ["<dir>"], "profile": "Bash"}` (profiles in GhostShell/ai_terminal.sublime-settings: `Bash` = Git Bash, `PowerShell`, `Dos Console`, `WSL Bash`).
2. eval_python: list `w.views()`; the new terminal is the view with `v.settings().get('ai_terminal_view')` true and a name like `MINGW64:/c/...`. Note its id. Donald's own Claude session tab is also an ai_terminal view (was id 13): NEVER send to it, and never use `ai_terminal_send_string_window` (it picks the active/first terminal, possibly his Claude tab). Refocus his Claude tab right after spawning (`w.focus_view(...)`) so his typing is not stolen.
3. Run a command: eval_python `sublime.View(<id>).run_command('ai_terminal_send_string', {'string': '\x15<command>\r'})` (the `\x15` = Ctrl-U clears any stray characters on the line; a stray "NG" once prefixed my first command).
4. Read output later with another eval_python: `v.substr(sublime.Region(0, v.size()))`, show the last lines. Wait a moment between send and read (separate calls).
Long-running or output-heavy commands: redirect to a file and read it.

**Status check (2026-10-05):** this describes what he asked for on 2026-09-29. Since then the working practice has been Claude Code's PowerShell tool with a plain-English description and the code kept in a script file (see [[feedback_permission_prompt_plain_english]] and [[feedback_never_use_bash_tool]]); the ai_terminal route was not used during 2026-10-04/05, and I have not re-verified that the steps above still work.

**Why it matters:** fewer prompts, and Donald can watch. Related: [[feedback_permission_prompt_plain_english]], [[feedback_ai_terminal_send_string_unreliable]] (that one is about steering a live agent CLI, not a plain shell).
