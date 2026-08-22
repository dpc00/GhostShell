# ai_terminal TODO

Open/unresolved items only. Full dev-session history (root causes, fixes,
verification detail) lives in [TODO-archive.md](TODO-archive.md).

## Session baton (2026-08-21) — multi-line cursor fix landed, bisection gates added, character-splatter bug found (UNRESOLVED), restart required

Long session (423K context at handoff). Three separate threads of work;
read all three before touching anything here. Full prior-art history for
the cursor/caret system (every change, why, revert-consideration per item)
is written up separately in [CURSOR_SYSTEM_HISTORY.md](CURSOR_SYSTEM_HISTORY.md)
— read that first, it's the map of everything this baton builds on.

### 1. Multi-line prompt cursor bug — FIXED, live-reload-verified only

**Root cause.** `caret.py`'s `adjust_display_caret` assumed the prompt is
exactly one row (`find_prompt_row`'s `py`); anything more than one row below
`py` was treated as "parked on the status footer" and pinned back up. A
multi-line prompt's continuation lines are naturally >1 row below `py`, so
editing line 2+ of your own typed text got misclassified as footer-park and
the visible caret got yanked to the wrong row. Same assumption also broke
the click-to-cursor router (`ai_terminal.py`, `_route_click_to_cursor_fallback`,
gated off — item 3 below) and contributed to the block cursor intermittently
not appearing at all (`render.py`'s `paint_host_cursor` silently no-ops on an
out-of-range position instead of erroring).

**Fix.** `caret.py`: added `_row_has_content`/`input_field_last_row` (scans
downward from `py` while rows are non-blank — a wrapped/multi-line prompt's
continuation lines count as still-input; the field ends at the first blank
row, the real separator before the footer). `adjust_display_caret` now
trusts the hardware cursor anywhere inside that field; only pins when
genuinely past it. `screen.py` gained a companion `input_caret_row` (next to
the existing `input_caret_x`) so the footer-pin remembers which row to
restore, not just which column.

**Verified:** live `importlib.reload()` of both modules in the running
process confirmed the new code loads and `input_field_last_row` exists.
**Not verified:** an actual restart + live multi-line-prompt edit test. Do
that first.

### 2. Terminus-deviation bisection gates — landed, current live state below

Per explicit user direction: every GhostShell addition on top of Terminus's
baseline cursor model (verified this session against Terminus's real source,
`terminus/render.py` `focus_cursor` — ~10 lines, raw PTY position, no
remapping/pinning/synthesis/override) should be independently gatable so a
regression can be bisected by disabling everything and re-enabling one flag
at a time.

Five settings added to `ai_terminal.sublime-settings` (top of file, fully
commented inline — read the comments there, not just this summary), wired
into `ai_terminal.py` behind `_setting_bool(<key>, False)`, read fresh every
render, no reload needed:

- `caret_footer_pinning_enabled` — gates item 1's `adjust_display_caret`.
- `host_cursor_paint_enabled` — gates `render.py`'s `paint_host_cursor`.
- `click_to_cursor_fallback_enabled` — gates `_route_click_to_cursor_fallback`.
- `user_owns_caret_enabled` — gates `term._user_owns_caret` read in
  `AiTerminalRenderCommand._run`.
- `fast_caret_patch_enabled` — gates the diff-patch shortcut in
  `AiTerminalRenderCommand._run` (partial `view.replace` instead of full
  buffer replace).

**Current live values (as left this session):**
`caret_footer_pinning_enabled: true`, everything else `false`. This is
*not* the full Terminus baseline — it's the safest configuration found
after two real incidents (below), not a deliberate bisection endpoint. Full
bisection (all five false, retest, re-enable one at a time) has not
actually been completed — it kept getting interrupted by real regressions.

**Two incidents from testing this, both resolved by re-enabling a gate:**
1. Disabling `caret_footer_pinning_enabled` fed `trim_display_rows`
   (**not itself gated** — see "Still open" below) a raw `cy` that jumps to
   Claude's footer; `trim_display_rows` treats a cursor parked away from
   content as "drop everything below the last real row," so real scrollback
   content (not just blank padding) got dropped — reported live as "300
   lines missing, tab down to ~22 lines." Fixed by restoring
   `caret_footer_pinning_enabled: true`. **`trim_display_rows` has a real,
   undocumented-until-now dependency on this gate staying on.**
2. Disabling `fast_caret_patch_enabled` caused visible "last line
   add/subtract" jiggle (every footer token/cost update forcing a full-buffer
   replace instead of a cheap patch) — cosmetic, not data-loss, left off
   intentionally in the final state.

### 3. Character-splatter bug — NOT RESOLVED, NOT explained, independent of items 1-2

**Symptom:** pasting/reading back rendered terminal text shows individual
characters wrong — spaces replaced by a stray letter/digit, or by long runs
of `─` (U+2500) — confirmed via `get_view_content` direct buffer inspection,
not just user-reported paste artifacts. Self-heals on the next full redraw
(transient torn-frame, not persistent corruption).

**Ruled out this session:**
- Not solely caused by `caret_footer_pinning_enabled` being off (reproduced
  with it back on).
- Not solely caused by `fast_caret_patch_enabled` (reproduced with it both
  on and off).
- Not a missing-lock bug at the Python level in the paths actually audited:
  every `term.screen.grid`/`render_cells()` access site found in
  `ai_terminal.py` (`_do_render`, `snapshot`, `_command_line_row_range`, the
  debug dump command) is correctly wrapped in `with term._lock:`/
  `with self._lock:`, matching the PTY reader thread's own locking in
  `_on_data`. This was a real audit (grepped every `term.screen.`/
  `self.screen.` access site), not a guess.

**User's working theory, not yet confirmed or refuted:** this exact
splatter hasn't occurred in weeks; it resurfaced specifically after this
session's gates were flipped off for the first time. If true, one of the still-off
settings (`host_cursor_paint_enabled` is the only one of the three that
touches painted text content at all — `click_to_cursor_fallback_enabled` and
`user_owns_caret_enabled` only affect position/selection, not painted
characters) may have been incidentally masking or timing-avoiding a
pre-existing bug. **Not tested** — see "Still open" below for why.

**Suspected deeper cause, not confirmed:** if it's concurrency-related at
all, it's more likely inside the ctypes boundary to `libghostty-vt` (the
native Zig library) than in Python-level lock discipline, given the audit
above. `ghostty_vt.py`/`ghostty_engine.py`'s actual ctypes calls were not
audited this session.

**A stress-test attempt this session went wrong and must not be repeated
the same way:** ran a two-thread (writer feeds known text / reader calls
`render_cells()` in a tight loop) stress test via `eval_python` — i.e.
*inside the live Sublime plugin_host process*, the same process as the
user's actual live terminal rendering. It did not complete cleanly (appears
hung or pathologically slow; `screen.render_cells()`/`GhosttyParser.feed()`
may have a real hang under sustained concurrent load, itself a lead worth
keeping) and leaked threads (39 active threads observed, up from a normal
baseline) into the live process. The user then observed the character-
splatter symptom live, immediately, plausibly *caused by* GIL/CPU
contention from those leaked threads racing the real render thread — i.e.
the test may have reproduced the bug's trigger condition by accident, for
the wrong reason (self-inflicted resource contention), not by isolating the
real cause. **A restart was already planned and is now also the only clean
way to reap those leaked threads** (no clean kill API for raw Python
threads).

### Still open, in priority order

1. **Restart Sublime.** Required to: pick up item 1's fix beyond the live
   `importlib.reload()` already done; reap the leaked stress-test threads
   from item 3; get a clean baseline before any further live testing.
2. **Re-run the multi-line-prompt cursor test post-restart**, current gate
   state (`caret_footer_pinning_enabled: true`, rest `false`). Confirm item 1
   actually fixed the original complaint before doing anything else.
3. **If pursuing the splatter bug further:** any stress/concurrency test
   MUST run as a fully separate, isolated `python` subprocess (spawnable and
   killable independently), never injected via `eval_python` into the same
   process as the user's live Sublime/terminal session again. A
   `tests/test_*.py`-style file using `Screen`/`GhosttyParser` directly
   (both are pure-Python, no Sublime imports per their own docstrings) run
   via a real `python -m pytest` subprocess is the correct shape for this,
   not an in-process thread stress test.
4. **`trim_display_rows` is not gated** despite touching cursor-adjacent
   behavior (drops rows based on `cy`) — deliberately excluded from the
   five bisection gates as "unrelated to cursor position," which incident 1
   above disproved (it has a real dependency on `caret_footer_pinning_enabled`).
   Consider whether it needs its own gate, now that the dependency is known
   and documented (see the settings file comment on
   `caret_footer_pinning_enabled`).
5. **Complete the actual bisection** the gates were built for (all five
   false, confirm clean, re-enable one at a time) — never actually finished;
   every attempt got interrupted by a real regression (items 2's two
   incidents, then the splatter bug, then the stress-test mistake). The user
   explicitly does not want to "wait for an accident" — the isolated-
   subprocess stress-test approach in (3) is the way to make this
   deterministic instead of opportunistic.
6. **Audit `ghostty_vt.py`/`ghostty_engine.py`'s ctypes calls** for
   thread-safety, if (3)'s isolated stress test reproduces the splatter and
   points at the native boundary rather than Python-level logic.

## Status-line resize/rewrap loop during active TUI output — CODE LANDED, PARTIAL LIVE-VERIFY (2026-08-18)

Reported live by the user while a Claude Code session was actively
streaming output in an `ai_terminal` tab (`Claude` profile). Two related
symptoms, same tab, same `.cast`
(`ai_terminal_asciinema_casts_for_troubleshooting_rendering/
ai_2026-08-17_132410.cast`). Investigated 2026-08-17 against GhostShell
source + that recording + a throwaway replay through the real
`GhosttyParser` (DLL). Fix landed in this session; `PluginLoader.py`
not touched. Needs a plugin reload + live Claude tab to confirm.

### Symptom 1 — 1-column resize/rewrap loop

**What the user saw.** With line numbers on, the tab rewraps in a loop
while Claude is thinking/streaming; it settles the instant output
stops. Turning line numbers off stopped the loop (one rewrap, then
stable). Original theory: 99→100 widens the ST line-number gutter by 1
cell, `_measure` reports one fewer col, we `resize`, rewrap crosses the
digit boundary the other way.

**What the cast actually recorded.** Header `120x42`, then a handful of
real user-size changes (`119`, `53`, `55`, `32`), then **199 resizes
oscillating `32x42` ↔ `33x42`**. 194 of those flips are tight: mean
gap **1.253s** (min 1.023s). Last event after a 198s idle gap is a
single `32x42` → `35x42` — exactly +3 cols, i.e. releasing a 3-digit
line-number gutter, matching the user's "I turned line numbers off"
step.

That 1.25s period is `_LayoutWatcher` itself: poll every 250ms, require
the same candidate on **two consecutive polls**, then `resize`. Two
stable states × ~500ms each ≈ 1s/cycle. The 2-poll gate was written to
kill *transient* 114/115 scrollbar flicker *during* one resize
(`ai_terminal.py` `_LayoutWatcher._run`); it does **not** kill a
two-state attractor where each applied size produces the other size as
the next *stable* measurement.

**Root cause (GhostShell, not Claude).** `_measure` uses
`view.viewport_extent()`, which already excludes the gutter / fold
buttons / a possible H-scrollbar. `_LayoutWatcher` comments claim "we
never auto-resize in response to PTY output" — true only as a direct
call. Indirectly: streaming output → `_do_render` `view.replace` → ST
relayout (gutter digit width and/or H-scrollbar) → next poll sees
`cols±1` → `term.resize` → parser reflow + ConPTY `SIGWINCH` → Claude
repaints at the new width → the other measurement wins → loop. When
output stops there is no `replace`, ST layout stays put, the watcher
sees a stable size, loop ends.

The 99→100 gutter-digit story is structurally real (`viewport_extent`
shrinks when ST's line-number column grows) and would loop at 9→10 /
99→100 / 999→1000 the same way. **This particular incident is probably
not that crossing**: oscillation started 4.6h into the session, right
after the pane was narrowed to 32 cols, when the view was already on a
3-digit gutter. At that width, line numbers on put the measure on a
1-col knife-edge (H-scrollbar / box-drawing slightly wider than
`em_width` / the existing `4.0*cw` subtract being just barely not
enough). Line numbers off added ~3 cols of slack and left the
knife-edge.

**Fix applied.** `accepted_cols` (`ai/terminal/layout.py`) never grows
by exactly one column; a shrink-by-one is applied (locks to the size
that fits). `_LayoutWatcher._run` uses it after the existing 2-poll
gate. Tests: `tests/test_layout.py` (watched `32→33` fail at `33 != 32`
before the hysteresis branch). 4-digit gutter reserve in `_measure`
was the optional complement — not done; hysteresis alone kills both
the gutter-digit and H-scrollbar attractors. A real sash-drag of ≥2
cols still goes through.

### Symptom 2 — status-line extra/missing newline, caret on last line

**What the user saw.** Persists with line numbers off. Claude's footer
(`⏵⏵ accept edits on (shift+tab to change) · ◐ medium · /effort`)
jiggles: an extra newline appears or the caret sits on the last line.
Retriggers on nearly every keystroke in Claude's own prompt.

**What the cast actually recorded.** 15,624 complete `CSI ?2026h`…
`CSI ?2026l` frames; 713 mention the accept-edits / shift+tab footer.
Last CUP row in those frames is usually **42** (the last row of the
pinned 42-row PTY) or 34/35. Newlines *after* that last CUP are **not**
a constant pad: among status frames the count is 0 (357), 1 (116),
2 (51), 3 (80), 4 (24), and a long tail up to 77. During the 32/33
oscillation window last-CUP is 42 on 78 frames (often with 3–4
following `\n`) vs 34/35 on 107 frames (usually 0 following `\n`).
**97 overflow-N flips** on consecutive status frames that sit within
400ms of an `"i"` (keystroke) event.

Typical live pair (tail of the file): one frame CUPs the footer into
rows 39–42 then emits 2 `\r\n`; the next CUPs `Ctx`/`Model` onto 41–42
and lets `Session` / `Reset` / accept-edits / effort overflow as 4
`\r\n`. Width 32 vs 33 changes how those lines wrap/`…`-truncate, which
is why (1) feeds (2).

**Root cause (interaction, both sides).** Claude is drawing a
fullscreen-style footer with absolute CUP on the **primary** screen
(`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` + `force_main_screen` strips
1049). A `\n` issued after `CUP` to row 42 is a real primary-screen
scroll. Replayed through `GhosttyParser` on a 32×42 `Screen`:

- `CUP 42` + N × `\r\n\x1b[K` → history grows by exactly N, hardware
  cursor stays at `y=41`, `_do_render`-style trim grows by N.
- Alternating the last two *verbatim* status frames from the cast:
  `hist` 0→8→16→24→32 and trimmed render height 42→48→50→56→58→64→72
  over 6 alternations. Not a closed jiggle — overflow **appends**.

`update_replace_scroll` (the 2026+CUP-home latch that stops Codex-style
dumps from duplicating history) does **not** fire on these frames: they
CUP to row 30/42, not home. Separately, 130 frames in this cast do
`CSI ?2026h` `CSI ?2026l` and only *then* `CSI H` (home **after** the
sync pair). `update_replace_scroll` stays false; those full-transcript
dumps append too. 103 frames are the well-formed 2026+home shape.

`_do_render` then makes the scroll visible as "extra newline / caret on
last line": `last_real` is initialized to `cy` (cursor row), so blank
rows down to a cursor parked on row 42 are kept. `adjust_display_caret`
would remap onto `❯` **if** that glyph is still on the live grid;
`find_prompt_row` only walks `screen.grid`, not history. After a
last-row overflow the `❯` has often scrolled off the grid, remap
fails, and the ST caret stays on the last line.

Synchronized-update handling in `_on_data` / `_do_render` is *not* the
bug — it correctly defers paint until `?2026l`. The damage is the
bytes inside the batch (CUP-to-last-row + variable `\n`) plus
main-screen scrollback + trim-to-cursor.

**Fix applied (visual jiggle only).** `trim_display_rows`
(`ai/terminal/render.py`) keeps the last non-blank row, plus a blank
cursor row only when it is the next line (empty shell prompt). A
cursor two or more rows below content is dropped. `_do_render` calls
it in place of `last_real = cy`. Tests: `tests/test_trim_display.py`
(watched blank-below-content fail at `4 != 2` before the new rule).

**Not applied (on purpose).** History append from last-row overflow
and empty-2026-then-home dumps. That is `_sync_scrollback` /
`update_replace_scroll` and needs its own failing parser test against
the verbatim tail frames before anyone touches it.

### Still open

Live-verify after the 2026-08-18 Sublime restart (plugin loaded
`GhostShell.ai.terminal.layout`; `accepted_cols(32, 33) → 32` in the
running process; 207 unit tests OK):

1. **Resize loop — only partial.** Line numbers are on. Claude is
   `123x42`, Grok Build `124x42`. Console since restart has a single
   `resized PTY to 123x42` (legitimate first measure), no 32↔33
   oscillation. The 1-col attractor cannot fire at this width. Still
   need a narrow (~32 col) Claude pane with streaming output to close
   this out. Do not disable line numbers to "test" it.
2. **Caret-on-last-line / extra blank growth — holds at idle.** Live
   Claude: hardware cursor on the `❯` prompt row, ST caret remapped
   there (not the last buffer line), `trim_display_rows` drops the one
   trailing blank. 2s idle poll: `hist` / `last_nb` / `view_rows`
   unchanged.
3. **1-line type-jiggle — generic trailer skip, opted in per profile.**
   `_scroll_to_bottom` subtracts `follow_ignore_trailing_lines` (default
   0) from the follow height. The engine does not inspect line contents
   and does not special-case any TUI. A profile that wants the last N
   rows left out of the snap target sets the number. Claude-family
   profiles are set to 2 in `ai_terminal.sublime-settings`; everyone
   else stays at 0. Tests: `tests/test_layout.py` `FollowLineCountTests`.
   Needs a plugin reload (do not touch PluginLoader.py while other
   agent tabs are live).

4-digit gutter reserve and the replace_scroll heuristic stay unstarted.

## Copy-mode "turns on by itself" report (2026-08-11), Kiro profile — UNRESOLVED

`term.copy_mode` flipped ON without a deliberate `ctrl+alt+c` press during
a Kiro session. Root cause not found — ruled out: deliberate press, AltGr/
accented-character composition, cursor-placement code (never touches
`copy_mode`), a settings-driven startup default (`self.copy_mode = False`
is hardcoded). The triggering Kiro tab was already closed by investigation
time, so no live state/console evidence from the actual incident was
recoverable.

TEMP DEBUG logging (call-stack dump on the ON transition) is still active
in `AiTerminalToggleCopyModeCommand.run` (`ai_terminal.py`), waiting to
catch the actual trigger next time this reproduces. Remove once
root-caused. See archive, "Copy-mode 'turns on by itself' report" section,
for everything ruled out so far.

## cmux feature-port backlog — not started

Two items survived triage against GhostShell's actual code (see archive,
"resolved via cmux-style copy-mode toggle" section, for the other 4 items
that were already implemented, not applicable, or demoted):

1. **Command-block segmentation** (`OSC133CommandParser.swift` in cmux — a
   pure idle→prompt→command→output state machine) — could power a "jump
   to previous/next command" navigation in scrollback.
2. **CR-redraw line folding** (`foldLine` in cmux) — collapses `\r`-progress-
   bar spam to its final state; useful for `render.py` copy/log-export so
   a copied progress bar isn't hundreds of stale redraw lines.

## page_keys_to_pty — minor follow-up verification gaps

From the 2026-08-13 full-profile audit (see archive for the complete
per-profile writeup). Not urgent, just not individually confirmed:

- `Claude --chrome` / `Claude →⇢⇨ Ollama --chrome` — inherited the fix
  status of the base `Claude` profile (unaffected either way; Claude
  itself needed no fix) but not separately re-verified.
- `OpenCode --mini` / `OpenCode →⇢⇨ Ollama --mini` — got
  `page_keys_to_pty: true` by inference (same binary family as the
  audited base `OpenCode` profile), not individually live-tested.
