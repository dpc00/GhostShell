# Session baton — 2026-09-18

Written by a Claude Code session at ~56% context usage, at the user's request,
so a fresh session can pick up without resuming this one. Read this whole
file before touching anything — it covers a long night with several wrong
turns that got corrected; don't repeat them.

## What actually shipped tonight (all committed, verified)

Starting point: user asked to compare GhostShell against `~/tools/ghostty`
(the real ghostty source) and `~/tools/cmux` (a maintained Ghostty-based
terminal for AI coding agents) to find real bugs.

**Finding:** GhostShell hand-reimplemented mouse protocol encoding in Python
(`terminal/mouse.py`) instead of using the native `GhosttyMouseEncoder` API
already in the vendored `ghostty-vt.dll` — the same DLL it already uses
correctly for keys. cmux confirmed the correct pattern (forward raw events,
let native ghostty own encoding).

**Fixed by omp** (a separate agent profile, working in parallel), commits:
- Native mouse-encoder migration (`_encode_pty_mouse` via `ghostty_mouse_encoder_*`),
  30 unit tests passing, verified live in a running ST plugin host.
- Separately: a real, confirmed bug in the **reattach** path — GhostShell's
  `_seed_restored_history` (used when reconnecting a tab, e.g. after an ST
  restart) reconstructs scrollback from the Sublime view's *displayed plain
  text*, even though the broker (`tools/agent_broker.py`, class
  `_OutputServer`) already tees raw PTY bytes into an in-memory ring buffer
  (`_Scrollback`) and replays them to any reattaching client. The client's
  `feed_bootstrap`/`finish_bootstrap` (`terminal/ghostty_engine.py`) were
  deliberately *skipping* import of that real native scrollback for a speed
  win, substituting the lossy text reconstruction instead — this is why mode
  state / scrollback fidelity questions kept coming up.

**Four commits, in order, all landed on `main`:**
1. `2ebf332` — unit tests for the broker's `_Scrollback` ring, no live code changed.
2. `125ef49` — the actual fix: `finish_bootstrap` now imports real native
   scrollback via `_sync()`; `_reattach_broker_view` no longer seeds from
   restored view text except when replay is genuinely empty. Proven with a
   before/after test (6 lines through a 4-row screen → history was `[]`
   before this fix, correct `L0,L1,L2` after).
3. `8789515` — durable on-disk mirror: `<registry-dir>/<pipe>.scrollback`,
   GSB1 header + circular payload, written by `_OutputServer` as bytes
   arrive. Client never reads this file directly — only ever gets replay
   over the named pipe, same as before. 45 tests passing.
4. `bf7a64d` — one-paragraph doc update in `docs/DETACHABLE_SESSIONS.md`.

Design doc: `ai/DURABLE_BROKER_SCROLLBACK.md` (also committed, `dd4c895`).

**None of this has been tested via an actual ST restart yet.** That's the
one thing still outstanding — see below.

## What was investigated and ruled out (don't re-chase)

- Missing cap enforcement in `ghostty_engine.py`'s full-rebuild scrollback
  path — looked buggy, isn't: native ghostty is already configured with
  `max_scrollback=cap` at terminal creation, so it can't over-produce rows.
- `trim_paused`/`_auto_follow` getting stuck during bootstrap — tested
  directly against the real DLL, doesn't happen; `_auto_follow` defaults
  True at `_Terminal.__init__`.
- A live incident tonight where a tab thrashed violently ("bucking
  bronco" — viewport stuck deep in scrollback, 42↔43 column resize
  oscillation, stray keystroke corruption) was **not** a reattach/scrollback
  bug — omp's own transcript confirmed it was self-inflicted: it opened a
  disposable Sublime window and used Computer Use OS-level automation to
  test mouse clicks live, which triggered a real gutter/resize oscillation
  (same class as historical commit `c93860c`) and stole OS focus while the
  user was typing. Already resolved by closing that window; no code bug.
- oh-my-pi's `/usage` bar panel: **bar fill = % used, numeric label = %
  free** (opposite of what it looks like at a glance) — see
  `feedback_verify_display_convention_before_filing.md` in project memory,
  this has bitten this exact investigation twice now.

## Current cleanup state (mid-cleanup, not finished)

The user is deliberately killing sessions before an ST restart, specifically
*because* the reattach code just changed and they don't want ST's automatic
reattach-on-restart to test the new code involuntarily against real work.
Logic: if nothing is registered as a live broker when ST restarts, nothing
can lock up on restart.

- **omp's session: killed.** Its work was already fully committed (see
  above) before killing, so nothing was lost.
- **This Claude session:** the user does not want to resume it (context is
  at ~56%) — expect it to be killed too, which is why this baton exists.
- Broker registry (`%LOCALAPPDATA%\GhostShell\broker_sessions\`) currently
  has exactly 2 files: this session's own entry
  (`ghostshell_9791ab00570d418a9223.json`) and one **pre-existing, unrelated
  stale entry from 2026-09-16** (`ghostshell_090d228c26714e40b2e3.json`) —
  not something caused tonight, but worth cleaning up.
- The user saw **3 sessions** in a "List Sessions" check and flagged it as
  a possible mess. Only 2 registry files exist on disk, so the 3rd is most
  likely a stale in-memory `_Terminal` reference the List Sessions command
  also scans for (`AiTerminalListSessionsCommand`/`_local_terms()` walks
  `gc.get_objects()` for orphaned objects) — **not independently confirmed,
  worth checking properly in the fresh session** rather than assuming.

## What's still open / next steps

1. **Verify the "3 sessions" discrepancy** — run List Sessions again in the
   fresh session (after this one is gone) and confirm what the 3rd entry
   actually is before assuming it's harmless.
2. **Test the reattach fix for real**, but carefully. The safest sequencing
   discussed and agreed with the user:
   - Kill/end every existing session first (in progress — see above), so
     ST's automatic reattach-on-restart has nothing real to act on.
   - Spawn one throwaway, disposable profile tab with a small amount of
     test content in it.
   - Restart ST.
   - Confirm the disposable tab reattaches with real, correct scrollback
     (not blank, not garbled) — this is the actual test of `125ef49`.
   - Only after that passes clean should real/important sessions be trusted
     to reattach through this code again.
3. Clean up the stale 2026-09-16 registry entry if it's confirmed orphaned.
4. Nothing else is pending — the mouse-encoder and scrollback-fidelity work
   itself is done, tested, and committed. This baton is about *safe
   verification*, not unfinished implementation.

## Where to find more context

- Project memory:
  `C:\Users\donal\.claude\projects\C--Users-donal-projects-GhostShell\memory\`
  — `MEMORY.md` is the index. Most relevant new entries from tonight:
  `project_mouse_encoding_reimplemented_not_native.md`,
  `reference_ghostshell_cast_log_location.md`,
  `feedback_verify_display_convention_before_filing.md` (updated tonight
  with the confirmed bar/label convention).
- Earlier handoff from the same investigation:
  `HANDOFF_MOUSE_ENCODING_AUDIT.md` (repo root) — the mouse-encoder part is
  now fully superseded/complete, but has useful background on the original
  ghostty/cmux comparison.
- omp's own session transcript (far more reliable than polling the
  Sublime terminal view for status) is the most recent file in
  `~/.omp/agent/sessions/-projects-GhostShell/*.jsonl`, sorted by mtime.
- Repo is on `main`, working tree clean except this file (untracked) and
  possibly `HANDOFF_MOUSE_ENCODING_AUDIT.md` (also untracked, harmless).
