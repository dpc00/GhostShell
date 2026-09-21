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

## 5. Input path: keys and mouse (VERIFIED)

**Key name table: NOT a deviation.** `terminal/keys.py` opens with "Key name -> terminal byte sequences
(Terminus-compatible)" and its `KEY_MAP` matches Terminus `terminus/key.py` (itself "adopted from
TerminalView").

**Win32 input mode (DEC 9001).** Terminus has no equivalent. GhostShell sends keys through
`encode_win32_key` (`terminal/keys.py:204`, called at `ai_terminal.py:8583`) when the child has enabled
mode 9001. Justification (code comment, `ai_terminal.py:8569-8571`): apps that enable it (confirmed:
Qwen Code) ignore plain xterm sequences entirely, so every key silently does nothing.
Several profile comments in `ai_terminal.sublime-settings` record other agents that turn it on.

**Keys go through the libghostty-vt key encoder first** (`ai_terminal.py:8586-8592`), then the static
table as fallback. Terminus uses only the static table. Justification (code comment): the native
encoder syncs the live terminal state (app-cursor mode, Kitty keyboard protocol, modifyOtherKeys,
alt-escape prefix), which the static table cannot do. The whole decision runs under `term._lock`
because the PTY reader thread can change mode state at the same moment a key arrives.

**Mouse reporting to the PTY.** Terminus has no DEC mouse encoding (a search of `terminus/*.py` found
none); it handles URL clicks only. GhostShell has `terminal/mouse.py` (SGR and legacy X10), because agent
TUIs enable DEC 1000/1002/1003/1006 and need real mouse reports. Basis: an audit recorded in
`ai/TODO-archive.md` (2026-08-11) that replayed 470 recorded asciicast sessions to learn which CLIs
enable mouse tracking, and `ai/CURSOR_SYSTEM_HISTORY.md`. GhostShell also has a click-to-cursor
fallback (`_route_click_to_cursor_fallback`, per `ai/RECOVERY_PLAN.md`) that sends arrow keys to move
the cursor of apps with NO mouse tracking. Since 2026-09-18 `mouse.py` uses the native
`GhosttyMouseEncoder` and keeps the hand-written encoder only as a fallback.

**Open items.** `terminal/mouse.py` imports `ctypes`; rule 5 (every ctypes line documented) has not been
audited. The per-profile routing switches (`mouse_handling`, `wheel_to_pty`, `page_keys_to_pty`) exist
to serve those audited per-agent differences, but Grok (2026-09-19) found some of the 16 gates
ignored on the alt-screen path (see section 1). Justified in intent, not yet checked gate by gate.

## 6. Output path: how the screen reaches the Sublime view (VERIFIED facts; one justification NOT found)

**Terminus.** Uses the Python `pyte` emulator. `TerminusRenderCommand.update_lines`
(`terminus/render.py:174-196`) walks only the screen's dirty lines and updates those lines in the
buffer, colours them per line, and trims history line by line. It only trims and re-anchors the cursor
if the user has not scrolled away (`render.py:150-154`).

**GhostShell.** `AiTerminalRenderCommand` (`ai_terminal.py:8700`) replaces the WHOLE buffer with the
current screen snapshot every frame (`view.replace(..., Region(0, view.size()), text)`, line 8793),
then re-applies all colour regions. One shortcut exists: when only the host cursor moved and at most 4
characters differ, it patches those characters instead (`fast_caret`, lines 8767-8790). The emulator is
`libghostty-vt` (native, via ctypes), whose state is copied into a second Python `Screen` object.

**What the repo already says about this (VERIFIED).**
- `ai/RECOVERY_PLAN.md` (lines 14-30) names the second Python `Screen` copy as the source of most of the
  regression ledger and states the intended architecture: the native engine is the single source of
  truth and the host is a thin consumer that reads snapshots. That plan does not defend the whole-buffer
  replace; it identifies the state-copy design as the problem.
- `ai/ARCHITECTURAL_BOUNDARIES.md` (2026-08-29) compares SublimeREPL, TerminalView, Terminus and
  terminus-persistence and states the scope rule: GhostShell combines four responsibilities
  (launch, emulate and paint, survive Sublime, reconstruct screen) and the fourth is the dangerous one.
  It sets a stop rule for restart recovery.
- `ai/TODO-resolved-2026-08-30.md`: a "Terminus stage 1/2" rewrite (2026-08-21 to 08-27) tried to move
  toward Terminus. Its "five Terminus-deviation bisection gates" are exactly these settings:
  `host_cursor_paint_enabled`, `click_to_cursor_fallback_enabled`, `user_owns_caret_enabled`,
  `caret_footer_pinning_enabled`, `fast_caret_patch_enabled`. Stage 2 was merged to `main` on the
  owner's decision to verify by living with it (`403c8ab`, `06f6de6`).
- The same file records that a live tab behaved differently for a whole session because a stale
  personal override in `Packages/User/ai_terminal.sublime-settings` forced four of those five gates back to
  their old values. A User-level override silently beats the repo defaults.

**Justification for the whole-buffer replace: NOT FOUND in git messages or `ai/`.** The design first
appears in the unlabeled `pybak` commit `820dd09` (2026-08-28). Next places to search: the Claude and omp
transcripts from 2026-08-15 to 08-28, which pre-date the range already extracted.
Terminus's dirty-line update shows a Sublime terminal does not need it. The whole-buffer rewrite is also
the mechanism behind the trim-and-shift viewport problem (section 1, `_compensate_trim_scroll`).
