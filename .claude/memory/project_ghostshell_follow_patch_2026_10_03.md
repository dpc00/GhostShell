---
name: ghostshell-follow-patch-2026-10-03
description: GhostShell cursor/scroll-jump fix (live since 2026-10-02, now inside backup commit 4a3b028); jump still reported 2026-10-03 05:00; recorder re-armed until ~13:00
metadata:
  node_type: memory
  type: project
  originSessionId: fcb54aa2-1c5d-45af-9d39-81632204ff55
  modified: 2026-10-03T11:02:34.396Z
---

2026-10-03 Donald reported GhostShell's view/cursor "jumping to random portions of the tab" (Ctrl-End needed; "jumped up into conversation"; pressing `9` put the cursor below the command line). Recorders showed: follow flag switched off by small view drifts (rule at ai_terminal.py `_run`: viewport < `_live_anchor_y` - 1.5 lines), buffer then growing while follow is off, collapsing in one step on the next printable key (jumps of 700 to 20,000 px); and frames where Claude Code hides its cursor parked bottom-right made the ST caret land on the last row.

**Why:** Donald wants the whole status line visible at all times and uses a "push up" habit; the code deliberately leaves a pushed-up tail alone, so the fix must not fight that.

**How to apply:** the patch (+27 lines, two edits: `cursor_parked` and the `tail_top_y` follow rule) is live and was swept into the automatic backup commit `4a3b028 pybak 2026-10-02 19:29` of `~/projects/GhostShell` (so `git status` is clean; it is NOT a deliberate release). Do not release without Donald confirming after longer use. Update 2026-10-03 ~05:00: Donald reported "cursor in Tab repositioned to 1/3 of tab contents" - so jumps are reduced, not gone. The old recorders had expired after 60 min; I re-armed a new one (scratchpad `gs_recorder.py`, runs in the MAIN Sublime via eval_python, 8 h, wraps `_set_viewport`/`_set_auto_follow`, dumps the last minute to `gs_jump_events.jsonl` on a viewport/caret jump; stop with `sys.modules['__gs_rec__']['stop']=True`). Read that file first when he reports another jump. The cause of the unprompted view drifts (no GhostShell write) is still unknown. See [[feedback_never_skip_for_missing_prereqs]] for how he wants hard bugs handled.

**Update 2026-10-03 ~12:50:** Donald said the cursor jumped again and I "did nothing". Recorder showed: (a) the Claude tab is `tui_owns_scroll`, so `_settle_viewport` never re-pins it (by design, so trackpad/keypad motion is not fought); the `_auto_follow`/`tail_top_y` patch only covers non-TUI tabs; (b) the view then stays at the old pixel row when the buffer jumps by several screens (reprint), e.g. 19k -> 61k chars, landing ~26% down; (c) separate small drifts of 200-520 px with no GhostShell call look like scroll input from outside. Added `_keep_tail_after_reprint(view, term)` (called from `_settle_viewport` for TUIs): if the layout grew by more than 1.5 screens in one frame, the viewport did not move, and it was within half a screen of the old end, move to the new end keeping the same gap. Compiled, live-reloaded, uncommitted (the repo is a clean checkout of backup commit 4a3b028 plus this edit). Recorder re-armed until 00:46; check gs_jump_events.jsonl and the call stack of `_keep_tail_after_reprint` in the ring (look for `_set_viewport` with that frame). Revert: `git -C ~/projects/GhostShell checkout ai_terminal.py` would drop BOTH this and nothing else, since the earlier patch is already in 4a3b028.
