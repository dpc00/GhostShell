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

**Timer vs. event, resolved for one of the two jobs (VERIFIED, code read, 2026-09-21).** `_clamp_vp_loop`
(`ai_terminal.py:9817-9948`) does two separate jobs on its 500ms tick:
1. Converts trackpad pan into PTY scroll for TUI apps (`_vp_pan_to_tui_scroll`, 9778, "content-grab"
   model) by comparing the viewport position against its own last-seen value every tick. Sublime has no
   command or event that fires on a raw viewport-position change from trackpad/scrollbar drag (only
   `on_selection_modified`/text commands fire on user text edits, not on scrolling); a poll is the only
   way to see this happen at all. **This job cannot be event-driven with Sublime's plugin API.**
   No deviation to resolve here beyond the interval, already loosened 8ms->500ms.
2. The height-change detector (line 9832 comment onward) exists because panel/sash/sidebar/resize can
   change `viewport_extent()` with no user text edit. The code comment argues this "is not just an
   enumerable list of commands" — a sash drag between groups, for example, is a raw mouse operation with
   no `on_post_window_command` hook at all in the public API (checked against Sublime's documented
   `sublime_plugin` event list: no `on_layout`/`on_group_resize` event exists). Panel show/hide (find,
   console, replace) IS a command (`show_panel`/`hide_panel`) and could be caught by
   `on_post_window_command`, but a sash drag could not. **This half of the loop cannot be fully replaced
   by events either, for the same missing-hook reason, though a command-triggered event could cut how
   often it needs to poll for that sub-case.**
Conclusion: the "timer vs Sublime event" question in the doc since 2026-08-28 has an answer — Sublime's
plugin API has no event for either raw-viewport-drift or arbitrary-layout-resize, so a poll is required
for both jobs this loop does, not merely convenient. This closes the "why a timer and not an event"
question; the 500ms interval itself is a separate, already-settled tuning decision (line 48 above).

**Open defect (user report, 2026-09-20).** With Claude Code CLI in a tab, the text jiggles one line
up and back down during command-line typing. Cause not yet identified.

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
`ai_terminal` session (this conversation's own tab) stayed alive (`term.pty.is_alive()` True) throughout. `ai_terminal.py`'s ConPTY
code and `tools/agent_broker.py` are not started.

## 13. Status summary of every deviation from Terminus (2026-09-21)

| # | Deviation | Status |
|---|---|---|
| 1 | Eight code paths write the viewport position, plus a self-rescheduling clamp loop (Terminus: one function, once per render) | Partly justified (the one-line jiggle fix of 2026-09-06/07; the panel-resize case). Timer-vs-event question resolved 2026-09-21: Sublime's plugin API has no event for raw viewport drift or arbitrary layout resize, so polling is required for both of the loop's jobs. The loop runs at 500 ms since 2026-09-19. The other 7 writers and their overlap (e.g. the compensate/settle pair implicated in the open jiggle defect) are still **Open.** |
| 2 | Viewport handling switch is a code constant, not a setting | **Fixed** 2026-09-21: now `scroll_manipulation_enabled` in `ai_terminal.sublime-settings`, live-verified in the running Sublime |
| 3 | History cap 300 lines (Terminus: 10,000) | **Justified**, checked 2026-09-21: settings-file comment calls it a measured minimap-fill constant ("rigorously tested, deliberate"), not a jumpiness knob. Whether an actual test exists behind "rigorously tested" is unverified |
| 4 | Detachable broker process | **Justified** (commit `b3b3be1`), and exercised live at least seven times, including a clean owner restart on 2026-09-21 |
| 5 | Key table, Win32 input mode, native key encoder, mouse reporting | **Justified** (Qwen needs mode 9001; native encoder follows live terminal modes; mouse from the 470-session audit). The routing switches (`mouse_handling`, `page_keys_to_pty`, ...) were not checked one by one |
| 6 | Whole-buffer replace on every frame (Terminus: dirty lines only) | **No recorded justification.** Inherited from the first version (2026-07-03). Feasibility of a fix investigated 2026-09-21: the native engine already tracks per-row dirty state and it is thrown away one call later (`ghostty_engine.py`). A dirty-line rewrite is a medium-sized, four-file change, not started. Needs the owner's decision |
| 7 | Static 450 KB colour scheme rewritten while running (Terminus: generated theme) | `#000001` background trick justified. Checked 2026-09-21: dynamic-registration-over-pre-generation is defensible (GhostShell's real 256-colour x 256-colour x 8-style space is ~1,600x Terminus's 16-colour table); the defect is that registered scopes are never evicted, so the file only ever grows. **Open** (needs an eviction/bound design) |
| 8 | Phantom toolbar and in-tab Settings panel | Toolbar **justified** (`583adbd`, sublimehq/sublime_text#1922). Settings-panel-unreachable-on-alt-screen defect: appears already fixed by `57624a0` (2026-09-19, same day) by code inspection 2026-09-21 -- the hard pin it depended on is no longer called anywhere. **Needs a live re-test against a real alt-screen TUI to close** |
| 9 | Logging and recording (five modules) | Contract written and useful (casts). Rule now: owner's installation only, off by default, no folders or files for users. Broker log made opt-in 2026-09-21. New finding 2026-09-21: `_durable_scheme_backup` writes an uncapped ~400KB+ file to `~/data/logs` unconditionally, with no setting or env-var gate at all -- worse than the already-known loggers. `~/data/logs` still hardcoded in 3 places. **Open**, needs the owner's decision on where the default should live and whether the scheme backup should be opt-in |
| 10 | Usage and quota scanning (read other programs' logins, rewrote Claude Code's credentials file) | **Removed** 2026-09-21, including all usage display |
| 11 | Agent catalog, availability checks (history scan and the agent catalog, both the detection table and the sqlite "Agent Help" lookup, **removed** 2026-09-21: the history scan did not work and belongs in the AISearch repo; the owner did not want a catalog in the repo) | **Removed.** The menus now come only from the profiles in `ai_terminal.sublime-settings`; Gemini was moved there |
| 12 | `ctypes` documentation | **Rule 5 not met overall.** `terminal/ghostty_vt.py` (the largest file) documented 2026-09-21, sourced from the real C headers -- directionally 41%->83% by a reimplemented measure. `ai_terminal.py`'s ConPTY code, `tools/agent_broker.py`, and the three small scripts remain **Open** |
| 13 | Launch Agent picker | **Removed** 2026-09-21. Agents launch from Ai Terminal > Agents and Shells submenus (sorted A-Z, uninstalled hidden) and the sidebar equivalents. History picker and the Open Here folder picker remain |
| 14 | Package Settings menu entry | Fixed 2026-09-21 (`16d0917`): the parent node was created only by Package Control, which left a blank menu without it |

Where the register says "no recorded justification", that is a finding, not a verdict: it means neither git, the `ai/` documents nor the
transcripts searched give a reason, so the decision is the owner's.
