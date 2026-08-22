# Cursor/caret system: full change history

Compiled 2026-08-21, on request, to support a revert-vs-keep decision on the
whole cursor/caret system. Sourced from `ai_terminal_notes.md`,
`TODO-archive.md`, `TODO.md`, live code (`caret.py`, `render.py`,
`ai_terminal.py`, `screen.py`), and one external session (a Codex log,
2026-08-16) that had independently investigated the same code. Every dated
claim below traces to one of those sources — nothing here is reconstructed
from memory.

**Reference point for the whole document:** Terminus (`randy3k/Terminus`,
verified directly against its real source tonight) does none of this. Its
entire cursor-positioning logic is ~10 lines (`render.py`, `focus_cursor`):
take the PTY's real `(cursor.y, cursor.x)`, add the scrollback offset, adjust
once for wide characters, place the ST caret there. No prompt-row detection,
no footer heuristic, no pinning, no per-app branching. Every item below is
GhostShell code that Terminus's model doesn't need.

---

## 1. Host caret hidden, synthesized cursor via reverse-video (origin unclear, predates 2026-08-10 batons)

**What:** `ai_terminal.sublime-settings` (lines 657-664): ST's native caret is
unconditionally OFF for every terminal view. `render.py`'s
`paint_host_cursor()` synthesizes a visible cursor by either color-reversing
the character under the PTY cursor, or (on a blank cell) drawing a shaped
glyph (block/bar/underline/hollow per DECSCUSR).

**Why:** Stated intent (settings comment): "same model as Terminus." Claude/
OpenCode already paint their own cursor via SGR reverse-video in the cell
stream; a second, real ST caret on top would look like an off-by-one double
cursor.

**Status:** Structurally necessary given the design choice to hide the host
caret — but that choice is itself the fork point from Terminus, which does
turn the real ST caret on and just moves it. Keeping the host caret hidden
means `paint_host_cursor` and everything feeding it (`adjust_display_caret`)
must be correct, or the cursor vanishes with no fallback. This is the
architectural root that items 2-4 below all sit on top of.

**Revert consideration:** The alternative — turn the real ST caret back on,
feed it directly from the PTY's raw `(y, x)` with no remapping, Terminus-
style — would eliminate items 2 and 4 below entirely (nothing left to pin or
route) and is the option tonight's investigation points to as closest to
"why Terminus never has this bug." Not attempted; would need its own
evaluation of the double-cursor risk the original design was avoiding.

---

## 2. `adjust_display_caret` / `find_prompt_row` remapping system (`caret.py`) — created to hide Claude's footer-repaint flicker

**What:** Computes a single "prompt row" (`py`, the last grid row starting
with a `>`/`❯` marker) and maps the PTY's real cursor position onto it:
trusts the hardware cursor only on that one row; anything more than one row
below is treated as "parked on the status footer" and the display caret is
pinned back up to the remembered prompt-row column instead of following it.

**Why (root cause, confirmed via `ai_terminal_notes.md` §2, dated
2026-08-18, referencing already-existing `adjust_display_caret`):** Claude
Code periodically CUPs its hardware cursor down to the footer to repaint
token/cost stats, then back. Un-remapped, this made the visible caret flicker
to the bottom of the screen and back on every repaint — cosmetically bad but
functionally harmless (Terminus would show exactly this flicker and nothing
else).

**Bug this caused (found tonight, 2026-08-21):** The "more than one row below
`py` = footer" rule has no way to distinguish "genuinely on the footer" from
"still legitimately inside your own multi-line prompt, which continues below
`py`." Every multi-line prompt reliably triggers the wrong branch: the
display caret gets pinned to row `py` while your real edit position is two,
three, N rows below. This is the bug you've been fighting all night —
"losing the cursor," inconsistent block-cursor visibility (a wrong/out-of-
range position silently paints nothing, see item 5).

**Status tonight:** Patched (not yet restart-verified beyond a live
`importlib.reload()` in the running process) — replaced the single-row `py`
with a scanned multi-row field (`input_field_last_row`: every contiguous
non-blank row below `py` counts as still-input; the field ends at the first
blank row, which is the real separator before the footer). Trusts the
hardware cursor anywhere inside that field; only pins when genuinely past it.

**Revert consideration:** This entire module exists to solve a cosmetic
flicker. Reverting it to "just trust the hardware cursor, no remapping"
(i.e. deleting the pinning behavior, keeping only the coordinate math other
things depend on) would bring Claude's footer-flicker back but eliminate
this whole class of bug by construction — there'd be nothing left to get
wrong about which row is "real." Tonight's patch is a middle path (extend the
model to be correct for multi-line input) rather than that more radical
option; both are legitimate, and the choice is a judgment call about how much
the footer flicker actually bothers you versus how much you trust a patched
heuristic over no heuristic at all.

---

## 3. `_route_click_to_cursor_fallback` — mouse click → synthesized arrow keys (`ai_terminal.py`, landed 2026-08-11, commit `c3d0860`)

**What:** For the 7 agents confirmed to never enable DEC mouse tracking
(Claude Code, Gemini, Antigravity, Codex, Kimi, Kiro, Junie — determined by
replaying 470 recorded asciicast sessions and regex-matching DECSET mouse
codes), a click has no protocol-level way to move the app's real cursor.
This function fakes it: when the hardware cursor is confirmed on the live
prompt row, it computes the delta between the clicked column and the current
column and sends that many synthesized Left/Right keypresses — exactly what
typing would do.

**Why:** Explicit follow-up complaint after the caret-visibility fix: "click-
to-reposition on the live command line doesn't move the TUI's own cursor,
only ST's local selection moves." Confirmed live 2026-08-11 as working, for
the single-row case.

**Bug this shares (found tonight):** Gated by the exact same single-row
assumption as item 2 (`if py is None or screen.y != py or (row - 1) != py:
return False`). On a multi-line prompt, this silently no-ops — clicking
anywhere in your typed text below the first line does nothing, by design,
because the function can't trust `screen.x` as meaningful off the one tracked
row.

**Status:** Not touched tonight. Would need the same multi-row field
extension as item 2 to work on multi-line prompts. This is the direct answer
to "mouse doesn't move the cursor" — it's not unimplemented, it's the same
bug reaching a third location.

**Revert consideration:** Unlike item 2, there's no Terminus-style
alternative to fall back to here — Terminus doesn't support click-to-
position for non-mouse-tracking apps at all (same protocol limitation this
function exists to work around). Reverting this one means losing click-to-
position entirely for 7 of your 13 agent profiles, not reverting to a simpler
working baseline. If kept, it needs the same multi-row fix as item 2 to
actually work for you.

---

## 4. `term._user_owns_caret` / `on_selection_modified` — click-to-read vs. auto-tracking arbitration (`ai_terminal.py`, iterated 2026-08-10 → 2026-08-11)

**What:** Governs whether the render loop is allowed to keep snapping the ST
caret back to the PTY-tracked position, or must leave it alone because you
manually clicked/selected elsewhere (e.g. to read/copy scrollback).

**Why — direct user request, quoted verbatim in `TODO-archive.md`:** *"if I
plant the cursor in the response, I want to see it... like I am editing a
document"*; *"anything a CLI does to dictate the position of my cursor should
be ignored"*; *"retain the user's control as a default."* Original
implementation (`AiTerminalRenderCommand.run` snapping the caret to the PTY
cursor on every frame) directly violated this — a CLI's own output silently
yanked your cursor back constantly.

**Bugs this went through (all in `TODO-archive.md`, 2026-08-10):**
- First fix added a `caret_detached` gate that, combined with a stale
  position-equality check surviving scrollback trims, ended up **swallowing
  all keyboard input** the first time it mis-fired — "ai_terminal's PTY input
  was completely dead." Root-caused live via `eval_python`, fixed by removing
  `caret_detached` from the gate entirely (a terminal must forward typed
  input regardless of caret position — not optional).
- Second fix: `on_selection_modified` unconditionally latched
  `_user_owns_caret = True` whenever no drawn input box was found — which is
  the *normal* case for plain shells (cmd.exe/PowerShell/bash draw no box),
  so the very first click in a plain shell froze the caret permanently.
  Fixed with a `_live_cursor_row()` row-comparison fallback for the no-box
  case.

**Status:** Current form (line ~3722 `on_selection_modified`) is the result
of both fixes. Clicking *inside* the detected command-line box hands control
back to the PTY-tracked position and **discards the click's landing spot
entirely** (`term._user_owns_caret = False`, then forces a re-render to the
tracked position) — this is the second, distinct mechanism behind "mouse
doesn't move my cursor" alongside item 3.

**Revert consideration:** The underlying user requirement (don't let a CLI
silently steal my cursor while I'm reading) is legitimate and independent of
the multi-line bug — reverting this whole mechanism would bring back
"cursor gets yanked away while reading scrollback," a real regression, not
just a flicker. Not a good revert candidate on its own; if anything the
click-inside-the-box behavior needs the multi-row fix too (right now it
overrides an in-progress multi-line-prompt click with the same broken
single-row tracked position).

---

## 5. `paint_host_cursor` bounds guard silently drops the cursor (`render.py`, unchanged tonight)

**What:** `if cy < 0 or cx < 0 or cy >= len(rows): return rows, False` — when
handed an out-of-range position, paints nothing rather than erroring or
falling back.

**Why:** Defensive guard against a transient bad position (reasonable in
isolation).

**Bug this causes in combination with item 2:** Whenever `adjust_display_
caret` (pre-tonight's-patch) computed a wrong row during multi-line editing,
this guard is *why* the cursor vanished instead of appearing in a visibly
wrong place — a silent failure mode that made the underlying bug harder to
notice/diagnose, not the root cause itself.

**Status:** Not touched tonight; downstream of item 2, should self-resolve
once item 2's fix is confirmed working, since the values it receives should
stay in-range.

**Revert consideration:** N/A — this is a defensive guard, not a feature to
revert. Only relevant as context for why the symptom was invisibility rather
than mispositioning.

---

## 6. `trim_display_rows` — drop a cursor parked far below content (`render.py`, dated 2026-08-18)

**What:** `_do_render` used to keep blank rows all the way down to wherever
the PTY cursor was, even if that was many rows past real content (because
Claude's footer-CUP, item 2's original trigger, parks the cursor down there).
`trim_display_rows(rows, cy)` now keeps only the last non-blank row, plus one
blank cursor row *if* it's the immediate next line (a genuinely empty
prompt) — a cursor two-or-more rows below content is dropped from the kept
rows entirely.

**Why:** Symptom was extra blank lines / a caret stuck on the last buffer
line, retriggered by every keystroke, growing the effective content
unboundedly downward.

**Status:** Working per its own live-verify note (2026-08-18): idle Claude
keeps the caret on the prompt, not the last line; one trailing blank
dropped; stable over a 2-second poll.

**Revert consideration:** This is a real bug fix for unbounded blank-row
growth, largely independent of items 2-4's cursor-position bug (it trims
*content*, not caret position). Keep — reverting would bring back growing
blank space, not related to your multi-line-editing complaint.

---

## 7. Copy-mode toggle (`ctrl+alt+c`) — sidesteps "is this row the live prompt" ambiguity (2026-08-10, from cmux research)

**What:** Explicit mode switch (ported from `cmux`) rather than a heuristic:
while off, plain arrows go to the PTY (readline navigation); toggled on,
plain arrows/PageUp/PageDown/Home/End (with Shift, for selection) go to
native ST movement instead, for reading/selecting scrollback.

**Why:** Plain Up/Down/Left/Right in *response* text (not the live command
line) were being forwarded to the PTY unconditionally, triggering shell
history recall instead of moving the ST caret. Root-cause research (real
ghostty source, cmux source, 4 closed anthropics/claude-code GitHub issues)
concluded there is no reliable signal from Claude Code's TUI to distinguish
"on the command line" from "in scrollback" — it never emits OSC 133
semantic-prompt markers and actively discards externally-injected ones. No
heuristic was viable; an explicit toggle was the only option found.

**Status:** Documented working, after fixing a first-contact regression
where it was accidentally gated together with `caret_detached` (see item 4)
and swallowed all input.

**Revert consideration:** Independent of the multi-line-prompt bug — this
solves a different problem (command-line vs. scrollback ambiguity) that
Terminus also doesn't solve any better (Terminus has no equivalent at all;
its Shift+Arrow just always goes to the PTY, no selection-extend). Not
implicated in tonight's findings either way.

---

## What's actually implicated in your reported symptoms tonight

| Symptom | Cause | Fixed tonight? |
|---|---|---|
| Cursor "lost" / jumps wrong in multi-line prompt | Item 2 (`adjust_display_caret` single-row assumption) | Yes, in `caret.py`, live-reload-verified only |
| Block cursor inconsistently visible | Item 5, downstream of item 2 | Should resolve once item 2 is confirmed, not independently touched |
| Mouse click doesn't move cursor in command line | Items 3 and 4, same single-row assumption plus the click-discards-position behavior | No — needs the same multi-row extension applied to `ai_terminal.py`'s click router, not done |

Items 1, 6, 7 are architecturally separate design decisions/fixes, not
directly implicated in tonight's three symptoms, included here only because
"the entire history" was asked for.
