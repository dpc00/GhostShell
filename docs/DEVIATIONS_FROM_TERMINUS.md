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

**Restart of 2026-09-21 (VERIFIED, owner-initiated).** Sublime was restarted at 00:39:57 (process start time). At
00:40:04 the broker log records the clients of both live tabs detaching and one reattaching, and one new
`ai_2026-09-21_004004_628381_reattach` `.log` (24,749 bytes) and `.cast` (66,436 bytes) pair was created at 00:43. The
Claude session running in one of those tabs kept working through the restart, and the Grok tab was also still present.
So the broker design did what section 4 says it should. Replay artefacts, measured the same day (VERIFIED):
- Recording `..._004004_628381_reattach.cast` (127 x 52, 304 s, 49,702 bytes of output): 0 resize events (no row or
  column flip), 2 cursor-home sequences and no screen-clear sequences, so no full-screen redraw dump. The cast holds only
  live output after reattach; the broker's replayed bytes are not recorded there (by design, see section 9).
- Reattach log (318 non-blank lines): only legitimate repeats (`Shell cwd was reset` once per Bash call, a message
  the owner sent twice, divider lines). No duplicated conversation block.
- The restored tab (262 non-blank lines) against the pre-restart log tail plus the reattach log: 259 of 262 lines align in
  order (98.9%). The 3 that do not are the live spinner and status-bar counters.
Conclusion: this restart reattached cleanly. One restart, one session; not a general proof.
Owner's observation (2026-09-21): no flaw found in the reattach; Sublime records the tab contents faithfully; the only visible
effect of reattachment is that the colours "come back". Mechanism (VERIFIED in code, `_apply_color_regions`,
`ai_terminal.py:5165-5191`): colours are Sublime regions added each frame with `flags=sublime.DRAW_NO_OUTLINE` and no
persistence flag, so Sublime does not keep them across a restart and they are rebuilt on the first frame after reattach.
Expected behaviour, not a defect.

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

**Origin of the whole-buffer replace (found 2026-09-20 in a Claude transcript, session `0dd8959e`,
2026-08-25 07:25, a history trace of this code; commit hashes checked where noted).**
- 2026-07-03, SText commit `f4697b9` (VERIFIED it exists; its message is an unlabeled `pybak`): `ai_terminal.py`
  is "born whole", 1185 lines, with a hand-rolled ANSI parser tailored to Claude's output and a capped
  history deque from the first line. Rendering the whole screen snapshot into the view came from that first
  version.
- 07-30: hand-rolled parser replaced by `pyte` (the same emulator Terminus uses, which supplies dirty
  lines); the renderer stayed whole-buffer. 08-03 (`aa8ee80`, VERIFIED in SText): `libghostty-vt` added via
  ctypes. 08-05: `pyte` deleted, `libghostty-vt` the only engine.
- 08-17/18: `_compensate_trim_scroll` added to compensate for history eviction drift that the
  whole-buffer replace exposes (transcript claim; `f6b8ecd` cited there is NOT found in either repo, UNVERIFIED).

**Justification: NONE RECORDED. This is an inherited design, not a chosen one.** The whole-buffer rewrite
came from a first version written for one agent, and nothing in git or `ai/` shows it being weighed
against Terminus's dirty-line update, even after `pyte` (which provides dirty lines) was adopted. The
records also show it is the mechanism behind the trim-and-shift viewport problem (section 1).
Status: an unjustified deviation. It needs a decision, not a defence.

## 7. Colors (VERIFIED facts; partial justification)

**Terminus.** One `view.add_regions` call per coloured segment, each with a unique key and a scope named
`terminus.<fg>.<bg>` (`terminus/render.py:217-243`). Colours come from a theme file generated by
`generate_theme_file` (`terminus/theme.py`).

**GhostShell.** `_apply_color_regions` (`ai_terminal.py:5165`) groups every colour run in the frame by
scope, adds one region set per scope, and erases keys used last frame but not this frame. Scopes are
`ai.fb.*`, defined in a static `ai_terminal.sublime-color-scheme` (450,593 bytes). The 16-colour palette
is copied from Terminus ("Terminus true_black vivid values", `terminal/colors.py:7`).

**Justified (code comment, `ai_terminal.py:5172-5182`).** Every `ai.fb.*` scope has a solid `#000001`
background because Sublime collapses a scope background equal to the view's global `#000000` to none,
which swaps foreground and background. With only a foreground, `add_regions` paints the foreground as the
fill and leaves the text uncoloured. `DRAW_NO_OUTLINE` avoids a border around each run.

**NOT FOUND.** Why a static 450 KB scheme and per-frame grouping instead of Terminus's generated theme
and per-segment keys. The design first appears in the unlabeled `pybak` commit `820dd09` (2026-08-28).
No performance or correctness claim was found in git or `ai/`, so none is made here.

## 8. Phantom toolbar and in-tab Settings panel (VERIFIED)

**Terminus.** Uses phantoms only for images (`terminus/terminal.py:362`). No toolbar.

**GhostShell.** A persistent bottom-of-tab toolbar (Kill Session, Keep Alive and Close Tab, Open in
Windows Terminal, Settings) is a `LAYOUT_BLOCK` phantom at the end of the buffer (`_add_close_toolbar`,
`ai_terminal.py:1183`). It is erased and re-added after every frame.

**Justified (commit `583adbd`, 2026-09-09).** Mouse-driven tab close never dispatches a plugin-visible
command (sublimehq/sublime_text#1922), so the earlier confirmation dialog never fired and closing a tab
killed the session with no warning. `on_close` now always detaches; ending a session for real is opt-in
through this toolbar. The phantom is re-added every frame because a phantom's anchor does not follow a
buffer that is fully replaced each frame (code comment, `ai_terminal.py:1189-1195`). That is another cost of
the whole-buffer replace in section 6.

**Settings panel (commit `e7814c1`, 2026-09-12).** Expands under the toolbar so "the tab is never
covered or navigated away from"; each checkbox saves immediately.

**Known defect, VERIFIED from Grok's read of the code on 2026-09-19 (`ai_terminal.py` about lines 2534-2541
and 7985-7990).** On an alt-screen tab, `_tui_like` is forced true and the viewport is pinned to the top every
frame, so the Settings panel, which sits below the pinned frame, cannot be reached. A wrong setting cannot
then be undone from the tab. The panel fails exactly where it is needed.

## 9. Logging and recording (VERIFIED)

**Terminus.** None. A search of `terminus/*.py` for logging, asciicast, transcript and record found no
session recording. This is a pure addition.

**GhostShell.** Five recorder modules in `terminal/`:
- `session_text_log.py` (200 lines): the tab text as a `.log`, without the 300-line cap.
- `cast_recorder.py` (187 lines): asciicast v3 recording, one file per session.
- `raw_debug_log.py` (24 lines): raw pre-decode PTY bytes, for suspected parser bugs.
- `settings_debug_log.py` (12 lines): traces live settings changes; no tests.
- `color_scheme_log.py` (9 lines): a retired diagnostic hook, now a no-op stub. Its own docstring says it is kept
  so 24 call sites in `ai_terminal.py` need not be deleted and tracing can be switched back on by editing one
  function. The owner recalls it logged until 2026-08-18; in this repo's git the file first appears
  (2026-08-28, `820dd09`) already as the stub, so git does not show the earlier logging period.
  Traced 2026-09-21: it was live diagnostics writing to `~/data/logs/ai_terminal/color_scheme.log` (earlier, from
  2026-07-10 to 07-20, to `developer_diagnostics_and_runtime_server_error_logs/color_scheme.log`). The newer file has 5
  lines, all plugin-start heartbeats ("Initialized. Loaded 5948 registered scope rules from disk (423061 bytes)"),
  the last at 2026-08-18 01:45. It became a stub in the unlabeled auto-backup commit `bb4a50f` (2026-08-18 18:43),
  which also changed `ai_terminal.py` (178 lines), `screen.py`, `ghostty_engine.py`, tests and 1,343 lines of settings.
  **Why it was silenced: NOT RECORDED** in git, `ai/`, or any transcript found (none from 2026-08-15 to 08-19 mention
  it). What the log shows: the runtime-rewritten scheme file keeps growing (5,140 rules / 360,702 bytes on 07-10;
  5,252 / 368,651 on 07-20; 5,948 / 423,061 on 08-18; 450,593 bytes now). The pipeline that writes it
  (`_init_dynamic_color_scheme`, `_repair_scheme_rules`, `_save_color_scheme`, `_durable_scheme_backup`,
  `ai_terminal.py` about lines 1755-1990) still runs with its 22 log calls silenced.
  The earlier file (`developer_diagnostics_and_runtime_server_error_logs/color_scheme.log`, 593 lines, 2026-07-10 to 07-20)
  shows what the pipeline does: 344 `[register]` lines ("Encountered new scope: ai.fb.146.23 (Memory registered count:
  5143)"), 149 `[init]` lines and 100 `[flush]` lines ("Flushed 6 dynamic rules to disk. Total rules: 5149"; largest
  flush 37 rules), written from a background thread. In other words, every new foreground/background colour pair seen
  on screen becomes a new scope rule and is written into the on-disk colour scheme file. No error, warning or repair
  line appears anywhere in the file.
  **Gap (VERIFIED from the two files, cause not recorded):** the earlier file ends 2026-07-20 at 5,252 rules; the later file
  (2026-08-18) starts at 5,948. About 696 rules were added in between with no `[register]` or `[flush]` line in
  either file, so the detailed logging had already stopped by 2026-08-18 and only the start-of-plugin line
  continued until then. When the detail stopped is not shown.
Both `log_tab_text` and `record_asciicast` default to `false` in the repo settings (lines 134 and 941);
individual users switch them on.

**Justified (written contracts).**
- `ai/PROFILE_AND_RECORDING_INVARIANTS.md` is the operating contract: recording files are created before the
  child starts so its first output is not missed; a reattach writes a new correlated `*_reattach.cast` and
  `*_reattach.log` and never appends to the old one (appending would duplicate the broker's replay).
- `ai/ARCHITECTURAL_BOUNDARIES.md` sets the authority order: the agent's own transcript for conversation
  content, the live emulator for current screen, the `.log` as an append-only record of tab lines (not a
  transcript), the `.cast` as diagnostic evidence.
- The asciicast recordings have a proven use: the 2026-08-11 mouse-tracking audit replayed 470 of them
  (`ai/TODO-archive.md`), and the 2026-09-19 resize-storm diagnosis came from one cast file.
- `session_text_log.py` writes only after 0.5 s of quiet (`_WRITE_DEBOUNCE_S`): a streaming tab was writing on
  every ~30 ms render and freezing Sublime (found live 2026-09-14, code comment).

**Current state of the text log: UNVERIFIED.** The current file documents the intended behaviour. The last recorded
check is Claude's by-eye comparison of log against tab on 2026-09-19 (settled lines matched; live bottom rows
differed), and omp found the same day that a `/usage` panel's inner rows never reached the `.log`. No scripted
comparison has been run since the 2026-09-18 redesign. Not known to be broken; not shown to be correct.

**Measured 2026-09-21 (VERIFIED, one session, read-only).** Tab 1 (the Claude tab, 304 non-blank lines) against its log
`ai_2026-09-20_194550_007603.log` (1,992 non-blank lines): a sequence alignment of the tab against the last 700 log lines
matches 294 of 304 tab lines (96.7%) in order. The 10 unmatched lines: 5 expected live rows (spinner, status-bar
counters, the reply still being written) and 3 wrapped continuation lines near the top of the tab (from an earlier
reply) that appear in the log only as quotations from tool output, so it is not known whether the log lost them or
recorded them wrapped differently. No long consecutive duplicate lines (0), so no replay duplication. All 12 commit
hashes mentioned in this conversation appear in the log. A second pairing checked: the Grok tab (Tab 3) against its
log `ai_2026-09-20_205311_192523.log`: 9 of 9 lines, exact, in order. Conclusion: for these sessions the current text
log is substantially faithful. Small gaps at wrapped lines are unexplained.

**Instability (VERIFIED from git).** The text log changed design at least three times: append-only (2026-08-15,
`3fe6767` per a Claude transcript, UNVERIFIED hash), whole-tab snapshot from 2026-08-17, then append again in
`f378b85` (2026-09-18). Commit `3ef8262` ("Keep lines that scroll off the tab in the session text log",
2026-09-18) exists in the object store but is NOT reachable from `main`: that work was discarded.

**Deviations from the owner's rules.**
- `settings_debug_log` and `raw_debug_log` are switched on by the `AI_TERMINAL_DEBUG` environment variable, not by a
  setting (rule 1). The 2026-09-19 Vibe session showed the effect: debug output flooded the Python console.
- `color_scheme_log` is now a hook that does nothing (rule 1). It was live diagnostics until about 2026-08-18
  (owner's account), so it is retired, not always dead.
- `settings_debug_log` has no test (rule 2).
