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

## Stage 4 — Bind native synchronized-output state (DONE); native mouse encoder (SPLIT OUT, not done)

**Revision note:** the original version bundled two unrelated changes
under one stage. They needed different amounts of verification, so they
were split rather than landed together.

**Part A — synchronized-output mode query — DONE, 2026-08-25.** The
original evidence claimed *two* independent regex state machines for
DEC mode 2026: `_on_data`'s `_sync_update_open` in `ai_terminal.py` and
`update_replace_scroll` in `ghostty_engine.py`. Checked both before
touching anything — they are not the same kind of thing:

- `_on_data`'s `_sync_update_open` really was a pure level query ("is
  mode 2026 on right now") reconstructed via regex after the fact. This
  one native-backed it correctly: added `MODE_SYNC_OUTPUT = 2026` to
  `ghostty_vt.py`, `Screen.sync_output` (defaults `False`), and
  `GhosttyParser._sync()` now sets it via
  `self._mode(gvt.MODE_SYNC_OUTPUT)` (`ghostty_terminal_mode_get`,
  confirmed present in the shipped DLL by symbol lookup before use).
  `_on_data`'s regex scan and the `_sync_update_open` attribute are
  removed; `_do_render` reads `term.screen.sync_output` directly. The
  render path's own "stop waiting after 0.5s" safety valve became a
  separate `_sync_defer_forced` latch, since it can no longer force the
  (now native-backed, not Python-tracked) state itself false — it just
  ignores it until sync_output naturally goes false again.
- `ghostty_engine.py`'s `update_replace_scroll` is a *different*
  mechanism and must NOT be touched: it decides, from raw byte position
  within one PTY chunk, whether a CUP-home occurred *while* mode 2026
  was transiently open earlier in that same chunk — a question about
  event order within a string, which a post-hoc native mode query
  cannot answer (feeding the whole chunk first, then querying, only
  sees the mode's value at the end of the chunk). This function feeds
  `merge_replace_scroll_history`'s full-rebuild decision, the exact
  mechanism Stage 3 already established is load-bearing. Left as-is.

Verified: new `SyncOutputModeTests` (`tests/test_ghostty_engine.py`)
against a live `GhosttyParser`/DLL, including the redundant-"h"
(Grok-style) case; existing `test_do_render_defers_while_synchronized_output_is_open`
updated to drive `screen.sync_output` through a fake parser instead of
the removed regex, still passing. Confirmed live via `eval_python`
against the running plugin. Full suite: 426 passed, same 3
pre-existing unrelated `test_launcher_flow.py` failures.

**Part B — native mouse encoder — not done, split out deliberately.**
`libghostty-vt` does expose a real mouse encoder C API
(`include/ghostty/vt/mouse/encoder.h`: `ghostty_mouse_encoder_new`/
`setopt_from_terminal`/`encode`, symbol-confirmed present in the shipped
DLL), so this is technically buildable, unlike the mouse claim would
have been if the API didn't exist. But `mouse.py`'s current
implementation and `ai_terminal.py`'s routing were built from a real
audit (`TODO-archive.md`, 2026-08-11: replaying 470 recorded asciicast
sessions to determine per-CLI mouse-tracking support) and include a
Sublime-specific feature with no native equivalent —
`_route_click_to_cursor_fallback()`, which synthesizes arrow-key
presses to reposition an app's cursor when it has *no* DEC mouse
tracking at all. Native mouse encoding would only ever help the apps
that already enable tracking; it cannot replace the fallback path, and
touching the routing risks the already-tuned per-profile
`mouse_handling`/`wheel_to_pty`/`page_keys_to_pty` settings. This is
real, substantial, separately-scoped work needing the same kind of live
verification across multiple real CLI profiles the original audit did
— not something to fold into a "bind the encoder" one-line action item.
Left for a dedicated future stage if wanted.

## Stage 5 — PTY-resize applied-state accounting (DONE); row suppression kept (REVISED)

**Part A — PTY-resize failure accounting — DONE, 2026-08-25.** Verified
first: `_Terminal.resize` already had *some* failure handling (a
try/except around the parser/screen resize that forgets the target size
on failure, with a comment explaining exactly why), but the actual
`self.pty.resize(cols, rows)` call right after it was bare — no
exception handling, no return value, nothing. Checked `_Pty.resize`
(ConPTY) and `_PosixPty.resize` (TIOCSWINSZ): both already catch and log
their own failures but always returned `None` either way, so a rejected
native resize looked identical to a successful one to the caller. Since
`_Terminal.resize`'s own early-return guard is
`if cols == self._last_cols and rows == self._last_rows: return`, and
`_last_cols`/`_last_rows` were set to the new size *before* the PTY call
regardless of outcome, a rejected resize could permanently wedge the
terminal at the wrong native size: a later poll measuring that exact
size again would match the (falsely) recorded last-applied size and
skip retrying forever.

Fixed narrowly: both `resize()` methods now return `True`/`False` for
whether the native call actually succeeded (behavior and logging
otherwise unchanged); `_Terminal.resize` resets `_last_cols`/
`_last_rows` to `None` on `False`, mirroring the existing parser-failure
branch exactly. New test
`test_resize_forgets_target_size_when_pty_rejects_it`
(`tests/test_launcher_flow.py`) drives a fake PTY that always rejects
and confirms an identical follow-up resize retries instead of being
skipped. Full suite: 427 passed, same 3 pre-existing unrelated
`test_launcher_flow.py` failures as the running baseline.

**Part B — "remove resize-time row suppression" — NOT done, same
reason as Stages 2/3.** The plan's second action item proposed removing
`_Terminal.resize`'s row-pinning under `force_main_screen` (only column
changes are ever forwarded to the child; rows stay pinned to whatever
was last sent) as unwanted policy. Read the code before touching it —
it is not incidental, it's a second deliberate compensation for
`force_main_screen` (kept intentionally per Stage 2), with the exact
failure mode it prevents spelled out in its own comment: on the primary
screen, vertical space is indefinite scrollback, not a fixed page;
forwarding a row-count change still reaches the child as a real resize,
so a fullscreen TUI that believes it's on the alt screen repaints its
whole frame — but with no alt-screen erase, the old frame merely scrolls
into history instead of being cleared, which reads as a duplicate or
garbled banner. Since `force_main_screen` stays true by design, this
pinning must stay with it; removing one without the other reopens a
concrete rendering bug for exactly the apps `force_main_screen` exists
to help. This is the third stage where the Ghostty/cmux comparison
recommended removing something that turned out to be a deliberate,
already-diagnosed compensation for a GhostShell-specific design
decision neither reference implementation has any equivalent of.

Exit criteria (met, Part A only): PTY resize failures are visible and
retried instead of silently wedging the terminal at an unconfirmed
size; row suppression under `force_main_screen` is unchanged.

## Stage 6 — Idempotent parser/native-resource teardown (DONE, 2026-08-25)

Verified before implementing: `GhosttyParser` really did have no
close/free path at all — `self._term`, `self._render_state`,
`self._row_iter`, `self._cells`, and the lazily-created
`self._key_encoder`/`self._key_event` were allocated once in `__init__`
and reused for the parser's whole life, but `_Terminal.kill()` never
touched `self.parser`. Every closed tab leaked all of it until Sublime
itself restarted. Unlike Stages 2/3/4-mouse/5-partB, no comment or
history anywhere suggested this was deliberate — it looks like a plain
gap: teardown was wired up for the PTY/OS-process side but never
extended to the native ghostty-vt side when that binding was added.

**Action taken, narrower than the original generation-token proposal:**

- `GhosttyParser.close()` (`ghostty_engine.py`): idempotent (guarded by
  a `_closed` flag), frees key event, key encoder (both `getattr`-
  guarded since they're only created lazily on first keypress), row
  cells, row iterator, render state, then the terminal — reverse of
  acquisition order. All five free functions were already bound in
  `ghostty_vt.py`; nothing new needed there.
- `_Terminal.kill()` now joins the PTY reader thread (bounded, 2s
  timeout) before calling `parser.close()` under `self._lock`. This
  matters because the reader thread can still be inside
  `parser.feed()` when `kill()` runs on a different thread — freeing
  native resources concurrently with an in-flight call touching them is
  a native use-after-free, not a Python exception, so it can't be
  caught after the fact. `pty.kill()` (called just before) already
  closes the pseudoconsole handles, which should unblock the reader's
  blocked `ReadFile` promptly; if the join still times out, the
  resources are deliberately leaked (logged) rather than freed
  unsafely — a bounded leak is recoverable (process exit), a crash from
  a bad free is not.

**Not done, deliberately smaller than proposed:** no generation-token
system, no "unbind the write callback" step, no `close-during-active-
render` test. The join-then-close ordering above is sufficient to make
freeing safe without needing per-call liveness checks throughout the
parser — cmux needs a generation token because its surfaces are
referenced from many places with unpredictable lifetimes (UI, async
callbacks, other threads) simultaneously; `GhosttyParser` has exactly
one thread (the PTY reader) that can call into it after construction,
and that thread is now provably stopped before `close()` runs.

Verified: `ParserCloseTests` (`tests/test_ghostty_engine.py`) against
the real DLL — close with no keys ever encoded, close after the lazy
key encoder was allocated, and double-close (idempotency) all run
clean with no exception. `test_kill_closes_the_parser_once_the_reader_thread_has_stopped`
(`tests/test_launcher_flow.py`) covers the `_Terminal.kill()` ordering
and its own idempotency. Confirmed live via `eval_python` against the
running plugin: feed, encode a key (allocating the lazy resources),
resize, close, close again -- no crash. Full suite: 431 passed, same 3
pre-existing unrelated `test_launcher_flow.py` failures as the running
baseline.

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
