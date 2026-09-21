# Deviations from Terminus

Rule (user, 2026-09-20): Terminus is the reference. Every deviation must have a plain-language
justification here. "No justification found" is a defect, not an acceptable entry.

Status key: VERIFIED = read in both code bases, file:line given. UNVERIFIED = claim from an agent transcript.
Terminus checkout: ~/tools/Terminus, commit 0cccd3f (2025-12-16), 4,029 lines in the core.

## 1. Viewport positioning

**Terminus (VERIFIED).** One function, `scroll_to_cursor`, `terminus/render.py:354-361`. It runs once
after each render, scheduled with `set_timeout` (`render.py:322`), and calls `set_viewport_position`
once (`render.py:361`). No self-rescheduling loop and no clamp/pin/settle logic in render.py,
terminal.py, view.py or event_listeners.py.

**GhostShell (VERIFIED).** Several separate writers of viewport position in `ai_terminal.py`:
`_pin_terminal_viewport` (6020), `_pin_viewport_rest` (7735), `_pin_viewport_rest_dip_only` (7750),
`_compensate_trim_scroll` (7841), `_scroll_to_bottom` (7910), `_settle_viewport` (8067),
`_vp_pan_to_tui_scroll` (10667), and the self-rescheduling `_clamp_vp_loop` (10706, `_CLAMP_POLL_MS = 500`).
Eight call sites go through `_set_viewport` (2359); one, in `_compensate_trim_scroll` (7894),
calls `view.set_viewport_position` directly and bypasses the kill switch.

**Justification (VERIFIED from git history and code comments; search continues for the remaining writers).**
- `_scroll_to_bottom` (7910) is NOT a deviation any more. Its docstring records that on 2026-09-06 the
  keystroke jiggle was root-caused by comparison with Terminus: with `scroll_past_end` on (Terminus also
  sets it, `commands.py:509`), `layout_extent()` is one full line taller than the buffer, so targeting it
  put the view one line off. The fix adopted Terminus's own formula, `text_to_layout(size())`
  plus one line height (Terminus `render.py:357`), and Terminus's one-line deadband.
- Commit `b915926` (2026-09-07, "keystroke jiggle") extended that fix: `_real_content_height` now uses
  `text_to_layout(view.size())`, because two functions disagreeing by one line at the fit boundary made
  the view "jiggle up a line on some keystrokes". Tests: `tests/test_keypress_viewport.py`,
  `tests/test_scroll_to_bottom.py`.
- The height-change detector inside `_clamp_vp_loop` exists (same commit) because opening or closing a
  panel, resizing the window or changing layout changes `viewport_extent()` with no PTY resize and no
  keystroke, so the render loop's follow/pin recompute never ran (console panel hid the last lines of a
  following tab). Terminus recomputes only after a render (`render.py:322`), so it has no equivalent;
  this is a real, justified deviation, but a timer is not the only way to meet it (a
  `on_post_window_command`/layout event would be closer to Terminus). UNRESOLVED: no test shows the
  timer is required rather than an event.
- The self-rescheduling loop itself first appears in commit `820dd09` (2026-08-28), an automatic `pybak`
  commit with no message, so the original reason is not in git. Later commits (`b915926`,
  `a63e5ef`) describe what it does, not why a poll was chosen over events.
- Agent claims (UNVERIFIED): loop keeps TUI apps and the host from fighting over scroll
  (Claude/omp/Grok, 2026-09-18/19). Grok changed 8ms to 500ms on 2026-09-19 (57624a0), no noticed harm.

**Open defect (user report, 2026-09-20).** With Claude Code CLI in a tab, the text jiggles one line
up and back down during command-line typing. Cause not yet identified.

## 2. Kill switch is a code constant, not a setting

`_SCROLL_MANIPULATION_ENABLED = True` (`ai_terminal.py:2356`). Its comment says "NOT yet the
default anywhere live", but it is True. The comment is stale. As a constant it cannot be tuned
without editing code, which conflicts with the "everything editable via settings" rule.

## 3. History cap (VERIFIED)

Terminus `scrollback_history_size` default 10000 (`render.py:123`). GhostShell caps at 300.
Justification (UNVERIFIED, from omp/Claude 2026-09-19): 300 was tuned so the whole buffer fills the
minimap. Not yet checked against the code comment.

## 4. Detachable sessions via a standalone broker (VERIFIED)

**Terminus.** The child process is owned by the Sublime plugin host. Closing or restarting Sublime
ends the session.

**GhostShell.** `tools/agent_broker.py` owns the ConPTY and the child process and serves named pipes.
`_BrokerPty` in `ai_terminal.py` is the client. On by default for every profile
(`"detachable": true`). Details in `docs/DETACHABLE_SESSIONS.md`.

**Justification (commit `b3b3be1`, 2026-08-26, plus the owner's rule at the time).** A Sublime
restart must not kill a running agent, and the tab must reattach to the SAME live session, not a
resumed transcript. An earlier Claude-specific resume launcher was rejected because the solution had
to work for every agent CLI, not one. Verified in that commit: Sublime's own process is inside a
Windows job, so the broker is spawned with `DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB` and was
shown to survive a real restart (`tools/job_breakaway_test.py`, `tools/check_in_job.py`). Two
unidirectional pipes replace one duplex pipe because a duplex pipe stalled output under a fast burst
(reproduced live).

**Cost of the deviation (VERIFIED, from the same doc and git history).** Reattach replays up to
2 MiB of raw bytes into the terminal parser, which is what caused the replay floods and
resize storms recorded on 2026-09-18/19. This deviation is the source of a whole class of bugs
Terminus does not have. It is justified by the feature.

**Restart path exercised (VERIFIED).** Since the reattach fix `125ef49` (2026-09-18 08:12) there are 6
`*_reattach.log` session logs and 6 `*_reattach.cast` recordings (4 on 2026-09-19, 2 on 2026-09-20)
in `~/data/logs/`. Each is created only when a tab reconnects to an existing broker, so the restart
path has been used in real work at least 6 times. Correction: an earlier version of this entry said the
live restart test was unconfirmed. That came from a 2026-09-18 handoff note and was wrong. Whether
each reattach was clean is not verified here.
