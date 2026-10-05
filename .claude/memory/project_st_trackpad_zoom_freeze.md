---
name: project-st-trackpad-zoom-freeze
description: "GhostShell PTY resize loop — trackpad pinch-zoom font-decrease over a large terminal buffer freezes ST; seen across many agents, report upstream to GhostShell not Terminus/agent CLIs"
metadata: 
  node_type: memory
  type: project
  originSessionId: 8f3f8bf6-8c4d-4991-8e81-66d8a0278c9d
  modified: 2026-08-27T04:39:19.797Z
---

An apparent Sublime Text UI freeze/thrash (both tabs unresponsive, looked like Codex "looping on conversation full replay") during heavy Terminus terminal output was actually a trackpad two-finger pinch (zoom-down/decrease font size) gesture over a large terminal buffer. Decreasing the font size specifically triggered a genuine, self-sustaining loop that would NOT stop on its own — increasing the font size back to its prior value is what stopped it, not just waiting it out. Console log confirmed 11 rapid `[ai_terminal] resized PTY to ...` events in a burst, matching the symptom.

Root cause of the accidental trigger: the `ai_terminal` panel (hosts claude.EXE via a Terminus-like broker/PTY backend) does not auto-scroll to the bottom on new output. The user's workaround — a two-finger vertical drag to manually scroll back down to the command line — can drift slightly diagonal on a trackpad, which Sublime reads as a pinch/zoom instead of a scroll. So the fix for one annoyance (no auto-scroll) is what triggers the other (font-shrink resize loop).

**Why:** Discovered during troubleshooting a stuck sublime-mcp HTTP bridge + a separate Codex-terminal-tab freeze in the same session (2026-08-26). Initially misdiagnosed as a Codex CLI rendering bug. User confirmed: shrinking the font caused the runaway loop; restoring the font size to what it was before is the fix that actually stopped it — this is not just "expensive redraw," it does not self-resolve.

**How to apply:** If ST/sublime-mcp appears to freeze or infinitely thrash while a terminal/Terminus tab has a lot of output on screen, check whether a trackpad pinch gesture shrank the font size. Do NOT wait it out — increase the font size back up (not just any change) to break the loop. This is a fast, low-risk first troubleshooting step, cheaper than reloading the plugin or restarting ST.

The correct upstream target for a bug report is **GhostShell**, not Terminus, ST, or any individual agent CLI — the `ai_terminal` panel's PTY is spawned via a "broker" backend on a `ghostshell_*` pipe (per the ST console log), and the user has seen this same font-decrease-triggers-non-terminating-resize-loop behavior across many different agents hosted in that terminal (not just Codex), confirming the bug lives in GhostShell's PTY/resize handling, not in any one agent.

Preventively: recommend `Ctrl+End` (or the ST/Terminus scroll-to-bottom command) over a two-finger trackpad drag to reach the bottom of the `ai_terminal` panel — the drag is what risks drifting into a pinch/zoom. The underlying `ai_terminal` auto-scroll-on-new-output gap is worth fixing or reporting separately.

Related: [[project-sublime-mcp-console-fix]] (separate, real bug fixed same session — get_console_win focus-stealing, unrelated to this issue).
