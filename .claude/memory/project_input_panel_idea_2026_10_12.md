---
name: input-panel-for-claude-tab-2026-10-12
description: His idea: type into a Sublime input panel and send on Enter, so permission prompts and auto-approve keystrokes never collide with his typing
metadata:
  type: project
---

2026-10-12, Donald: permission prompts pop up (and auto-approve sends keys) while he is typing in the Claude tab, which makes a mess. He proposed moving text input from the terminal's command line to a Sublime input box (window.show_input_panel); he types and edits there, presses Enter, and the plugin sends the text to the tab. He knows it departs from a normal TUI but it avoids the whole problem.

Open points: the panel needs a way to answer a permission prompt (a key, or a one-line answer mode; a bare Enter works because the cursor starts on "1) Yes"); auto-approve should still wait until he is idle (see item 11 of the sublime-mcp fix plan). Not built; he has not asked to start.

**Why:** typing collisions with prompts. **How to apply:** if he says go, prototype show_input_panel that sends via the existing ai_terminal send-string path; keep normal terminal input available.
