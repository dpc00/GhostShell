# GhostShell recovery plan

Status: proposed, not started. No files edited, no tests run to produce
this plan — it is synthesized entirely from source-level comparison
against `C:\Users\donal\tools\ghostty` and `C:\Users\donal\tools\cmux`
(see credit/evidence below), plus the existing SText→GhostShell history
in `ai_terminal_notes.md`, `ai/TODO.md`, and `ai/TODO-archive.md`.

## Why this plan exists

GhostShell's stated purpose is an owned, controllable replacement for
Terminus — differences from normal terminal behavior should be
intentional improvements, not accumulated special cases. The project has
drifted: `ai/TODO.md` and `ai/TODO-archive.md` show a pattern of
heuristic patches (viewport compensation, `force_main_screen`, selection
paint suppression, per-profile mouse routing) layered on top of each
other, several of which caused live regressions and were reverted.

Both reference implementations (Ghostty itself, and cmux which embeds
libghostty) converge on the same architecture: **the native VT engine is
the single source of truth for terminal state, and the host UI is a thin
consumer that reads snapshots and forwards complete input events.**
GhostShell embeds `libghostty-vt` too, but instead of trusting it,
copies its state into a second, independently-mutable Python `Screen`
object, then patches that copy with heuristics whenever the copy
diverges from reality. That divergence-and-patch cycle is the source of
most items in the regression ledger.

The fix is not a bug-by-bug patch pile. It's removing the second copy of
terminal state so there's nothing left to diverge.

## Ground rules for every stage below

- Each stage lands independently, with its own tests, and can be
  reverted independently. Do not batch stages into one large change —
  that repeats the pattern that caused past regressions.
- Terminus-compatible behavior is the baseline. A stage that changes
  visible behavior must state what Terminus does, what GhostShell will
  do, and why they differ (if they do).
- Preserve the invariants already proven in this project:
  - Restrictive modes (e.g. `force_main_screen`) may be explicitly
    engaged and heuristics may disengage them, but heuristics must never
    silently engage them.
  - Full-transcript replay is overwrite/rebuild behavior, not append.
  - Terminal-local logging stays in GhostShell; conversation/hook/JSONL
    logging is STLogs's job, not the terminal's.
- Risky live testing happens in the isolated Testing Agent Sublime
  profile, never in a working conversation tab.
- Any exploration of Ghostty/cmux beyond this plan must produce a
  concrete file/function mapping into GhostShell, not a citation.

## Stage 0 — Safety net (no behavior change)

Before touching ownership or protocol handling, make regressions visible
immediately instead of days later.

1. Record the `ghostty-vt.dll` build/version/hash actually in use
   (currently untracked — README doesn't document provenance). Store it
   next to the binary or in a settings key read at startup.
2. Fix the documented test command. README says
   `python -m unittest discover -s tests -v`; the real, fuller command is
   `python -m pytest tests/ -q` (426 collected vs. the 48 pytest-only
   tests the unittest runner misses). Update README and
   `tools/check_import.py`'s stale/partial expected-class list.
3. No CI currently runs the suite. Even a local pre-push hook running
   `pytest tests/ -q` would have caught some of the reverted regressions
   sooner. Out of scope to build full CI here, but note it as a gap this
   plan doesn't fix.

Exit criteria: `pytest tests/ -q` is the documented, accurate command;
DLL provenance is recorded.

## Stage 1 — Close the native-state race (correctness fix, no architecture change)

Evidence: `ai_terminal.py`'s `AiTerminalKeypressCommand.run` calls
`GhosttyParser.encode_key` on Sublime's main thread without holding
`_Terminal._lock`, while the PTY reader thread may be inside
`terminal_vt_write` concurrently. `_do_render` also reads `screen.dirty`
before acquiring the lock and reads `cursor_visible`/`cursor_shape`
after releasing it — three separate unlocked touches of shared state.

Action: make every touch of `_term`, the key encoder, render state, or
mirrored metadata go through `_Terminal._lock`. This is a pure
correctness fix — no visible behavior should change if the race was
benign so far, and if it wasn't, this removes a plausible root cause for
some of the unattributed intermittent bugs in `TODO.md` (e.g. the
unattributed viewport drift entry).

Exit criteria: existing concurrent feed/render tests
(`tests/test_ghostty_engine.py`, `tests/test_splatter_stress.py`) pass;
add a test that exercises keypress-during-feed if one doesn't already
exist.

## Stage 2 — Fix the alt-screen strip regex; keep `force_main_screen`'s intent (REVISED, landed 2026-08-25)

**Revision note:** the original version of this stage, written from the
Ghostty/cmux source comparison alone, proposed deleting
`_strip_alt_screen` entirely on the theory that GhostShell should never
rewrite the incoming VT byte stream, matching Ghostty/cmux. Live
investigation before implementing showed that recommendation was wrong:
`force_main_screen`'s docstring, the settings file, and a full prior
audit (`ai/TODO-archive.md`, 2026-08-13, `page_keys_to_pty`) all
establish that keeping AI-CLI transcripts in real, searchable ST
scrollback across alt-screen episodes (Codex's Ctrl+T pager, Grok's TUI)
is a deliberate, already-tuned product feature, not architectural drift.
Deleting it would have made that content live only in an ephemeral
native alternate screen (zero scrollback, per Ghostty's own model) —
the opposite of what this project is for. The Ghostty/cmux comparison
correctly diagnosed "GhostShell rewrites the byte stream" but had no way
to know that divergence was intentional; source comparison alone can't
distinguish an intentional product decision from an architectural wart.

What *was* a real bug, confirmed live: `_ALT_SCREEN_RE` (originally
`\x1b\[\?(1049|1047|47)[hl]`) only matched an isolated alt-screen
sequence. Any app combining it with another private mode in one CSI
sequence (e.g. `\x1b[?1049;2004h`, alt-screen + bracketed paste
together) slipped through unstripped, silently defeating
`force_main_screen` for that app. Confirmed via `eval_python` against
the live plugin before touching any code:

```
\x1b[?1049h                -> stripped OK
\x1b[?1049;2004h            -> LEAKED THROUGH
\x1b[?2004;1049h            -> LEAKED THROUGH
\x1b[?1;47h                 -> LEAKED THROUGH
```

Action taken: `_strip_alt_screen` now removes only the alt-screen mode
numbers (1049/1047/47) from a private-mode parameter list and keeps the
rest, instead of matching only an all-alt-screen isolated sequence. An
unrelated mode toggled in the same sequence (e.g. bracketed paste) still
reaches the parser; a sequence that becomes empty after removal is
dropped entirely, same as before. `force_main_screen`'s behavior and
default are unchanged.

Verified: unit tests added for the three combined-parameter cases above
(`tests/test_ghostty_engine.py::StripAltScreenTests`); confirmed
end-to-end via `eval_python` against a live `GhosttyParser` that
`screen.alt_screen` stays `False` across a combined `?1049;2004h`/
`?1049;2004l` toggle, matching the pre-existing behavior for the
isolated form. Full suite: same 3 pre-existing, unrelated
`test_launcher_flow.py` failures as Stage 0/1's baseline.

Exit criteria (met): the alt-screen strip is correct for combined
private-mode sequences; `force_main_screen`'s documented behavior is
unchanged for every case that already worked.

## Stage 3 — DO NOT IMPLEMENT AS WRITTEN (verified against project history, 2026-08-25)

**Original proposal (do not do this):** delete `Screen`'s own history
deque and `_sync_scrollback`/`merge_replace_scroll_history`, on the
theory — from the Ghostty/cmux comparison alone — that GhostShell keeps
a second, purely-duplicative copy of terminal history that native
scrollback should replace outright.

**Why this is wrong, with evidence:** `ai/TODO.md` (2026-08-21/22
entries, "content-loss bug" and "splice bug") documents a real, already
shipped and tested investigation into exactly this mechanism:

- A full-transcript-replay TUI (Codex-style: `CSI H` + re-dump the whole
  conversation every turn) genuinely produces **duplicate native
  scrollback rows** when the redraw overflows past one screen height —
  documented in the record as "correct raw terminal behavior, not a
  Python bug." Trusting native history directly, as Stage 3 proposed,
  means showing the user those duplicates.
- A first attempt at simplifying this exact code (a "just don't clear
  history" fix, 2026-08-22) was tried, reverted after it broke
  `HomeReplaceScrollTests` (lines started appearing twice), and is
  recorded as "do not repeat this exact approach."
- The fix that actually shipped, `merge_replace_scroll_history`, aligns
  a new dump against existing history by content-matching and splice
  repair specifically so the *displayed* transcript stays deduplicated
  despite the terminal-correct duplication underneath. It's covered by
  `ReplaceScrollMergeTests` and `HomeReplaceScrollTests`
  (`tests/test_ghostty_engine.py`), independently re-verified at the
  time against an 8-turn live repro (`missing=0 dupes=0 total_lines=233`).
- A separate, still-open native-level character-splice bug was chased
  through a pure-C repro linked directly against the exact
  `ghostty-vt.dll` GhostShell ships (SHA256-matched) and **cleared
  Ghostty/libghostty-vt entirely** — it reproduces only under the live
  plugin's real threading, not in any single-threaded repro. This bug is
  orthogonal to Stage 3 either way: keeping or removing the Python
  history copy does not touch it.

Both explore-4 and explore-5 (the Ghostty/cmux source comparisons this
plan was built from) had no way to know this history — they compared
architectures, not this project's own bug-fix record. Source comparison
correctly spotted a structural difference from Ghostty/cmux, but could
not tell an accidental duplication from a deliberately-earned fix for a
workload (full-transcript-replay AI CLIs) neither reference
implementation needs to handle. This is the second plan stage (after
Stage 2) where that blind spot produced a recommendation that would have
reintroduced an already-fixed bug — treat any remaining stage's
"delete this, trust native" recommendation as unverified until checked
the same way.

`trim_paused`'s narrower claim ("can't restore rows libghostty already
pruned") is true but not actionable: `trim_paused` only defers Python's
own cap enforcement on rows *already copied* into `Screen.history`; it
was never responsible for retrieving rows already evicted from native
before Python read them, so there's nothing to fix there either.

No code changed for this stage.

## Stage 4 — Bind native mouse and synchronized-output state

Evidence: keyboard already correctly derives from live terminal state
via `GhosttyParser.encode_key` (modulo the Stage 1 locking fix); mouse
does not — `mouse.py` reimplements the 1000/1002/1003/1006 protocols
independently, and routing lives in `ai_terminal.py`'s
`_route_mouse_click`/`_route_mouse_wheel`/`_mouse_force_release`, gated
by settings like `mouse_handling`, `wheel_to_pty`, `page_keys_to_pty`.
Similarly, synchronized-output (DEC mode 2026) is tracked by two
independent regex state machines (`_on_data`'s `_sync_update_open` and
`ghostty_engine.py`'s `update_replace_scroll`) instead of querying the
one native mode flag Ghostty already maintains.

Action: bind libghostty's native mouse encoder (`ghostty_surface_mouse_*`
equivalents already exposed via the C API used elsewhere) and drive it
from `_term`, keeping only Sublime event capture and coordinate
acquisition in Python. Replace both regex-based mode-2026 scanners with
a query against native terminal modes.

Exit criteria: `tests/test_mouse.py` passing against the native encoder;
manual check of mouse reporting in an app that uses it (e.g. `htop`,
`less -S` in mouse mode); synchronized-output paint delay still bounded
but no longer regex-driven.

## Stage 5 — One resize pipeline with applied-state accounting

Evidence: `_LayoutWatcher` debounces geometry, then `_Terminal.resize`
resizes `GhosttyParser`/`Screen` *before* resizing the PTY; PTY resize
failures are logged but don't return an applied-size result, so
`_Terminal` can retain a target size ConPTY never actually accepted.
`force_main_screen` also suppresses row changes outright during resize.
cmux's `TerminalSurface+Sizing.swift` pipeline explicitly distinguishes
desired vs. applied pixel size and only updates its own last-known-size
bookkeeping after the native call confirms.

Action: introduce one `ResizeOutcome`-style result threaded through
`_LayoutWatcher` → `_Terminal.resize` → PTY backend → `GhosttyParser`,
so a failed PTY resize is visible and retryable instead of silently
producing a Python/native size mismatch. Remove resize-time row
suppression as a general policy — if a no-reflow mode is ever genuinely
needed, model it as its own explicit mode (as cmux does for tmux
mirroring), not a side effect of `force_main_screen`.

Exit criteria: resize tests cover a simulated PTY-resize failure path;
live check of rapid resize (dragging the pane edge) shows no stuck
geometry.

## Stage 6 — Idempotent parser/native-resource teardown

Evidence: the FFI already exposes `terminal_free` and frees for the
encoder, cells, iterator, and render-state, but `GhosttyParser` has no
explicit close/free path — `_Terminal.kill()` just starts a daemon
thread. cmux's `TerminalSurface+RuntimeLifecycle.swift` models this
properly: idempotent close sequence, liveness-checked before every
native call, generation-tagged to catch stale queued callbacks.

Action: give `GhosttyParser` a lifecycle with a generation token —
stop accepting input, unbind the write callback, stop/join the reader,
quarantine handles, then free encoder/cells/iterator/render-state/
terminal in reverse acquisition order. Daemon-thread exit is not a
substitute for this.

Exit criteria: a new test exercises close-during-active-render and
close-during-pending-input and confirms no use-after-free-shaped
Python exceptions (AttributeError on a nulled handle, etc.) and no
stale callback fires after close.

## Stage 7 — Grapheme-aware text offsets (small, isolated)

Evidence: `render.py`'s `build_text_and_regions` advances the
Sublime-buffer offset by one per terminal cell even when the underlying
character (`ch`) contains multiple Unicode code points (e.g. combining
marks, some emoji sequences). This desyncs the cell↔buffer-offset
mapping for any such input.

Action: maintain a cell-to-Sublime-character offset map derived from the
native grapheme snapshot rather than assuming one code point per cell.

Exit criteria: a regression test with a combining-mark or multi-codepoint
emoji sequence round-trips correctly (cursor lands in the right column
after such input).

## What this plan deliberately does not do

- It does not adopt cmux's Metal-rendering or Swift-specific pieces —
  those aren't applicable to a Sublime host.
- It does not add new product features (no new settings beyond what
  removing dead ones requires).
- It does not attempt a full CI/release pipeline — flagged as a gap in
  Stage 0, not solved here.
- It does not touch cursor overlay rendering (`caret.py`,
  `CURSOR_SYSTEM_HISTORY.md` lineage) beyond what Stage 3's snapshot
  change requires it to consume — that system has its own detailed
  history and should be changed separately, on its own evidence, not as
  a side effect of this plan.

## Suggested order and independence

Stages 0 and 1 are safe prerequisites and should land first, in either
order. Stage 2 should land alone and be live-verified before Stage 3,
since Stage 3 depends on alternate-screen state being trustworthy.
Stages 4, 5, 6, and 7 are independent of each other and of Stage 3's
internals (though 4 and 5 both touch `ai_terminal.py`'s hot paths, so
land them separately to keep blame/revert boundaries clean).

## Evidence sources

- `C:\Users\donal\tools\ghostty` — `src/terminal/Terminal.zig`,
  `stream.zig`, `stream_terminal.zig`, `render.zig`, `PageList.zig`,
  `Screen.zig`, `ScreenSet.zig`, `c/mouse_encode.zig`,
  `src/input/mouse_encode.zig`, `src/renderer/State.zig`,
  `src/termio/Termio.zig`.
- `C:\Users\donal\tools\cmux` — `Sources/GhosttyTerminalView.swift`,
  `Packages/macOS/CmuxTerminal/Sources/CmuxTerminal/Surface/
  TerminalSurface+Sizing.swift`, `TerminalSurface+Input.swift`,
  `TerminalSurface+RuntimeLifecycle.swift`,
  `TerminalSurface+AppliedSize.swift`,
  `Sources/GhosttyApp+ChildExitPolicy.swift`, and its test suite under
  `Packages/macOS/CmuxTerminal/Tests/CmuxTerminalTests/`.
- GhostShell: `ai/ai_terminal.py`, `ai/terminal/ghostty_engine.py`,
  `ai/terminal/screen.py`, `ai/terminal/render.py`,
  `ai/terminal/caret.py`, `ai/terminal/mouse.py`,
  `ai/terminal/keys.py`, `ai/terminal/ghostty_vt.py`.
- Regression history: `ai/TODO.md`, `ai/TODO-archive.md`,
  `ai_terminal_notes.md`, `ai/CURSOR_SYSTEM_HISTORY.md`.
