# ai_terminal TODO

Open/unresolved items only. Full dev-session history (root causes, fixes,
verification detail) lives in [TODO-archive.md](TODO-archive.md).

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
