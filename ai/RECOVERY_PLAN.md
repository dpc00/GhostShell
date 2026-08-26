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

## Stage 2 — Stop rewriting the VT byte stream

Evidence: `ghostty_engine.py`'s `_strip_alt_screen` regex-strips
alternate-screen escape sequences before they reach the parser when
`force_main_screen` is enabled. The regex is documented as missing mixed
private parameters. Neither Ghostty nor cmux ever rewrites the incoming
stream — cmux's only comparable behavior (manual-I/O tmux mirror mode)
is an explicit, separately-modeled mode, not stream editing.

Action: delete `_strip_alt_screen`. If "stay on primary screen"
(transcript mode) is still a wanted product feature, implement it as a
presentation-layer choice over native primary/alternate snapshots —
i.e. GhostShell may choose which native screen to *display*, but must
never lie to the parser about what the child process sent.

Risk: this is exactly the kind of change that caused the
`auto_follow`/`_compensate_trim_scroll` regression Grok shipped and
reverted (see `TODO.md` 2026-08-23 entry) — a plausible-looking
simplification that changed live behavior. Land this alone, verify live
in the Testing Agent profile across both a full-screen app (e.g. vim)
and a normal shell session, before proceeding to Stage 3.

Exit criteria: `force_main_screen` no longer touches the byte stream;
manual live check confirms alt-screen apps still render correctly
whether or not the setting is on.

## Stage 3 — Retire the second scrollback/history copy

Evidence: `ghostty_engine.py`'s `_sync_scrollback` re-reads native
history into a second Python deque; `merge_replace_scroll_history`
heuristically repairs synchronized full-frame replay; `screen.py`'s
`_retire_line` independently drops blank rows, applies its own cap,
supports pausing trim, and emits logging callbacks. Both Ghostty and
cmux keep exactly one scrollback owner (Ghostty's `PageList`); cmux
never copies historical cells into the host, it only asks Ghostty for a
compact scrollbar position and issues `scroll_to_row`.

This is flagged by explore-5 as GhostShell's single largest duplication,
and the checkpoint's `trim_paused` note is a real hazard: pausing trim in
Python cannot restore rows libghostty already pruned via
`max_scrollback` — the feature currently promises something it can't
deliver.

Action:
1. Make native history the sole display source; stop maintaining a
   parallel Python deque of historical rows.
2. Move durable session-log persistence (if still wanted) to an
   append-only listener on the PTY byte stream — the same layer
   `_Terminal._on_data()` already exists at — completely decoupled from
   the terminal's own scrollback/rendering path. This keeps the proven
   invariant: full-transcript replay rebuilds, logging appends, and the
   two must not be the same code path.
3. Remove `trim_paused` and any cap/eviction logic that duplicates
   `max_scrollback`; if a smaller/larger scrollback is wanted, it's a
   native setting, not a second cap.

This stage is the biggest and should itself be split into sequential
patches (read-path first behind a flag if needed, then remove the old
deque) rather than one commit.

Exit criteria: `Screen.history` (or equivalent) is a read-only view over
native state; `tests/test_screen.py` and `tests/test_ghostty_engine.py`
updated and passing; live check of scrollback across a resize and across
an alt-screen exit/re-entry (the two conditions that previously needed
heuristic repair).

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
