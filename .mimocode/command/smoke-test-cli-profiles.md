---
description: Safely launch and audit every configured GhostShell CLI profile without closing tabs or spending inference quota.
---

Run a startup smoke test of GhostShell's configured `ai_terminal` profiles.

Use `$ARGUMENTS` as an optional profile-name filter. With no arguments, test every profile. Use the current project directory as the explicit launch path.

1. Confirm Sublime Text and `sublime-mcp` are responsive. Record the current sheets/views and recent console-log position as the baseline.
2. Read both `ai_terminal.sublime-settings` and `ai_terminal_agents.sublime-settings` through Sublime's Settings API. Merge generated profiles first and explicit profiles second, matching GhostShell's `_all_profiles` behavior. Apply the optional filter and report the resulting ordered profile list before launching.
3. For each profile, one at a time, invoke Sublime's `ai_terminal_open_here` command with both an explicit `profile` and `paths: [<current project directory>]`.
4. Never close, replace, reuse, terminate, or send input to any tab or process. In particular, do not call a close command without a path/view identity. Leave every spawned test tab open for the user to inspect and close manually.
5. After each launch, verify that a new terminal sheet appeared, inspect only newly emitted Sublime console lines for GhostShell errors, and note missing secrets, spawn failures, tracebacks, abnormal delays, or immediate process exits. Do not authenticate, answer prompts, or send a model prompt merely to prove quota or connectivity.
6. If Sublime becomes unresponsive, the tab hosting the current agent session changes unexpectedly, or one launch stalls abnormally, stop immediately. Preserve all tabs and report the last profile attempted plus the evidence available in `C:\Users\donal\data\logs\` and the ai_terminal asciicast directory.
7. Return a compact table with profile, launch status, new sheet, startup/console evidence, and follow-up. Distinguish confirmed failures from warnings and untested interactive behavior.

Stopping condition: every selected profile has one recorded launch result, or the safety stop in step 6 fires.
