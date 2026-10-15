---
name: briefing-omp-native-view-unreadable-2026-10-14
description: Briefing for omp (tab 2): why an agent cannot read the omp tab through sublime-mcp while Donald can see it, what was verified on 2026-10-14, and what a fix could look like
metadata:
  type: project
---

**For omp (read this, then answer in plain words; Donald is ill and cannot read long screens).**

**The problem in one line:** Donald can read the omp tab. Claude Code (the other agent, using sublime-mcp) cannot read it as text. Something is wrong if a person can see content that an agent attached to the same editor cannot get. Please explain why in short plain words, and propose a way for sublime-mcp to read it. Donald means ONE NEW TOOL inside the existing sublime-mcp (not a new MCP server, not a rewrite); it may be backed by a GhostShell command that the tool calls.

**What Claude Code verified on 2026-10-14 (main Sublime, build 4215, GhostShell plugin loaded):**
1. The omp tab is a native TSP view (`ai_tsp_native` is set on the view; status bar says "omp native view: live HH:MM:SS"). `terminal/tsp_view.py` draws it: one blank text line per panel and the panel itself as an HTML block phantom (`sublime.LAYOUT_BLOCK`) hanging under that line. So the buffer text is only spaces and newlines: about 100 lines, 200 to 300 characters, with 98 to 101 phantoms. `view.substr(...)` and every sublime-mcp tool that reads view text therefore return nothing useful for this tab.
2. The only way Claude Code could read the tab was a screenshot of the Sublime window (mcp screenshot tool), and only after making that tab the active one. A screenshot shows just the visible part (about 15 panels), so older output needs scrolling, and the tab must be on top. This is not reading; it is looking at a picture.
3. `eval_python` can read internal state: `term._tsp_view._shown` (a list of `(key, html)` parts, one per panel), `term._tsp_view._outline`, scroll position, layout height, `_hold_id`, `_pin_bottom`, `_animation_ms`. Nobody has tried extracting plain text from those parts or from the TSP document held by `term._tsp` (terminal/tsp_host.py).
4. In the Claude Code tab (not native) the buffer holds the real text, so reading it works. The gap is only in native TSP views.
5. Side findings the same day: the view can be scrolled past the end of the content (viewport y 7429 with the maximum 7337) and then the tab shows only black; a restart or a move back to the bottom fixes it. `_hold_id` had reached 5723 within minutes, so the new restore code fires very often.

**What would solve it (omp: confirm, correct or replace this):**
- A GhostShell command, for example `ai_terminal_read_surface`, that returns the TSP document as plain text (one entry per panel: kind such as Edit/Read/Bash/Thought/message, title, text, status, time) plus the visible range and scroll position. It reads from `term._tsp` (the document), not from the HTML.
- A matching sublime-mcp tool (in `projects\sublime-mcp\sublime_mcp.py`) that calls it for a named or active terminal view, so an agent can ask "what does tab 2 say" without a screenshot, without switching tabs and without scrolling.
- Optionally a scrolling read ("last N panels", "panels from id X").

**Constraints (from Donald's notes, please respect):**
- sublime-mcp exposes an EDITOR's commands to an agent; do not reshape it into an agent-style Read tool. A tool that reports what a view shows is an editor command and fits.
- Saving `sublime_mcp.py` reloads the MCP plugin and drops the agent connection: use detach_all, restart, reattach_all and tell Donald first. GhostShell files in a subfolder (`terminal/*.py`) are not reloaded by Sublime on save; a restart is needed.
- Do not write tests. Do not use the Bash tool for his routine tasks where a dedicated tool exists. Everything you change stays uncommitted until he says so; there are already uncommitted GhostShell edits (tracer fix, auto-approve repeat, omp's `tsp_view.py` and `tsp_host.py` changes, the `tsp_animation_ms: 0` stopgap).
- Keep your answers short; he is sick.

**Questions for omp:** (a) Why is the native view unreadable as text, in your own words? (b) Is the TSP document in `term._tsp` complete enough to rebuild the transcript as plain text? (c) What is the smallest change that gives Claude Code a text read of this tab?
