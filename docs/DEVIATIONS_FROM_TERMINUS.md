# Deviations from Terminus

Rule (user, 2026-09-20): Terminus is the reference. Every deviation must have a plain-language
justification here. "No justification found" is a defect, not an acceptable entry.

Status key: VERIFIED = read in both code bases, file:line given. UNVERIFIED = claim from an agent transcript.
Terminus checkout: ~/tools/Terminus, commit 0cccd3f (2025-12-16), 4,029 lines in the core.

> **Note (2026-09-21):** the owner deleted the whole `tests/` folder that day. References to tests in entries dated on or
> before then describe what was checked at the time; they are not ongoing protection. Changes are now proven live in Sublime
> (`AGENTS.md`, rule 7a).

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

**Full catalogue completed, 2026-09-22 (VERIFIED, every name above re-checked against current line numbers
and call sites — `grep` for each function's own name across the whole file).** Two of the "eight" are dead
code: **only 6 of the 8 originally-named writers still write anything.**

| Writer | Current line | Status | Call sites / role |
|---|---|---|---|
| `_pin_terminal_viewport` | 5804 | Live | Thin wrapper around `_pin_viewport_rest_dip_only`; 2 call sites, both in the mouse-wheel scroll command path (re-pin after `_route_mouse_wheel`) |
| `_pin_viewport_rest` | 7104 | **Dead code** | Zero callers. Superseded by `_pin_viewport_rest_dip_only` everywhere on 2026-09-19 (commit `57624a0`) — see section 8's alt-screen-Settings-panel finding, which traces the same commit |
| `_pin_viewport_rest_dip_only` | 7119 | Live | 5 call sites (`_pin_terminal_viewport`, `_resync_viewport_after_height_change`, `_settle_viewport`, and twice in `AiTerminalRenderCommand._run`). The one real "pin to rest, negative overshoot only" function everything now funnels through |
| `_compensate_trim_scroll` | 7210 | Live | 1 call site (the render loop). Corrects the text-slide the whole-buffer replace causes on scrollback eviction (section 6's root cause). Bypasses the kill switch deliberately (own docstring) |
| `_scroll_to_bottom` | 7279 | Live | 11 call sites across copy-mode exit, several key/click handlers, `_settle_viewport`, `_post_render_follow`, `_resync_viewport_after_height_change`. The "snap to live prompt" primitive |
| `_settle_viewport` | 7436 | Live | 1 call site (the render loop). Dispatches to `_pin_viewport_rest_dip_only` (TUI) or `_scroll_to_bottom` (following, non-TUI) depending on state — implicated in the open jiggle defect below |
| `_vp_pan_to_tui_scroll` | 9891 | **Dead code** | Zero callers. Already independently found and listed as dead code in section 10's 2026-09-21 entry ("Found while doing this, not touched") — that finding was not cross-referenced into this section when it was first written, causing this same fact to read differently in two places in one document until this pass |
| `_clamp_vp_loop` | 9930 | Live | Self-rescheduling, 500ms. Two jobs: the height-change detector (a real Sublime-event gap, see below), and — since job 1 turned out to be dead-code-adjacent, see the "Timer vs. event" correction below — a dip-only overshoot/dx correction across three view-state branches |
| `AiTerminalViewListener._preclamp_vp` | 5334 | Live | **Missed by the first catalogue, found live 2026-09-22.** Runs on `on_hover`, `on_activated` and `on_deactivated`. Until 2026-09-22 it forced (0,0) on any drift when content fit within one line, the one direction-agnostic writer `57624a0` did not convert; it snapped a deliberate scroll down to the toolbar back to the top. Now dip-only, targets `rest` (see section 8) |
| `_page_scroll` | 7351 | Live | **Missed by the first catalogue.** Ctrl+PageUp/PageDown and plain PageUp/PageDown (non-TUI). A direct, user-requested move, not a correction |

**Correction, 2026-09-22 (VERIFIED):** the "full catalogue" above was first built by grepping the known function names,
not every call site, so it missed the last two rows. Rebuilt from `grep -nE '_set_viewport\(|set_viewport_position'`
over `ai_terminal.py`: every call site is now in this table.

`_resync_viewport_after_height_change` (7388) is not a 9th writer: it only ever calls `_scroll_to_bottom` or
`_pin_viewport_rest_dip_only`, both already in this table.

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

**Timer vs. event, resolved for one of the two jobs (VERIFIED, code read, 2026-09-21; corrected 2026-09-22
after cataloguing every viewport writer for this section — see below).** `_clamp_vp_loop`
(`ai_terminal.py:9930-...`) does two separate jobs on its 500ms tick:
1. **Correction, 2026-09-22: this is NOT what job 1 does.** The original version of this entry said job 1
   converts trackpad pan into PTY scroll via `_vp_pan_to_tui_scroll` — wrong. `_vp_pan_to_tui_scroll`
   (9891) is dead code: `grep` finds zero call sites anywhere in the file. It was already caught and listed
   as dead code once before, in section 10's 2026-09-21 entry ("Found while doing this, not touched") —
   that finding was not cross-referenced when this entry was written the same day, so the same fact got
   contradicted in two places in one document. The loop's real remaining job, confirmed by its own comment
   at the `tui_like` branch ("No pan→PTY and no hard-pin... Only fix the negative overshoot glitch",
   `ai_terminal.py:10020-10024`), is a dip-only correction: fix a negative viewport overshoot (or nonzero
   horizontal drift) back to `rest`, across three branches (`tui_like`, `near_fit`, tall-scrollback) — never
   a forward/pan-to-PTY conversion. The `# Every ai_terminal view: trackpad = core pan. Convert + pin.`
   comment a few lines into the function (9937) is itself stale for the same reason: no "Convert" happens
   any more, only "pin." Whether Sublime has an event for a raw viewport-position change is now moot for
   this loop, since it does not act on that signal at all any more — only on the height check below and a
   dip below `rest`, neither of which is "trackpad pan."
2. The height-change detector (comment from 9945 onward, current numbering) exists because panel/sash/
   sidebar/resize can change `viewport_extent()` with no user text edit. The code comment argues this "is
   not just an enumerable list of commands" — a sash drag between groups, for example, is a raw mouse
   operation with no `on_post_window_command` hook at all in the public API (checked against Sublime's
   documented `sublime_plugin` event list: no `on_layout`/`on_group_resize` event exists). Panel show/hide
   (find, console, replace) IS a command (`show_panel`/`hide_panel`) and could be caught by
   `on_post_window_command`, but a sash drag could not. **This half of the loop cannot be fully replaced
   by events, for the missing-hook reason above, though a command-triggered event could cut how often it
   needs to poll for the panel sub-case.**
Conclusion, corrected: the height-change detector is the one part of this loop that genuinely has no
Sublime-event equivalent, so a poll is justified there. The dip-only overshoot correction (job 1, as it
actually exists today) is a much smaller ask than the original "trackpad pan → PTY scroll" description —
whether IT needs a 500ms poll specifically, versus e.g. running only inside the existing render loop, was
not re-examined under this corrected understanding and is now open again, not closed. The 500ms interval
itself remains a separate, already-settled tuning decision (line 48 above).

**Open defect (user report, 2026-09-20).** With Claude Code CLI in a tab, the text jiggles one line
up and back down during command-line typing. Cause not yet identified.
**2026-09-22, corrected the same day.** An earlier note here said the jiggle was gone after the line-by-line redraw
(section 6). It was not: the owner saw it again minutes later, triggered by the app's status updates, not by keystrokes.
Measured live in the Claude tab (VERIFIED, every viewport write logged) it had two causes, both GhostShell code:
1. **Trailing-row trim.** `trim_display_rows` (`terminal/render.py:127`) drops blank rows at the bottom every frame, so the
   tab's height followed Claude Code's status footer (350 -> 348 -> 350 -> 351 rows), and the follow code chased the
   bottom each time. Terminus trims the same way (`terminus/render.py:251`, `trim_trailing_spaces`), so matching Terminus
   does not fix this. Fixed by `steady_screen_height_enabled` (new deviation, section 6): once shown, a row stays.
   Afterwards: 0 row-count drops in about 200 frames.
2. **Scrollback-eviction compensation fighting follow.** Old lines are only evicted while following (`Screen.trim_paused`
   is on whenever follow is off). `_compensate_trim_scroll` then moved the view up by the evicted lines, and
   `_scroll_to_bottom` moved it straight back down in the same frame (4185 -> 4157 -> 4185 on every eviction). The
   compensation write bypasses `_set_viewport`, so a recorder wrapping only that function missed it at first. Fixed by
   `compensate_trim_while_following` (default false): 6 evictions over 55 frames then moved the view 0 px. This repeats
   the 2026-08-23 attempt recorded as reverted in that function's docstring; the difference now is the two changes above.
After both changes the view itself no longer moves (146 frames, 0 px, row count constant). The owner still sees the response text move down and back up inside the still view while the agent writes, and at times the command line. That is the text changing, not the view scrolling. The owner judged this to be the TUI's own redraw, not GhostShell (live observation, 2026-09-22; not cross-checked in Windows Terminal). GhostShell already defers painting during synchronized output (mode 2026, `ai_terminal.py:4803`). The GhostShell jiggle is closed.

**Viewport fixers measured and trimmed, 2026-09-22 (VERIFIED live, every write logged).** `_post_render_follow` (a 35 ms timer after every following frame, written because a whole-buffer replace left `layout_extent` stale) ran 194 times, including in a new PowerShell tab growing from 20 to 351 rows, and never moved the view. **Removed.** Retest without it: after a 400-line burst the prompt was still at the bottom. The height-change detector in `_clamp_vp_loop` did move the view when the console panel opened, so it stays. It now anchors the bottom: `_resync_viewport_after_height_change` moves the view by exactly the height change, because `_scroll_to_bottom`'s "last line visible anywhere" tolerance left blank lines below the text when the panel closed. Owner confirmed live. Also removed: a second, identical `_pin_viewport_rest_dip_only` call after `_settle_viewport` for full-screen apps (the first call leaves nothing for it to do). Live check with vim in a PowerShell tab: the view scrolled to 120 px stayed there through vim redraws in 2 logged runs. In an earlier, unlogged run it went back to 0; that writer was not identified.

**Related observation, Vibe, 2026-09-22 (logged live, cause NOT assigned).** With the view scrolled down to show the
toolbar (y=258) and the caret on Vibe's input row (row 49), typing moved the view about one line further down on each
frame (314, 328, 342 ... 412). Instrumentation showed the position was unchanged inside `AiTerminalRenderCommand.run`
and changed only after it returned, with no GhostShell viewport write logged at all (every `_set_viewport` call was
wrapped). When the caret left row 49 the view went back to exactly 258. Every one of Vibe's frames was a full-buffer
replace, never a patch (section 6). A second run, driven by Claude, could not isolate it (the view moved before the first
key was sent), and the owner reported it had stopped shortly after, so it is recorded here and not chased further.

## 2. Kill switch is a code constant, not a setting — FIXED 2026-09-21

`_SCROLL_MANIPULATION_ENABLED = True` (`ai_terminal.py:2308`, was `2356` when this entry was written).
Its comment said "NOT yet the default anywhere live", but it was True. The comment was stale. As a
constant it could not be tuned without editing code, which conflicted with rule 7 ("everything editable
via settings").

**Fix (VERIFIED, live-tested in the running Sublime, 2026-09-21).** Converted to
`scroll_manipulation_enabled` in `ai_terminal.sublime-settings` (next to `scrollback_history_size`,
same doc-comment style as the four existing cursor bisection gates), read through
`_scroll_manipulation_enabled()` -> `_setting_bool("scroll_manipulation_enabled", True)`
(`ai_terminal.py:2308-2317`), the same resolution pattern (profile override, then global, then default)
every other live-tunable gate in this file already uses. `_set_viewport`, the single choke point for
every viewport write, now calls this instead of reading the module constant.
Verified live via `eval_python` in the one running `ai_terminal` session (window 2, view 18, profile
"Claude" — this very conversation's own tab): the plugin's file-watcher already auto-reloaded
`ai_terminal.py` after the edit (per rule 11) without disrupting the running PTY (`term.pty.is_alive()`
stayed `True` throughout); `_scroll_manipulation_enabled()` correctly read `True` from the new setting,
then flipping the live settings object to `False` and back to `True` changed the function's return value
immediately, matching the documented "no reload needed" behaviour of the sibling gates. No restart was
needed or performed — per rule 11 ("never kill a running session"), and there was exactly one live
session to protect.

## 3. History cap (VERIFIED, justification now checked)

Terminus `scrollback_history_size` default 10000 (`render.py:123`). GhostShell caps at 300.

**Justification, checked against the code comment 2026-09-21.** `ai_terminal.sublime-settings:934-942`,
the setting's own comment: "Number of lines kept in scrollback history (the `history` deque). This is
the minimap-fill knob: at font 14 / 685px viewport, 300 fills the minimap exactly -- rigorously tested,
deliberate (the whole buffer top-to-bottom is always visible in the minimap at once)." It also warns
against raising this to fix trim-induced viewport jumps, naming `trim_paused` + `_compensate_trim_scroll`
(section 1's jiggle mechanism) as the right tool for that instead — i.e. the comment's author already
knew 300 is unrelated to the jiggle problem, matching what the section 1 trace found. This is a written,
specific justification (a measured screen/font combination, called "rigorously tested"), not the vaguer
secondhand paraphrase ("300 was tuned so the whole buffer fills the minimap") this entry previously
carried from an agent transcript. **UNVERIFIED still:** whether "rigorously tested" refers to an actual
test that exists somewhere, or is the comment author's own characterization with no artifact behind it —
no such test was found in this pass.

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

**Dead brokers' files, 2026-09-22 (VERIFIED live).** A broker removes its registry record and 2 MB `.scrollback` only in its own `finally` (`tools/agent_broker.py:1220`), so one ended from outside (Task Manager, a crash) left both forever. Found: three such records from 09-18 and 09-21; the owner's own note in the 01:27 session recording confirms two were ended from Task Manager. `_registered_brokers` now removes the files of a record whose broker `_broker_confirmed_dead` proves gone (no such process, exited, a different program on the PID, or a start time 5+ minutes off); access denied or any failed query keeps the record. Live run: the three dead records and their 6 files removed, both live sessions kept and still recoverable.

**Nuke and the broker, 2026-09-22 (VERIFIED live).** Nuke (ctrl+alt+k) clears the tab and the terminal engine but not the broker's saved scrollback (`_Scrollback`, `tools/agent_broker.py`), so a reattach after a Sublime restart replays the nuked lines. **Owner's decision: leave it.** The broker copy acts as an undo for an accidental Nuke, and pressing ctrl+alt+k again after the restart clears it.

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

**Mouse routing follows Ghostty, 2026-09-23 (VERIFIED live; owner's design).** Terminus forwards no mouse events at all (`terminus/mouse.py`: a plain click only moves the cursor), so this whole path is a GhostShell deviation; its reference is now Ghostty (`~/tools/ghostty`), which cmux uses unchanged. Rules: (1) mouse events go only to an app that asked for them, in its own mode (`_app_wants_mouse`, like Ghostty's `isMouseReporting()`, `src/Surface.zig:3640`); `mouse_handling` / `wheel_to_pty` are the settings and their labels say exactly this. (2) The wheel goes to such an app as wheel events only, never arrow or page keys (the old translation is removed), and the mousemap now binds `scroll_up`/`scroll_down`, without which no scroll ever reached the plugin. (3) A click sent to the app first clears Sublime's selection (Ghostty `Surface.zig:3910`); before, a selection left behind froze painting. (4) A click that goes to the app first focuses its pane. (5) Text Edit Mode (`copy_mode`, Ctrl+Alt+C) hands the mouse back to Sublime as well as the keys, for one tab, in memory: Ghostty's `toggle_mouse_reporting` role, chosen by the owner over a separate mouse toggle; reachable from the toolbar (shows the state), the Ai Terminal menu and the Command Palette. Shift/Ctrl/Alt are not used as a mouse escape hatch (owner: they belong to Sublime's text selection). An intermediate "always send, even if the app did not ask" design (2026-09-22) was tried and dropped: no terminal does it, and the apps that did not ask just ignored the events. Evidence: PowerShell received clicks and wheel steps as Windows mouse events through ConPTY, each at its sent timestamp; direct calls on a Claude tab (switch on, app not asking) and a real Vibe tab (asking, tracking 1003) gave the expected routing; the owner confirmed focus-on-click, the menu item and the toolbar toggle live. A test click aimed at column 5 that encoded as column 1 was checked 2026-09-23 and is not a bug: the aim was on a row shorter than 5 characters, where `text_point(row, 5)` lands on a later line. `_event_to_pty_cell` round-trips exactly where the point exists (columns 0, 5, 20 -> cells 1, 6, 21), for a visible and a hidden tab alike.

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

**Partly fixed, 2026-09-22 (VERIFIED live).** New setting `line_diff_render_enabled` (default true): each frame replaces
only the lines that differ, last line first, as Terminus does. Lines added or removed at the end are inserted or erased
there. The native per-row dirty state is not used yet: this compares old and new text in Python. Measured in the Claude
tab: every frame took this path after the line-count case was added. This alone did not stop the jiggle (see section 1's
2026-09-22 correction).

**New deviation, 2026-09-22: steady screen height.** Terminus drops blank rows below the cursor after each render
(`terminus/render.py:251`), and so did GhostShell (`trim_display_rows`). With an app whose status footer changes height,
that makes the tab shrink and grow and the view jump. `_keep_screen_height_steady` (`ai_terminal.py`, setting
`steady_screen_height_enabled`, default true) lets the number of trimmed rows only shrink, the way a real terminal's
screen never changes height. It starts over on a resize or a switch between the normal and alternate screen.
Justification: the 350 -> 348 -> 350 -> 351 row data in section 1.

**Feasibility of switching to dirty-line updates (VERIFIED, read-only investigation, 2026-09-21, no code changed).**

The native engine already computes per-row dirty state and the Python side already throws it away:
- `terminal/ghostty_engine.py:_sync_grid` (690-765) reads `RENDER_STATE_DATA_DIRTY` (the whole-frame flag)
  and, if set, walks rows via `RENDER_STATE_DATA_ROW_ITERATOR` and checks `RENDER_STATE_ROW_DATA_DIRTY`
  per row (730-736), writing `s.grid[y]`/`s.attrs[y]` only for rows the native engine flagged. This is
  real per-row dirty tracking, already used to skip unnecessary FFI cell walks.
- That per-row information is discarded immediately after: `_sync()` (868-872) collapses everything to
  one Python bool, `s.dirty = True` (also `terminal/screen.py:85` and every other `self.dirty = True` in
  that file). `_do_render` (`ai_terminal.py:4716`) only ever asks "is anything dirty", never "which rows".
- So the raw material for a Terminus-style per-line update already exists at the native boundary; it is
  deleted one call later. Recovering it means `_sync_grid` returning (or accumulating) the set of dirty
  row indices instead of folding them into a bool.

Three things make GhostShell's buffer layout harder to line-diff than Terminus's, found while reading
`terminal/screen.py` and `ai_terminal.py`'s render path:
1. **The visible row count is not fixed.** `render_cells()` (`terminal/screen.py:364-387`) concatenates
   history + the full `self.rows` grid rows every call; `trim_display_rows` (`terminal/render.py:127-147`)
   then drops trailing blank rows based on where the last non-blank content and the cursor (`cy`) are.
   Terminus's pyte screen is a fixed `rows` grid — the trailing-blank trim is GhostShell's own addition
   ("wall of blank lines below the cursor", comment at `ai_terminal.py:4722-4727`) and it recomputes the
   kept row count on every frame from cursor position, not just from cell content. A cursor moving down
   one line can shift the total line count even when no cell changed.
2. **Extra pad lines are prepended/appended.** `_HOST_SCROLL_PAD_LINES` / `_append_host_scroll_pad`
   (`ai_terminal.py:4756-4764`) add blank lines above and below the real content "trackpad can pan both
   ways", and colour-region + caret offsets are shifted by the pad size after the fact. Any line-diff
   scheme has to account for this fixed offset, not just the dirty rows themselves.
3. **Scrollback retirement shifts every grid row's buffer-line index.** When a line retires from the grid
   into `history` (`terminal/screen.py`, `_retire_line`/`_enforce_history_cap`), every subsequent frame's
   grid rows sit one line further down in the concatenated text. This is exactly the shift that
   `_compensate_trim_scroll` currently patches over at the viewport level (section 1) because the whole
   buffer gets rewritten anyway. A line-diff renderer would instead need to compute
   `delta = new_len(history) - old_len(history)` once per frame and treat it as an insert of `delta` new
   lines at the history/grid boundary, not a rewrite.

None of these are blockers — Terminus's own `update_lines` (`render.py:150-154`) handles the history-trim
case by choosing not to re-anchor when the user has scrolled away, which is the same problem in a smaller
form. But points 1-3 mean this is not a small textual change to `AiTerminalRenderCommand`; it is:
- a change to `ghostty_engine.py` to preserve the row-dirty set instead of collapsing it,
- a decision about whether the trailing-blank trim (point 1) and scroll pad (point 2) are kept, reworked
  to be diff-friendly, or dropped,
- a rewrite of `_do_render`/`AiTerminalRenderCommand` (`ai_terminal.py:4656`, `7964-8060`) to walk dirty
  row indices, map each to a buffer line via the history-boundary delta, and call `view.replace` per line
  instead of once for the whole buffer (subsuming `fast_caret`, which becomes unnecessary once line-level
  patching exists),
- removal or simplification of `_compensate_trim_scroll` (7841) and probably `_settle_viewport` (8067),
  since their reason for existing (whole-buffer rewrite disturbs the viewport, section 1) goes away,
- a correctness fallback: unlike Terminus, GhostShell already has one confirmed history of the Sublime-side
  buffer drifting from the emulator's own state (the state-copy problem named in `ai/RECOVERY_PLAN.md`).
  A line-diff renderer trusts that the dirty set is complete; a periodic full-resync safety net (e.g. every
  Nth frame, or on any detected mismatch) is worth keeping so a missed dirty flag self-heals instead of
  leaving a stale line on screen forever — this has no counterpart requirement in Terminus, which VERIFIED
  has no state-copy step at all (pyte's screen *is* the render source).

**Estimate:** medium-sized, self-contained change (four files: `ghostty_engine.py`, `screen.py`,
`render.py`, `ai_terminal.py`'s render path), correctness-sensitive, and it should retire several existing
workarounds (`_compensate_trim_scroll`, `_settle_viewport`, `fast_caret_patch_enabled`) rather than sit
alongside them. Per rule 7a it would need to be proven live in Sublime (typing, scrollback trim while
scrolled away, a TUI full-screen redraw, and a window resize), not by a unit test. Not started; no code
changed in this investigation.

Why no reason can be found (owner, 2026-09-21): several purges of logs, transcripts and memory files have
deleted the older records, so a reason that was once discussed may have existed and is now unrecoverable.
Do not search for it again; decide on the evidence in the code. Evidence gathered live 2026-09-21 with a
`set_viewport_position` trace in the running Sublime (VERIFIED, session observation): on every history trim
`_compensate_trim_scroll` (`ai_terminal.py:7153`) moves the view up one or more lines and `_settle_viewport`
(`ai_terminal.py:7336`) moves it back to the bottom in the same render call, and the owner still sees a
one-to-three-line up-and-down wobble while typing. A whole-buffer replace cannot be made steady by
repairing the scroll afterwards; a line-level update avoids the shift at its source.

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

**Partial justification found, and a real defect identified (VERIFIED, code read, 2026-09-21).** Terminus's
`generate_theme_file` (`~/tools/Terminus/tools/theme_generator.py:47-124`) only builds scopes for its 16
named ANSI colours by default (`color256_scopes=False`) — `terminus.<u>.<v>` for `u, v` in 16 colours (plus
`default`/`reverse_default`), roughly 18x18 = 324 rules, written once, using `var(...)` references into a
small variables table rather than literal hex per rule. That is why Terminus's file is small and never
regenerated at runtime.

GhostShell's colour space is not the same size. `terminal/colors.py` quantizes every truecolor cell to the
nearest of the full **256**-entry xterm palette (`quantize256`, line 34) before building a scope name
(`ai.fb.<fg>.<bg>` or `ai.fb.<fg>.<bg>.s<style>`, `scope_name_for`, line 153), where `fg`/`bg` each range
0-256 (0 = default) and `style` is a 3-bit bold/italic/underline combination (0-7, `style_id_for`, line 189).
The full addressable space is therefore up to 257 x 257 x 8 = **~528,000** possible scopes — roughly 1,600x
Terminus's ~324 — because GhostShell renders the real 256-colour xterm palette CLIs actually emit (Claude
Code, ratatui apps, etc. all use 256-colour/RGB SGR codes routinely), not just the 16 named ANSI colours
Terminus targets. Pre-generating that entire space up front, Terminus-style, would produce a far larger
static file than today's already-large one (528,000 rules vs. the current 5,948). Registering scopes lazily
as they are actually seen (`_register_scope_async`, `ai_terminal.py:2033`) is a real, defensible answer to
that size problem — it keeps the file only as big as the colours actually used, which is why the design
exists at all.

**The defect is that "lazily" never means "temporarily."** `_REGISTERED_SCOPES` (`ai_terminal.py:1692`) is
a set that is only ever added to (`.add`, lines 1839/1862/1868/2025/2041) across the whole file — no removal
path exists anywhere. A colour combination used once, in one session, stays a permanent rule in the shared
`ai_terminal.sublime-color-scheme` file forever, for every future profile and session, even after the
profile that produced it is never used again. This is the mechanism behind section 9's measured growth
(5,140 rules on 2026-07-10 -> 423,061 bytes by 2026-08-18 -> 450,593 bytes now): it is not a leak in the
sense of wasted memory, but it is unbounded on-disk growth with no cap, which conflicts with rule 15 (every
generated file needs a hard size cap; this one is unrelated to logging but is the same kind of ungoverned
file). A bounded design consistent with both Terminus's approach and GhostShell's actual colour range would
be either (a) an LRU/TTL eviction of scopes unused for N sessions, or (b) switching to `var(...)`-style
rules referencing a small palette table (Terminus's trick) so the *rules* stay proportional to distinct
(fg, bg, style) triples actually seen, without literal hex duplicated per rule — worth comparing against (a)
for file-size impact before choosing. Neither exists today.

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

**Appears already fixed the same day, code-verified 2026-09-21; not yet re-confirmed live against a real
alt-screen app.** Commit `57624a0` (2026-09-19 14:40) replaced every `tui_owns_scroll`/`_tui_like` call to
the old direction-agnostic `_pin_viewport_rest` (which snapped the viewport back to the top on ANY drift,
including a deliberate downward scroll toward the toolbar) with `_pin_viewport_rest_dip_only`
(`ai_terminal.py:7020-7052`), which by design only corrects a *negative* overshoot above rest and leaves a
positive/downward scroll alone (its own docstring: "a deliberate forward scroll past rest... is left
alone"). Checked in the current file: `_pin_viewport_rest` (the old hard pin) is called nowhere any more —
`git grep` finds it only at its own definition; every one of the four `tui_owns_scroll` call sites
(`_settle_viewport:7344`, `AiTerminalRenderCommand._run:8206`, and `_clamp_vp_loop`'s `tui_like` branch)
now uses the dip-only version. That means the specific mechanism Grok described — the viewport being
forced back to the top on every frame regardless of direction — no longer exists as a live code path,
for either the render loop or the 500ms clamp loop. Whether the same commit predates or postdates Grok's
2026-09-19 reading is not established from the doc's date-only citation, so this is recorded as "appears
fixed by code inspection," not "confirmed fixed" — it has not been re-tested against a real alt-screen TUI
(e.g. vim, htop) live in Sublime, which rule 7a requires before closing this out. Not done in this pass
because it would mean opening a new tab/session, which needs the owner's go-ahead.

**Live test, 2026-09-22 (VERIFIED): not fully fixed; one more writer found and fixed.** With Vibe (alt-screen) in a tab,
the owner could push the text up to show the toolbar, but it snapped back intermittently. Every viewport write was
wrapped and logged with its call stack. Both snaps came from `AiTerminalViewListener._preclamp_vp`
(`ai_terminal.py:5334`), triggered by `on_hover` and by `on_deactivated`: layout 752 vs viewport 743 is within one line,
so its "content fits" check applied, and it then forced (0,0) on any drift in either direction. The render loop and clamp
loop behaved correctly. `_preclamp_vp` is now dip-only, like the others: it corrects a horizontal drift or a position above
`rest`, and targets `rest` instead of (0,0). With that patch live, about two minutes of real use logged zero viewport
writes, and the owner confirmed the snap-back had stopped.

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

**New finding, VERIFIED code read 2026-09-21: an unconditional writer, not behind any switch.**
`_durable_scheme_backup` (`ai_terminal.py:1925-1952`) writes a full `.sublime-color-scheme` snapshot
(~400KB+, growing with the scope-registration problem in section 7) to
`~/data/logs/ai_terminal/scheme_backups/` every time the live scheme is saved and has 100+ rules — which
is true for nearly any session running a colorful CLI for more than a few minutes. Unlike every other
recorder in this section, this one has **no settings key, no `AI_TERMINAL_DEBUG` gate, and no dev switch
at all** — it is unconditional for every installation of the package. It keeps only the newest snapshot
(`bak[1:]` removed, lines 1939-1949), so it is at least bounded to one file, but that one file has no size
cap (rule 15's default 32KB is nowhere close; a 400KB+ file with no cap). This is a plainer, more direct
violation of rule 14 ("must not write anywhere except Sublime's standard locations... never to `~/data`...
off by default until the owner's own dev switch is turned on") than the already-known loggers, because
those are at least off by default; this one is always on. `_color_scheme_log` calls inside it
(lines 1949, 1950, 1952) are dead — that logger is the stub noted above — but the file write itself is
real and runs regardless.

**Paths still hardcoded to `~/data/logs` (VERIFIED, not fixed in this pass — rule 13 requires the owner's
approval before moving or creating any log location, so this is recorded, not acted on).**
- `terminal/log_paths.py:14`: `LOG_ROOT = os.path.expanduser(os.path.join("~", "data", "logs"))`, a module
  constant, used by `session_text_log.py` and `cast_recorder.py` (both gated off by default via
  `log_tab_text`/`record_asciicast`, so at least opt-in) and by `raw_debug_log.py` (gated by the
  `AI_TERMINAL_DEBUG` env var, not a setting — already flagged above).
- `ai_terminal.sublime-settings:48`: `"color_scheme_log_path"` still defaults to a `~/data/logs/...` string,
  even though the logger it feeds is now a no-op stub — the setting itself is a personal path shipped in
  the repo's own defaults.
- `_durable_scheme_backup`'s `~/data/logs/ai_terminal/scheme_backups` path above, the most urgent of the
  three because it is the only one that is both hardcoded AND unconditional.
None of these are Sublime's standard locations (`sublime.cache_path()` or `Packages/User`) as rule 14
requires for a release. Fixing this needs a decision on where the default should point (likely
`sublime.cache_path()`) and, for `_durable_scheme_backup` specifically, whether it should exist by default
at all or become opt-in like its siblings — both are the owner's call before any file is touched.

## 10. Usage and quota scanning (VERIFIED in code; the biggest trust issue found)

**Terminus.** Nothing comparable.

**GhostShell.** `terminal/usage_scan.py` (1,119 lines). Every `usage_refresh_minutes` (20) it reads other programs' saved
OAuth credentials and calls the providers' servers to show quotas in the launcher and menus. Verified in the code:
- Files read: Codex `auth.json`; Claude Code `~/.claude/.credentials.json` (access AND refresh token); also opencode, Kimi,
  Mimo, GitHub Copilot and OpenRouter credentials.
- Servers called: `api.anthropic.com/api/oauth/usage`, `chatgpt.com/backend-api/wham/usage`,
  `api.github.com/copilot_internal/user`, `api.kimi.com`, `openrouter.ai/api/v1/key`, and others.
- **It rewrites another program's login.** For Claude, when the access token has expired it performs the OAuth refresh-token
  grant using Claude Code's own client id and a `claude-cli/2.0 (external, cli)` user agent (`_refresh_claude_token`,
  `usage_scan.py:637`), which rotates the refresh token, and writes the new tokens back into Claude Code's
  `.credentials.json` (`_persist_claude_oauth`). Its own docstring says a failed write "costs the user their CLI login".
- Kimi tokens are refreshed the same way (`auth.kimi.com/api/oauth/token`).

**Stated purpose (code comments).** Accurate quota windows (5h, weekly, reset times) straight from the provider, at no
inference cost, so the launcher and menus do not show stale figures. The setting comment says it is "Off by default for a
public release".

**Defect found 2026-09-21.** The committed default was `"usage_scan_enabled": true`, contradicting its own comment ("Off by
default for a public release"). It was first changed to `false` (`6c348bb`).

**Decision and removal (owner, 2026-09-21; commit `6cbad2c`). The whole scanner is gone.** The owner had wanted omp's method,
but omp authenticates as an authorised app registered with each provider, which GhostShell cannot do. What GhostShell did
instead was borrow other CLIs' tokens and client ids, which is the part that cannot be shipped. omp already provides usage and
quota correctly (`omp usage`), so it was removed rather than reworked. Removed: `terminal/usage_scan.py` (1,119 lines), the
scanner threads and refresh timer in `ai_terminal.py` (now 240 lines shorter), the "Refresh Usage & Quota" command, palette
entry and menu item, the `usage_refresh_minutes` and `usage_scan_enabled` settings, and 92 tests. First pass verified on a copy, then in the
repo: 501 tests pass; the same 2 unrelated tests fail as before the change (`test_history_scan` antigravity variants, and
`test_launcher_flow` history opens text sessions). `tools/check_import.py` passes. The running Sublime keeps the old code in
memory until its next restart.

**Second pass, same day: no usage display at all (owner: "the command was to remove ANY usage from the codebase").** The
first pass had kept usage read from terminal output, which still drew "Installed — no usage data" on every row of the
Launch Agent list, plus "64% left, resets 3h" in menu captions, a "Usage:" line in the session-info view, and a
"quota exhausted" marker. All removed: `_observed_usage`, `_profile_is_exhausted`, `_with_reset`,
`_record_profile_usage`, `_usage_annotation`, `usage_update_from_text`, `reset_update_from_text`, and the `exhausted`
argument of `profile_kind`. A row now shows only availability ("Not installed" when the program is missing, nothing
otherwise). `ai_terminal.py` is 10,629 lines (from 10,951). `tests/test_no_usage_in_code.py` scans the code and fails if
usage or quota text or the old function names reappear. 489 tests pass; the same 2 unrelated tests still fail.

**Uninstalled agents hidden (owner, 2026-09-21: "if it is not installed it should not be in the list or not be
selectable").** This reverses an earlier design, recorded in the old docstring of `_profile_items`, that kept unavailable
profiles visible and marked because hiding them "breaks muscle memory and hides the reason". Now the Launch Agent list
offers only installed agents (`AiTerminalLauncherCommand.run`), falling back to Open Here when none are installed, and
the menu entries for missing agents are hidden (`is_visible` on `AiTerminalOpenHereCommand` and
`AiTerminalOpenInEditorCommand`). The "Not installed" row marker, `profile_availability_label` and the constants that only
existed to draw it were removed. The Cody profile and its catalog entry were removed the same day. 491 tests pass; the same
2 unrelated tests still fail.

**Agents launch from menus, not a picker (owner, 2026-09-21: "pickers must go ... put all the agents on a sub-menu, sorted
properly, like SublimeREPL").** This restores a design that existed on 2026-09-01/02: a generated "All Agents" menu and a
"Shells" menu (`830edaa`, `4e441d5`), removed on 2026-09-02 by `b5bf041` ("Consolidate agent-launch UI to one picker plus one
shortcut") as duplicates of the picker. Now: **Ai Terminal > Agents** and **Ai Terminal > Shells**, sorted A-Z ignoring case,
built by `tools/regen_agent_menu.py` from the agent catalog plus the profiles in `ai_terminal.sublime-settings`, with
`terminal/agent_menu.py` holding the logic and `tests/test_agent_menu.py` failing if the checked-in menu drifts. Agents that
are not installed are hidden at run time (`is_visible`). Removed from the menu, palette and keymap: "Launch Agent…"
(`ai_terminal_launcher`), its `Ctrl+Alt+N` chord, and the "Default Profile" item (the owner does not use a default profile).
Sublime has no API to add menu items while it runs; the official menus page documents only the static format and
`Packages/User` customization. A plugin can rewrite a menu file on disk and Sublime reloads it (verified live on 2026-09-02),
which would allow a recency-ordered menu, but that means writing into Sublime's folders on every launch, so it is not done.
Done the same day: the picker code (`AiTerminalLauncherCommand`, its folder picker, and the recent-agents file
`terminal/recent_profiles.py` added earlier that day) was deleted, and the sidebar's right-click menu got the same two
submenus. `ai_terminal.py` is 10,479 lines. `tests/test_agent_menu.py` fails if any of it returns. Still present: the
history picker (`ai_terminal_history`) and the folder picker that Open Here falls back to when no folder can be worked out.
Found while doing this, not touched: these names in `ai_terminal.py` were already unused before this work and are dead code
(`_BLANK`, `_REG_LOCK`, `_TERMINALS`, `_RELAUNCH_REQUIRED_PROFILE_KEYS`, `_follow_content_height`, `_looks_like_project_root`,
`_pin_viewport_rest`, `_sublime_view_info_lines`, `_vp_pan_to_tui_scroll`; and three `_CREATE_*`/`_DETACHED_*` constants that only
`tools/job_breakaway_test.py` refers to).

## 11. Agent catalog, history scan, launcher, availability (VERIFIED headers)

- `terminal/agent_catalog.py` (487 lines): a data table of known agent CLIs and the quirks each needs. Its docstring says it
  exists "so that knowledge survives a fresh settings file instead of being re-discovered". Generic, no personal paths or secrets.
- `terminal/history_scan.py` (268 lines): a read-only sweep of local agent history files, nothing written to disk, for the
  session-recovery listing. New agents are registry entries, not code.
- `terminal/launcher.py` (81 lines), `terminal/profile_availability.py` (125 lines): row formatting and local checks. The latter
  states it performs no network requests, provider probes, OAuth or inference.
Justification: these support "one launcher for many agents", which Terminus does not do. Not audited further in this pass.

## 12. ctypes documentation (owner's rule 5: every ctypes line has a proper name and a stated purpose) (VERIFIED by measurement)

Terminus uses no `ctypes` for its terminal (it uses the Python `pyte` emulator and Python pty modules). GhostShell uses `ctypes` for the
native Ghostty engine, the Windows ConPTY and the broker. Measured 2026-09-21 with `ctypes_audit.py` (method: a code line that uses
`ctypes` names counts as documented if it has a `#` comment, the previous non-blank line is a comment, or it is inside a function or
class that has a docstring; a generous test, because one docstring covers every line under it):

| File | ctypes lines | Documented | Share |
|---|---|---|---|
| `terminal/ghostty_vt.py` | 136 | 61 | 45% |
| `ai_terminal.py` (ConPTY client, key and mouse calls) | 95 | 45 | 47% |
| `tools/agent_broker.py` | 82 | 35 | 43% |
| `terminal/ghostty_engine.py` | 54 | 53 | 98% |
| `tools/recover_console.py` | 48 | 5 | 10% |
| `tools/job_breakaway_test.py` | 31 | 0 | 0% |
| `tools/agent_broker_client.py` | 17 | 0 | 0% |
| `terminal/mouse.py` | 7 | 3 | 43% |
| `tools/check_in_job.py` | 7 | 0 | 0% |
| **Total** | **477** | **202** | **42%** |

**Rule 5 is not met.** Only `terminal/ghostty_engine.py` is close. Suggested order of work, largest first: `terminal/ghostty_vt.py`
(the native engine bindings), the ConPTY code in `ai_terminal.py`, then `tools/agent_broker.py`. Three small scripts
(`tools/job_breakaway_test.py`, `tools/check_in_job.py`, `tools/recover_console.py`) look like one-off development tools from the broker
work in August; whether to keep them is the owner's call.
**Correction, 2026-09-22 (VERIFIED):** `tools/recover_console.py` is NOT a dev tool. It is the relay the toolbar's "Open in Windows
Terminal" runs (`ai_terminal.py:8528`, path built at `ai_terminal.py:796`). `tools/spawn_outside_job.ps1` is also needed at run time
(`ai_terminal.py:982`). `tools/` mixes these reattach/runtime files with experiments, which is how this misclassification happened.
**Owner's decision, 2026-09-22: keep every file; delete nothing.**

**`terminal/ghostty_vt.py` documented, 2026-09-21.** The `ctypes_audit.py` tool used for the table above no longer exists in the
repo (correctly, per rule 16 -- it was a scratch measurement tool), so an exact re-run of the same methodology against the same
numbers is not possible; a reimplementation of its described rule gives a different absolute line count for the same file (117
vs. 136) because the two scripts likely differ on edge cases (e.g. whether a `#` comment two lines above still counts). Treated as
directional, not a replacement for row 1's `45%`: the same reimplementation measured `41%` documented before this pass's edits and
`83%` after, on real source. Every docstring added was sourced from the actual C headers in `~/tools/ghostty/include/ghostty/vt/*.h`
(`color.h`, `style.h`, `point.h`, `grid_ref.h`, `terminal.h`, `mouse/event.h`, `mouse/encoder.h`, `types.h`), not guessed (rule 12) --
each new docstring names the header and struct it mirrors. Added: docstrings for `_BuildInfoString`, `GhosttyBuffer`,
`GhosttyColorRgb`, `_GhosttyStyleColorValue`, `GhosttyStyleColor`, `GhosttyStyle`, `GhosttyPointCoordinate`, `_GhosttyPointValue`,
`GhosttyPoint`, `point()`, `GhosttyGridRef`, `GhosttyTerminalOptions`, `GhosttyMousePosition`, `GhosttyMouseEncoderSize`,
`load_library()`, `_sha256_file()`, `_bind()` (the ~100-line FFI signature table -- one docstring explaining the `sig()` helper and
the `p`/`u16`/`u32`/`sz`/`i` type aliases, rather than per-line comments on a repetitive block), and
`mouse_encoder_setopt_int`/`_bool`. `.init()` on `GhosttyStyle`/`GhosttyGridRef` were left uncommented -- their enclosing class
docstrings already state what `.init()` does (sets `size` to `sizeof(...)`, the C API's "sized struct" convention), so a repeated
docstring there would be the kind of comment the project's own style guidance says not to write. Live-verified in the running
Sublime, 2026-09-21: `terminal.ghostty_vt` reloads cleanly (`hasattr(m, 'Ghostty')` True after `importlib.reload`) and the one live
`ai_terminal` session (this conversation's own tab) stayed alive (`term.pty.is_alive()` True) throughout. `tools/agent_broker.py`
is not started.

**`ai_terminal.py`'s ConPTY binding started, 2026-09-21 (same pass).** Every previously-undocumented Win32 struct in the top-of-file
kernel32 binding block now has a docstring/comment: `_COORD`, `_SECURITY_ATTRIBUTES`, `_STARTUPINFOW`, `_STARTUPINFOEXW`,
`_PROCESS_INFORMATION`, plus a comment on each grouped `argtypes`/`restype` block (ConPTY lifecycle, proc-thread-attribute-list,
`CreateProcessW`, the process-lifecycle group, the process-heap group) explaining what each function is for and, where relevant,
why the group exists at all (e.g. the heap group backs `InitializeProcThreadAttributeList`'s two-call size-then-alloc pattern --
confirmed against the real call site, not guessed). Also documented: `_Pty.start`/`_start_child`/`_close_pc`/`write`/`is_alive`/
`kill`/`_close_handles`/`_release_attr_list` (all previously undocumented despite the class itself having a one-line docstring --
that one-liner does not actually explain the ConPTY calls inside, so rule 5's spirit, not just its letter, called for more), and
the mouse-hover `_POINT` struct and `_hover_poll_tick` (Win32 `GetCursorPos`/`ScreenToClient`). All grounded in the standard,
well-documented Win32 APIs involved (ConPTY, process/thread creation, heap, cursor) -- no guessing. A crude reimplementation of the
line-counting audit undercounts this file specifically (it only matches the literal substring `ctypes`, missing the many lines
that use bare names imported via `from ctypes import ...`/`from ctypes.wintypes import ...`, e.g. `HANDLE`, `DWORD`, `byref`), so no
percentage is quoted here -- unlike `ghostty_vt.py` above, where that script's numbers were usable as a directional check.

**`_BrokerPty` and two bare-PID helpers documented, same day.** `_try_connect` (the CreateFileW/WaitNamedPipeW retry loop),
`read` (ReadFile loop plus the replay-marker boundary it also handles), `write`, `resize` (a text control-pipe line, not a ConPTY
call -- the broker owns the real ConPTY, not this client), and `is_alive` (no HANDLE at all on this side, unlike `_Pty.is_alive`)
now all have docstrings; `kill`/`explicit_kill` already had them plus thorough inline comments and needed nothing added. Also
documented: `_pid_is_alive` (OpenProcess+GetExitCodeProcess by bare PID, for checking a broker recorded in the on-disk registry
after a restart) and `_filetime_to_unix` (the FILETIME-to-Unix-epoch conversion GetProcessTimes' result needs, used by
`_broker_process_matches` to confirm a PID is the SAME process the registry recorded, not one Windows recycled the PID to).
Checked and found to need nothing: `terminal/keys.py`'s `encode_win32_key` is pure Python text-building (the DEC 9001
win32-input-mode escape sequence) with no ctypes at all -- the original table's "key... calls" for this file most likely meant
the mouse-hover code above, not this function; `_broker_registry_file`/`_broker_pipe_path`/`_broker_script_path`/
`_recover_console_script_path` are plain path-string helpers with no ctypes either.

**Not fixed, found instead: a fourth "owner's call to keep" script.** `tools/agent_broker_client.py` (140 lines, 0% documented in
the original table) is not broker-runtime code -- its own module docstring calls it a "minimal attach/detach test client for
agent_broker.py" for manual testing at a terminal, duplicating `_BrokerPty`'s connect logic standalone. Same category as the three
scripts row 12's original table already flagged ("one-off development tools... whether to keep them is the owner's call") --
documenting a script that might be deleted is not a good use of the remaining effort here, so it is only catalogued, not touched.

**`tools/agent_broker.py` fully documented, same day.** The actual broker server (the third and last of the three real,
definitely-kept ctypes files in this row) is done: the same 5 ConPTY structs as `ai_terminal.py` (identical layout, documented the
same way), the broker-specific named-pipe-server kernel32 group (`CreateNamedPipeW`/`ConnectNamedPipe`/`DisconnectNamedPipe`/
`FlushFileBuffers`) and the `IsProcessInJob` check `_current_process_is_in_job()` uses to confirm the broker actually escaped
Sublime's job object at spawn (see section 4's `DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB` justification -- this is how that
claim gets verified at runtime, logged once in `main()`). Documented every method of this file's own `_Pty` (the broker-side twin
of `ai_terminal.py`'s client-side `_Pty` -- one bug found while writing its docstring: an initial claim about where `_REPLAY_END`
gets written was wrong, corrected after checking the real call site, `_OutputServer.run_forever`, not `feed()`) and the three
named-pipe server classes -- `_OutputServer` (broker-to-client, write-only, the snapshot+marker handoff on connect),
`_InputServer` (client-to-broker, read-only, `force_disconnect`'s `CancelIoEx` hand-back from a Windows Terminal relay), and
`_ControlServer` (`RESIZE`/`KILL`/`DISCONNECT` on a duplex pipe, one thread per connection). `_Scrollback` (the ring-buffer
snapshot file) uses no ctypes at all -- out of scope for this rule, not touched. Verified (not live in Sublime -- this file runs
as its own separate process, never imported into the plugin host, so editing it cannot affect any already-running broker or this
session's own live tab): `python -m py_compile` and `python tools/agent_broker.py --help`, which re-executes the entire top-level
kernel32 binding block (every `argtypes`/`restype` assignment) under the real Windows ctypes runtime without error.

**Settings check, 2026-09-22 (VERIFIED, rule 7).** Every key in `ai_terminal.sublime-settings` was checked for a read in the code: 32 of 37 are read (3 more are profile names, read as a group). Two do nothing: `terminal_font` (its own comment, `ai_terminal.sublime-settings:1033`, calls it a dead key; tabs use Sublime's global font) and `color_scheme_log_path` (its comment says null disables the log, but `terminal/color_scheme_log.py` never reads it; logging is the owner's area). **Open, owner undecided:** both left as they are. Not yet done: a live test that each routing switch in section 5 changes behaviour as its comment says.

## 13. Status summary of every deviation from Terminus (2026-09-21)

| # | Deviation | Status |
|---|---|---|
| 1 | Several code paths write the viewport position, plus a self-rescheduling clamp loop (Terminus: one function, once per render) | State after 2026-09-22 (every write site grepped): 7 functions write the viewport. They are `_scroll_to_bottom` (follow), `_pin_viewport_rest_dip_only` (upward-overshoot fix), `_compensate_trim_scroll` (reading scrollback only; skipped while following), `_page_scroll` (PageUp/Down), `_resync_viewport_after_height_change` (panel open/close, bottom-anchored), `_preclamp_vp` (hover/focus, now dip-only) and `_clamp_vp_loop` (500 ms poll: height detector plus dip fix). The dead `_pin_viewport_rest` and `_vp_pan_to_tui_scroll` were removed in `5b4d4fd`; `_post_render_follow` (measured 0 moves in 194 runs) and a duplicate pin were removed 2026-09-22. The status-update jiggle is **fixed** (section 1 correction: steady tab height plus no compensate-vs-follow fight; the view moved 0 px in 146 frames). Clamp loop's dip fix measured 2026-09-22: 15 min of use, including an emptied (Ctrl+Alt+K) and a 3-line PowerShell tab with hover and focus switches, gave 0 viewport writes from any source; the negative-overshoot glitch did not occur on build 4200. Kept, because the poll stays for the height check anyway and the dip check costs almost nothing. Still open: one unlogged vim run where the view returned to 0 |
| 2 | Viewport handling switch is a code constant, not a setting | **Fixed** 2026-09-21: now `scroll_manipulation_enabled` in `ai_terminal.sublime-settings`, live-verified in the running Sublime |
| 3 | History cap 300 lines (Terminus: 10,000) | **Justified**, checked 2026-09-21: settings-file comment calls it a measured minimap-fill constant ("rigorously tested, deliberate"), not a jumpiness knob. Owner, 2026-09-22: the tests were done by hand, filling the minimap at different font sizes; the notes and logs were since purged. Recorded as the owner's account (UNVERIFIED: no surviving test or log) |
| 4 | Detachable broker process | **Justified** (commit `b3b3be1`), and exercised live at least seven times, including a clean owner restart on 2026-09-21 |
| 5 | Key table, Win32 input mode, native key encoder, mouse reporting | **Justified** (Qwen needs mode 9001; native encoder follows live terminal modes; mouse from the 470-session audit). The routing switches (`mouse_handling`, `page_keys_to_pty`, ...) were not checked one by one |
| 6 | Whole-buffer replace on every frame (Terminus: dirty lines only) | **Partly fixed** 2026-09-22: `line_diff_render_enabled` (default on) replaces only changed lines, and adds or removes lines at the end. New related deviation: `steady_screen_height_enabled` keeps the tab from shrinking with an app footer (Terminus trims). With `compensate_trim_while_following` off, the status-update jiggle measured 0 px; owner confirmation pending. Removing the now-redundant viewport fixers is the next step |
| 7 | Static 450 KB colour scheme rewritten while running (Terminus: generated theme) | `#000001` background trick justified. Registered scopes are never evicted, so the file only grows. **Accepted** by the owner 2026-09-22: there is no way to drop colours |
| 8 | Phantom toolbar and in-tab Settings panel | Toolbar **justified** (`583adbd`, sublimehq/sublime_text#1922). Settings-panel-unreachable-on-alt-screen defect: live-tested with Vibe 2026-09-22 -- `57624a0` fixed the render and clamp loops, but `_preclamp_vp` still snapped the view back on hover/focus change. **Fixed** 2026-09-22 (dip-only), owner confirmed live |
| 9 | Logging and recording (five modules) | Contract written and useful (casts). Rule now: owner's installation only, off by default, no folders or files for users. Broker log made opt-in 2026-09-21. New finding 2026-09-21: `_durable_scheme_backup` writes an uncapped ~400KB+ file to `~/data/logs` unconditionally, with no setting or env-var gate at all -- worse than the already-known loggers. `~/data/logs` still hardcoded in 3 places. **Open**, needs the owner's decision on where the default should live and whether the scheme backup should be opt-in |
| 10 | Usage and quota scanning (read other programs' logins, rewrote Claude Code's credentials file) | **Removed** 2026-09-21, including all usage display |
| 11 | Agent catalog, availability checks (history scan and the agent catalog, both the detection table and the sqlite "Agent Help" lookup, **removed** 2026-09-21: the history scan did not work and belongs in the AISearch repo; the owner did not want a catalog in the repo) | **Removed.** The menus now come only from the profiles in `ai_terminal.sublime-settings`; Gemini was moved there |
| 12 | `ctypes` documentation | **Done for every runtime file.** `terminal/ghostty_vt.py`, `ai_terminal.py`'s ConPTY surface and `tools/agent_broker.py` (2026-09-21), and `tools/recover_console.py`, the Open in Windows Terminal relay (2026-09-22: every constant, struct and kernel32 group commented, four `c_int` restypes named `BOOL`; checked with `py_compile` and a live `--list` that found the running Claude session through the process-check bindings; the pipe/console path, comments only, was not exercised). Undocumented ctypes remains only in the dev scripts the owner keeps (`agent_broker_client.py`, `job_breakaway_test.py`, `check_in_job.py`) |
| 13 | Launch Agent picker | **Removed** 2026-09-21. Agents launch from Ai Terminal > Agents and Shells submenus (sorted A-Z, uninstalled hidden) and the sidebar equivalents. History picker and the Open Here folder picker remain |
| 14 | Package Settings menu entry | Fixed 2026-09-21 (`16d0917`): the parent node was created only by Package Control, which left a blank menu without it |

Where the register says "no recorded justification", that is a finding, not a verdict: it means neither git, the `ai/` documents nor the
transcripts searched give a reason, so the decision is the owner's.
