---
name: cast-handles-leak-2026-10-28
description: Broker keeps asciicast files of ended attach/reattach sessions open, so they cannot be recycled until the broker exits (suspected, handle owner not proven)
metadata:
  type: project
---

2026-10-28: Donald asked to delete every cast in `C:\Users\donal\data\logs\ghostshell\asciinema_casts_for_troubleshooting_rendering`. 11 of 16 went to the Recycle Bin; 5 stayed, each failing an open-for-write test (locked).

Evidence: only one `agent_broker.py` process was running (pythonw pid 20368, started 2026-10-27 21:18, also the host of the Claude tab). The five locked files were `ai_2026-10-28_043336_114737_reattach`, `043336_277628_reattach`, `061340_163364`, `061503_555160`, `062005_515844_reattach`. Four belong to sessions from earlier today, which suggests the broker never closes the cast file when a session ends or is reattached.

Not proven: which process holds the handles (no handle tool used). Setting `record_asciicast` to false stops new casts, but the already-open one (`062005_515844`) kept growing.

**Update 2026-10-28 (later):** after Donald restarted Sublime the five casts recycled fine, so the holder was Sublime's plugin host (CastRecorder runs in-process), not the broker. Cause found: the recorder is closed only in `_Terminal.kill()`; Recover Session (`_recover_term`, pops the term then `pty.kill()`) and Relaunch (`_restart_view`, pops the term then respawns) never reach it. Fix: new `_Terminal.end_recording()` called from both and from `kill()`. Compiles; NOT yet tested live (needs a Relaunch or Recover in a running Sublime, then a write-open test of the old cast).

**Why:** cast files of dead sessions cannot be deleted or recycled while the broker lives, and the broker lives as long as the Sublime session.

**How to apply:** to confirm, check the handles of the broker (Sysinternals handle64 or Restart Manager) before blaming it. A fix would close the cast writer in the session-end and reattach paths, and stop writing when `record_asciicast` turns false. After the next broker restart, recycle the five files with `recycle.ps1` and check that the folder is empty. See [[feedback_never_close_own_tab_no_blocks_2026_10_15]] before restarting anything.
