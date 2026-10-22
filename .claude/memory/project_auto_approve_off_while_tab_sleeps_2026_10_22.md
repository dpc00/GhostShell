---
name: auto-approve-off-while-tab-sleeps-2026-10-22
description: After Donald restarted ST, the Claude tab did not reactivate until he clicked it, and auto_approve_permissions did nothing while the tab was "sleeping"
metadata:
  type: project
---

2026-10-22 (Donald): after a Sublime restart, auto-approve stopped answering Claude Code permission prompts. His words: "tab does not reactivate until you click on it. Autoapprove off while 'sleeping'." His User `ai_terminal.sublime-settings` still had `auto_approve_permissions: true`, so the setting was not the cause.

**Why:** `_auto_approve_permission` runs only when the tab receives Claude Code's "blocked / permission" status report; a tab that stays asleep after reattach gets no report (cause not verified, nothing in GhostShell was changed).

**How to apply:** if he reports prompts waiting after a restart, ask him to click the tab once; look at why the restored tab stays inactive before touching the auto-approve code. See [[never-close-own-tab-no-blocks-2026-10-15]].
