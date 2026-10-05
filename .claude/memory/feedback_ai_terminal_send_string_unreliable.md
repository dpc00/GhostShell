---
name: ai-terminal-send-string-unreliable
description: "ai_terminal_send_string_window / queue_input are unreliable ways to programmatically inject text into a live omp (or similar CLI agent) terminal session -- don't rely on them, ask the user to relay instead"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: bd237c82-c328-4cea-8ba5-30e19d5fa848
  modified: 2026-09-14T21:47:58.947Z
---

Tried to programmatically inject a "stop and do X" instruction into a live, actively-running `omp` CLI agent session via GhostShell's `ai_terminal_send_string_window` command (after `ai_terminal_queue_input`, the idle-gated variant, never fired because the agent never went idle long enough). The immediate `send_string` variant did appear to land in the raw PTY/transcript (confirmed via the jsonl session log showing it as a `role: user, steering: true` message), but Donald reported it did NOT actually work cleanly in practice -- he had to personally intervene (press Esc, dig through terminal scrollback/command history) to actually get it delivered/accepted by the omp CLI's own input handling.

**Why:** these commands write directly to the PTY without any awareness of the target CLI's own input-focus/readline state (e.g. if the CLI's UI was mid-render, in a scrollback/search mode, or otherwise not at a clean prompt line, the raw bytes can land somewhere unintended). Confirming the write reached the log is not the same as confirming the target application's input layer actually accepted it as a live instruction.

**How to apply:** don't trust `ai_terminal_send_string_window`/`ai_terminal_queue_input` (or raw PTY writes generally) as a reliable way to steer a live agent CLI session from outside. When Donald needs an instruction delivered into a running omp/Claude/other terminal session, either ask him to type/paste it himself, or verify delivery very explicitly (not just "it appeared in the log") before assuming the target agent actually received and is acting on it. Related: [[project_ghostshell_session_text_log_bug]] (same terminal session, same day's incident cluster).
