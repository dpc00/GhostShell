
## 2026-11-12 - page keys wedged after a crash kill spree

Symptom: PageUp/PageDown dead in GhostShell terminal tabs (the transcript could not be scrolled at all) after a session where several VS Code processes were force-killed. GhostShell's own setting was the fix: set page_keys_to_pty - page keys came back at once. Mouse wheel and the scrollbar kept working throughout; restarting the tab or Sublime was NOT needed.

(Donald fixed it himself: "set 'page_keys_to_pty'. page keys work now.")

Committed 2026-11-12 as 3be4597 on the OpenCode profile in ai_terminal.sublime-settings,
so a revert or package update cannot lose it again.
