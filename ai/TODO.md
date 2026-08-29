# ai_terminal TODO

Before expanding process reattachment, transcript logging, or restart-time
screen reconstruction, read `ai/ARCHITECTURAL_BOUNDARIES.md`. It records the
comparison with SublimeREPL, TerminalView, Terminus, and terminus-persistence,
plus the explicit stop rule for bounded restart recovery.

## Saved working directory survives restart — LIVE VERIFIED (2026-08-29)

Selected the GhostShell directory with **Set Ai Terminal Working Directory**,
launched and exited Qwen, restarted Sublime Text, then launched Qwen from
**Tools | Ai Terminal**. It opened directly in the saved GhostShell directory
without showing the directory picker, as intended. Automated verification for
the accompanying launcher, bounded broker replay, and exact painted-tab log
changes: **482 passed, 2 skipped**.

## ACCEPTED LIMITATION — cursor keys stay on the command line; mouse selects and minimap scrolls (live accepted 2026-08-29)

**Current decision:** in this profile, cursor-movement keys remain owned by
the live command line. Mouse selection works, and the minimap provides
scrollback navigation. The user confirmed this division is acceptable. Do
not pursue a click-to-reposition or scrollback-cursor redesign unless a new
requirement is raised.

**Current live status (2026-08-29):** the previously described confusing
PageUp/caret behavior was not occurring because PageUp itself did nothing in
the normal Codex terminal. Root cause: the Codex profiles sent page keys to
the PTY for Codex's Ctrl+T detail viewer. Live testing confirmed PageUp works
there, but the viewer is mostly dense file/tool detail and of little use for
ordinary conversation review. Removed those profile overrides; **live
verified:** PageUp now pages Sublime's normal terminal scrollback. Do not
pursue the old `terminal.offset` hypothesis without a new reproduction. The
separate cut/paste limitation below remains a design limitation.

**User's live report, same session as the caret-staleness confirmation
above.** Two compounding gaps, not one:

1. **Plain arrow keys can't select scrollback text.** By design (Terminus-
   style: arrows always go to the live PTY command line unless copy mode is
   on), so after PageUp into scrollback, u/d/l/r "snap back" to the command
   line instead of moving within the scrollback. Copy mode (Ctrl+Alt+C) is
   the existing intended answer for keyboard-driven selection — user recalls
   it as a working solution "once upon a time," but it is not, on its own,
   a full fix for the actual goal below.
2. **Mouse click on the command line does not reposition the PTY's edit
   cursor.** Expected, not a regression — `click_to_cursor_fallback_enabled`
   defaults to `false` for real profiles (per the 2026-08-21 baton: gated
   off along with `host_cursor_paint_enabled`, and explicitly not touched
   since). So neither keyboard nor mouse click can precisely move the live
   edit-insertion point; only mouse-drag selection is precise (Sublime owns
   selection/mouse unconditionally, independent of the PTY, per the
   Terminus-style rewrite's own design constraints above).

**Net effect: no reliable way to cut text from scrollback and paste it back
in at a chosen point in the command line without the mouse for both halves
of the operation** (drag-select the source with the mouse — works — but
there is no equally precise way to place the paste-target cursor, since
clicking the command line doesn't move it and arrows don't either once
you've left copy mode). **User's own assessment, explicit:** "may not be
any solution to this... it may be a 'live with it' proposition. Until
someone thinks up some neat idea." Recorded as an open limitation, not
assigned a fix — do not attempt one without a concrete design, and do not
flip `click_to_cursor_fallback_enabled`'s default without asking first, per
the standing directive already on that gate.

## Terminus stage 2 merged to main — 2 of 3 original bugs confirmed fixed live, one new caret finding, permission prompt still untested (2026-08-27, later still)

Real-streaming-session verification of the three originally-reported bugs
(typing while scrolled back mid-stream, PageUp/PageDown mid-stream, a
permission prompt staying visible) — the one item stage 2 was still
waiting on per the entry below — was first attempted in the portable
worktree instance with a real Claude session and initially stalled: it
could not reliably be coaxed into a long enough streaming reply to
exercise the mid-stream scenarios (several prompt attempts, including ones
designed to force long output, did not produce a sustained stream to test
against). One partial data point from that stalled attempt: typing into
the input while scrolled back **but idle** (no active stream) worked
correctly — it scrolled to reveal the command line and the typed text
landed intact — but that's not the actual reported scenario, which
requires an active stream.

**User's explicit decision, given the choice between (a) merge and verify
by living with it, (b) one more attempt at forcing a real stream, or (c)
shelve stage 2 unmerged: chose (a).** Cherry-picked both stage 2 commits
(`403c8ab`, `06f6de6`) from `terminus-stage1` onto `main` (clean, no
conflicts — `main`'s only commit ahead of the branch's merge-base was a
documentation-only change to this file). Full suite re-run on `main`
after the merge: **448 passed, same 1 pre-existing unrelated failure**
(`AiTerminalEndSessionCommand` not registered in `PluginLoader.py`).

**Unstuck, same session, minutes later.** The fix for the stall above: have
the assistant itself interleave short paragraphs of text with explicit
multi-second pauses (`sleep 5` between chunks) so the session stayed
visibly "streaming" long enough to interact with — previous short replies
had simply completed too fast to test against.

- **Typing while scrolled back, actively streaming:** no text was lost,
  but typing anything at all causes a yank — the instant a key is
  pressed, the viewport jumps down to expose the command line. Corrects
  the earlier claim (below/above) of "no yank to bottom" — that was wrong.
  This is a real, confirmed observation, not a hypothesis. **User's
  judgment: this yank is fine** — it's the intended "typing takes you to
  where you're typing" behavior, not the original complaint (an
  involuntary jump into old history with no keypress involved at all).
- **PageUp/PageDown mid-stream:** confirmed working — paging moves the
  viewport and it stays where paged, matching the mechanical verification
  already documented below.
- **New finding, not yet root-caused:** after paging (specifically —
  reported as not occurring at other times, e.g. right after a response
  finishes with no paging involved), the ST caret is visibly off the
  command line until the next key is pressed, at which point it corrects.
  User confirmed this is "actually confusing," not cosmetic — you can't
  tell where typing will land until you've already pressed a key. Not
  diagnosed yet; a plausible but UNVERIFIED hypothesis is that caret
  placement (`text_point(cursor.y + terminal.offset, col)`-style
  positioning) is only recomputed on render events tied to new PTY output
  or an explicit keypress, not on a pure Sublime-side viewport scroll —
  so if `terminal.offset` shifted while paged away (history retiring
  during the stream), the caret's buffer position can go stale until the
  next keypress-driven reposition. **Do not act on this hypothesis without
  tracing the actual code path first**, per this file's established
  practice — record only.
  **Live-confirmed, separately (2026-08-27, later still):** user reports
  (portable instance, post env-leakage fix, otherwise-normal session) "PgUp
  puts it in scrollback, but cursor keys return it to command line" — the
  exact caret-in-scrollback-then-self-corrects behavior described above,
  now directly observed rather than only hypothesized. Not reported as a
  usability complaint this time (self-corrects immediately on the next
  keypress); still not root-caused. The underlying-mechanism hypothesis
  above (`terminal.offset` staleness across a pure Sublime-side scroll) is
  unchanged and still needs actual code tracing before any fix is
  attempted.
- **ROOT-CAUSED: the live tab's differing behavior all session was a stale
  personal settings override, not a stage-2 code issue at all.**
  `C:\Users\donal\AppData\Roaming\Sublime Text\Packages\User\ai_terminal.sublime-settings`
  (Sublime's per-user override file, outside the repo entirely, not
  git-tracked) contained exactly the five Terminus-deviation bisection
  gates, all forced back to their OLD values —
  `"host_cursor_paint_enabled": true, "click_to_cursor_fallback_enabled":
  true, "user_owns_caret_enabled": true, "caret_footer_pinning_enabled":
  true, "fast_caret_patch_enabled": false` — almost certainly left over
  from the 2026-08-21 session's live `sublime.load_settings/save_settings`
  bisection testing (that API writes to exactly this User-override file).
  Sublime merges User settings over package defaults, User always wins —
  so **this conversation's live tab has been running with
  `caret_footer_pinning_enabled: true` (old path) this entire session,
  regardless of today's `main` flip to `false`,** independent of any
  restart. The portable instance has no such override and was correctly
  running the real new defaults the whole time. This fully explains the
  arrow-key-history difference (and likely explains some of today's other
  "portable vs. live" comparisons that assumed live was just running
  unrestarted old code, when it was actually running deliberately
  overridden old settings that a restart would not have fixed). **Fixed**:
  deleted the override file. Live tab will run on real `main` package
  defaults after its next restart. GhostShell's own keymap file was
  checked and ruled out (byte-identical between `main` and the worktree)
  before this was found.

Permission-prompt-during-streaming check (the third original symptom)
still not exercised — user reports not knowing how to reliably trigger a
live permission prompt on demand, and notes a permission prompt isn't
something the user can invoke like a keypress or a page action — it's the
agent's own tool-use decision, not a deliberate user gesture with
predictable timing. Needs either a known reliable trick to force one on
demand, or accepting this check only happens opportunistically during
real use.

## Detachable reattach could permanently pin an inactive Claude tab to 1 row — FIXED (2026-08-27, ~5am)

The failed live test after commit `b165743` was not a failure of the
disposable Testing Agent profile. Direct inspection of the live terminal
registry showed Claude at `94x1`, while Codex and Testing Agent were both at
normal 40-row heights. All three Sublime views had an 821px viewport by the
time they were inspected.

Root cause: `plugin_loaded()` immediately reattached every restored broker
view. Sublime had not finished laying out the inactive Claude sheet, so
`_measure()` returned the legitimate global floor of one row and that value
was sent to the surviving broker. Main-screen profiles intentionally pin
their initial row count and ignore later row-only layout changes, so Claude
could never recover from that transient startup measurement. The tab then
rendered repeated one-row TUI fragments and appeared completely trashed.

Live recovery: deliberately cleared Claude's stale main-screen row baseline
for one synchronized resize through `_Terminal.resize`; broker, parser, and
bookkeeping all moved from `94x1` to `94x40`. The prompt and rate-limit footer
became readable again without restarting Sublime.

Permanent fix: broker reattachment now requires two identical layout
measurements 250ms apart. A restored **inactive** sheet that still measures at
the one-row floor remains detached until its normal `on_activated` path
starts a fresh confirmed attempt. A genuinely one-row **active** pane remains
valid; the fix does not invent a comfortable minimum or exceed the visible
height. Regression test:
`test_broker_reattach_does_not_pin_inactive_restored_view_to_one_row`.

Live verification used a full Sublime restart with detachable Claude and
Codex sessions. Immediately after restart only active Codex reattached, at
`94x40`; activating Claude then reattached it at `94x40`. The non-detachable
Testing Agent tab was correctly just an orphaned disposable buffer after the
restart and was closed. Full suite: 448 passed, same one pre-existing failure
(`AiTerminalEndSessionCommand` missing from `PluginLoader.py`).

## Splatter/splice corruption: one real bug found+fixed, root cause narrowed to the ctypes/native boundary (2026-08-27, ~2:30am–3:45am)

**Correction to this entry's own earlier claim** (caught in review): the
control-profile test below varied only `caret_footer_pinning_enabled`; the
other four bisection gates (`host_cursor_paint_enabled`,
`click_to_cursor_fallback_enabled`, `user_owns_caret_enabled`,
`fast_caret_patch_enabled`) were held at the same value (`false`) in both
the test and control profile, not independently tested. Only
`caret_footer_pinning_enabled` — and, per the deeper trace below, the whole
`_replace_scroll`/`merge_replace_scroll_history` machinery — is
conclusively exonerated for this bug. The other four gates are untested
for this specific corruption, not exonerated.

Stage 1 of the Terminus-rewrite plan below (removing `adjust_display_caret`'s
prompt/footer remapping) hit a documented blocker before any code change was
even trusted: `ai_terminal.sublime-settings` records a 2026-08-21 report of
real scrollback content loss when `caret_footer_pinning_enabled` was
previously disabled. Investigated (see plan file / commit `a4667d3`):
`trim_display_rows` provably cannot drop non-blank content regardless of
cursor position (existing test `test_content_below_cursor_is_kept` proves
it), so reverted the stage-1 code change back to its original conditional
gate — zero behavior change — pending live verification in an isolated
profile.

**That live verification found something more important than what it was
looking for.** Spawned the existing "Testing Agent" profile (all five
bisection gates false, `tests/mock_agent_cli.py`, Codex-style full-
transcript replay) and drove it with real input (`"5"`+CR, wait, `"9"`+CR,
wait — two-step submit, burst text+CR gets treated as paste). Reproduced,
live, the exact character-splatter/splice corruption already documented in
this file's 2026-08-21 "SUPERSEDED by a real splatter repro" entry:
`› user prompt TURN-00ent reply line 8019` (two non-adjacent stream
positions spliced together), followed by ~9 lines of rolling-digit-shift
garbage, then a clean self-heal.

**Per explicit instruction not to assume causation from correlation**
(caret display logic reads already-rendered rows; it should not be able to
mutate `Screen.history`, which is where the 2026-08-21 note already placed
the corruption): built a single-variable control. Cloned the profile
identically except `caret_footer_pinning_enabled: true` (the same value
Claude's and Codex's live tabs actually run under) and reran the identical
workload.

**Result: the same corruption occurred, at multiple points, in the pinned
control too.** Same splice shape, same rolling-digit pattern, same self-
heal. `caret_footer_pinning_enabled` specifically is conclusively not a
causal or contributing factor for this bug. This de-risks the Terminus
rewrite: the 2026-08-21 caution against disabling that gate was conflating
two different bugs.

**Root-cause chase, continued, per explicit instruction (test-first, fix,
validate against the real repro, don't assume causation).** Built a
deterministic offline tool: `replay_single_thread.py` (scratchpad, path in
session record) feeds a real captured `.cast` through the actual
`GhosttyParser`/`Screen`, single-threaded, checking rendered rows for the
splice signature after every feed. **Reproduced the exact corruption with
zero concurrency** — this is not a race condition, ruling out an entire
prior hypothesis (ctypes thread-safety) that earlier sessions had flagged
as a candidate.

**Real, independent bug found and FIXED**: `merge_replace_scroll_history()`
(`ai/terminal/ghostty_engine.py`) aligned to the *first* (earliest)
matching old row when locating where a replay dump's content starts in
prior history (`break` on first match). Since Codex-style tools redraw
from turn 0 on every dump, that matched text recurs once per prior replay
cycle — picking the earliest occurrence instead of the most recent
misaligns every subsequent row comparison, both letting real splice
corruption through uncorrected *and* silently dropping genuinely unique
older content. New test proved this with actual demonstrated data loss
before the fix: `tests/test_ghostty_engine.py`
`test_ambiguous_repeated_match_prefers_most_recent_occurrence` — confirmed
failing against the original code, passing after removing the `break` (all
4 pre-existing tests in that class unaffected — none have ambiguous
matches). Full suite: 446 passed (was 445), same 1 pre-existing unrelated
failure. **Kept — real, TDD-verified fix**, but:

**Replaying the real cast against the fixed code still reproduces the
identical corruption.** This fix is correct but not the (or not the only)
cause of the live-reproduced splatter. Instrumented
`merge_replace_scroll_history` directly (call-logging monkeypatch) and
found the corrupted text is **already present in `new_rows` before the
merge function ever runs** — confirmed at 3 separate events. This
exonerates the whole `_replace_scroll`/merge machinery for this bug too.

**Narrowed to `_sync_scrollback()`'s row-reading loop**
(`ai/terminal/ghostty_engine.py` ~801-817), which reads native scrollback
rows directly via `terminal_grid_ref` (3 ctypes FFI calls per cell). The
corrupting event read 135 new rows in one call (~37,665 ctypes calls in
one synchronous loop, from one large Codex-style dump). The corruption is
present in what these calls return — at essentially the ctypes/native-
library boundary. **Not yet distinguished**: genuine bug in the native
`ghostty-vt.dll`'s scrollback/grid tracking under this access pattern, vs.
a GhostShell-side indexing/staleness issue in assembling these per-cell
native reads into rows. This is where the session stopped.

**Root cause found, precisely — a design gap, not a small bug.** Read the
real native source (`~/tools/ghostty/src/terminal/point.zig`) directly:
`Tag.screen` addressing (`POINT_TAG_SCREEN`) is documented as relative to
"the furthest back in the scrollback history *supported*" — not a stable
absolute index over time. That explained a secondary phenomenon (a later
re-read of "the same" row returned unrelated content) but was a red
herring for the corruption itself, not its cause — confirmed by re-tracing
the actual failing call with instrumentation.

The real cause: for the corrupting sync, `new_rows[0]` was
`"TURN-03 L17 mock agent reply line 3017"` — a **mid-stream continuation**,
not a repeat of `"TURN-00"` (this dump chunk didn't start at the
transcript's beginning). `merge_replace_scroll_history`'s row-0 alignment
search found no match for it in `old_rows`, so `keep` fell back to its
default, `len(old_rows)`. Once that happens, `oi = keep + i` is
`>= len(old_rows)` for *every* subsequent row in that sync — the splice-
cleanup safety net's own guard (`oi < len(old_rows)`) can never be true
again for the rest of the chunk. **The whole protection mechanism only
activates when a dump's first row happens to align with existing history
(the common "starts at TURN-00" case) — for a chunk that starts mid-
stream, which is routine with how large PTY reads get split across OS-
level reads, the safety net goes silently inert for the entire chunk,**
including the genuinely corrupted row within it.

**Not fixed this session, deliberately.** `merge_replace_scroll_history`
exists specifically to solve an earlier, already-fixed duplicate-history
bug (2026-08-18/21) — a correct fix means redesigning how the splice
safety net anchors itself (e.g. tracking absolute position/offset instead
of re-deriving it via a row-0 content search every sync) without
reintroducing that original bug. Needs careful design plus extending
`tests/test_ghostty_engine.py::ReplaceScrollMergeTests` with a mid-stream-
start case specifically — not an improvised change at the end of a long
session. Confirmed NOT the cause: threading/concurrency (single-threaded
offline repro), `caret_footer_pinning_enabled` specifically (pinned vs.
unpinned control, identical), and the leftmost-vs-rightmost ambiguity
already fixed this session (real, kept, but insufficient alone here since
`keep` never had *any* candidate match for this event, ambiguous or not).

Full reproduction evidence and the reusable offline replay/probe tools
(`replay_single_thread.py`, `native_boundary_probe.py`) are preserved
outside the repo (scratchpad, path in this session's record) — both work
against any real captured `.cast` and are ready to validate the eventual
fix.

## Row-0 alignment gap: FIXED and TDD-verified, with an honest limit found (2026-08-27, later)

Implemented, test-first, exactly per the diagnosis above. `_best_alignment`
now returns an anchor **pair** (`new_start`, `old_start`) instead of one
collapsed linear offset — a single dump chunk can contain a plain
continuation *and* a restart back to back, which one offset can't
represent (confirmed live: forcing one offset either discarded a correct
alignment when the subtraction went negative, or, once fixed, duplicated
"in-between" rows that actually needed splice-cleanup rather than pass-
through). Also: the search now stops at the **first** `new_rows` index
with any candidate match at all — run length only disambiguates *within*
that one candidate set — and processes `new_rows` in **segments**,
re-searching alignment against the growing merged result each time one
segment's old-row reference runs out, because a single native write can
contain multiple embedded restarts concatenated in one buffered PTY read.
Iterated through three real regressions during this (each caught by the
full suite before being accepted, including two further real-cast
corruption instances only visible after fixing the first). New regression
test: `test_mid_stream_start_finds_alignment_via_later_rows` — confirmed
failing against the original code with real demonstrated data loss.

**Full suite: 447 passed, same 1 pre-existing unrelated failure.**

**Honest validation result**: diffed the real captured cast's *final*
rendered state (all 30 events) between original and fixed code
(`git stash`) — **identical**. The original code's own later-merge
self-healing already reaches this same endpoint for this specific
sequence. The fix genuinely eliminates the alignment gap (proven via
isolated tests with real data-loss prevented) and narrows the *window*
during which corruption is visible mid-session, but the 2 splices that
survive to the end of this cast have a different, deeper cause: by the
time they occur, a prior restart has already (correctly) dropped the only
clean reference they'd need to be repaired against — no history-
comparison fix, however smart, can repair a row when nothing clean
survives to compare it against. That needs either a native-`ghostty-vt`-
level fix or a same-dump self-consistency check, neither attempted.
7 pre-existing exact-duplicate lines were also confirmed identical in
both versions — not introduced this session, not investigated further.

Full precise findings (byte-for-byte before/after, every intermediate
regression and its fix) in the scratchpad `repro_notes.md`, same path as
before.

**Live-test in a disposable profile: attempted, BLOCKED (2026-08-27, later
still).** Committed the fix, restarted Sublime Text to load it, respawned
the "Testing Agent" profile. Console showed `[ai_terminal] resized PTY to
94x1` immediately after spawn, and it never self-corrected even after
repeated waits and re-sends. Root cause confirmed by reading code, not
guessed: `_measure()`'s row math is `max(_min_rows(), int(ex[1]/lh) - 1)`
and `_min_rows()` floors at 1 (`ai_terminal.py:1813`) — so `94x1` is exactly
what falls out when `view.viewport_extent()[1]` reads ~0. That means the
Sublime Text window had no real paintable viewport right after the
automated restart (minimized/occluded/not yet composited), not a
measurement bug: the 250ms layout watcher (`_LayoutWatcher._run`) polls
continuously and would have corrected a transient 0-height reading once the
extent recovered, and it did not, for either tab. Separately, the "Claude"
tab (this session's own render pane) was visibly producing garbled/
corrupted output around the same time, and `next_view`/`get_view_size`
round-trips through sublime-mcp started landing on the wrong tab (asked for
"Claude", got "Codex" twice) — an unreliable signal on top of an already
un-paintable window. Stopped there rather than keep sending blind input.
**Disposition:** the fix's substantive verification (failing regression
first, fix, 447-pass suite, real-cast replay, honest before/after diff) is
already complete and committed at `b165743` and stands regardless. Only the
"live-test only in disposable profiles" step is deferred — pending the user
restoring a visible, paintable Sublime Text window before retrying.

**Live-test in a disposable profile: RETRIED and PASSED (2026-08-27, later
still, after a fresh ST launch).** New ST process, fresh "Testing Agent"
spawn — measured a real `93x40` viewport this time (`viewport_extent`
non-zero), confirming the prior block was exactly the un-painted-window
diagnosis above, not a code defect. Drove ~30 replay cycles (mock CLI's
`5` + Enter, repeated) via direct `term.send_string()` calls (the window-
level MCP command's param is `string`, not `text` — a caller mistake this
session, not a plugin bug; also reconfirmed the console is line-buffered/
cooked, so a lone digit without a following `\r` only echoes, it doesn't
reach the child until the line is flushed — matches the established "send
text and Enter as two separate calls" convention). Mid-run (~14 turns,
buffer size 22349, 14 unique headers), the buffer showed exactly the
residual signature already characterized above: 2 splices, `TURN-07`..
`TURN-11` duplicated — no new or worse symptom. After ~30 turns the buffer
read 0 splices, 0 duplicates — but **that is eviction, not repair**: size
had dropped to 11458 with only 10 headers visible for 30 turns of
transcript, i.e. the scrollback cap had simply evicted the rows holding
the splices, exactly the "no clean reference survives to compare against"
limitation already documented above. Correcting an earlier overclaim in
this same entry: nothing "self-healed." **What this run legitimately
establishes:** the fix runs live without crashing, the residual signature
appears at the same documented magnitude and does not grow under
continued load (bounded, not worse), and the profile survives 30 replay
cycles. **This closes the live-test requirement** on that basis — live
behavior matches the offline characterization, including its limitation.

## Terminus rewrite, stage 1: raw-cursor path verified in a genuinely isolated process (2026-08-27, later still)

The in-code note at `_do_render` (added earlier this session after a
reverted attempt) required this be "live-verified in a genuinely separate
process" before `caret_footer_pinning_enabled`'s live default could be
touched. Built that: a portable Sublime Text install
(`D:\Programs\Sublime Text`, own `Data` dir, own `sublime-mcp` on ports
9520/9522) pointed at a real `git worktree` (`D:\GhostShell-caret-test`,
branch `terminus-stage1`) rather than a junction to the live repo — a
junction would have let the live process's own file watcher pick up the
same edits and hot-reload into this conversation's tab, which is exactly
what the in-code note was guarding against. Isolation confirmed
empirically before any behavioral edit: touched a comment in the worktree,
checked the live instance's console — no reload, no output. Copied the
gitignored `ghostty-vt.dll` into the worktree by hand (not tracked in
git). In the worktree's `ai_terminal.sublime-settings` only, flipped
`caret_footer_pinning_enabled` to `false` (settings-only change, code
untouched, so this stage's actual code deletion is still separately
pending). Restarted the portable instance; live instance's console stayed
silent, confirming no crossover.

**User tested a real profile there directly: "multiline typed in, ST
cursor appearance and position perfect."** This is the confirmed fix for
the multi-line-prompt cursor bug (CURSOR_SYSTEM_HISTORY item 2) this whole
rewrite was chasing. **Permission-prompt-obscured-by-typed-input scenario
also tested: "first permission prompt was fine."** PageUp/PageDown also
retested here and confirmed still correct: "pgup/dwn working, scrolls view
properly" — no regression from the raw-cursor path on the fix made earlier
tonight for those same keys. Both of tonight's originally-reported live
bugs are now confirmed fixed by the raw-cursor path, in the isolated
worktree. Not yet decided: whether to carry this
settings flip back to the main repo (making it the default for real
profiles) or proceed to the plan's literal code deletion first. Both need
the user's call — do not default the live gate without asking, per the
original directive.

## Terminus rewrite, stage 2: single-anchor viewport-follow, mechanically verified (2026-08-27, later still)

Built and committed on `terminus-stage1` (worktree `D:\GhostShell-caret-test`)
in two deliberately small commits, per advisor guidance after tracing that
the "dead" pin/settle machinery wasn't actually dead: `_pin_viewport_rest`
sets `term._last_vp_y = rest` (rest is always 0.0) as a side effect
independent of whether the guarded `_set_viewport` write executes, and
that same `_last_vp_y` field is read by the live, load-bearing auto-follow
drift check a few lines earlier in `_do_render` — so it already had two
incompatible writers before any of this started, not zero live readers.

**Commit 1** (`403c8ab`): introduced `term._live_anchor_y`, a single-writer
field mirroring every one of the 11 existing `_last_vp_y` write sites.
Nothing reads it yet — purely additive, `_SCROLL_MANIPULATION_ENABLED`
stays `False`. 448 passed, same 1 pre-existing failure.

**Commit 2** (`06f6de6`): the one follow-decision comparison now reads
`term._live_anchor_y` instead of `term._last_vp_y` (a no-op today since
commit 1 made them always equal — establishes which field the decision
actually depends on going forward), and `_SCROLL_MANIPULATION_ENABLED`
flips to `True`, making every `_set_viewport()` call in the file live
instead of a no-op for the first time since 2026-08-18. Deliberately did
NOT delete `_settle_viewport` / `_pin_viewport_rest` / `_pin_terminal_viewport`
/ `_host_rest_y` in this commit — they're no longer a correctness risk
(both fields written consistently since commit 1), only an architectural
cleanup, kept separate so a regression is traceable to one of two changed
lines. 448 passed, same 1 pre-existing failure.

**Mechanically verified live** (portable ST instance, own `sublime-mcp`
connection on port 9522, driven directly via `eval_python` against the
mock "Testing Agent" replay profile — not a real streaming agent, see
caveat below): spawned a session, confirmed the viewport actually moves
now (`vp == layout_extent - viewport_extent`, exact, where it was
permanently stuck at `(0, 0)` before this stage); grew the transcript and
confirmed it kept following (`_live_anchor_y` tracks `vp[1]` exactly at
each step); manually set the viewport away from bottom and grew the
transcript again — `auto_follow` correctly flipped `False` and the
viewport did NOT get yanked back (stayed exactly where set) despite the
buffer growing underneath it; manually scrolled back near bottom and grew
the transcript once more — `auto_follow` correctly re-engaged and the
viewport snapped to the new exact bottom. All three mechanics behave as
designed.

**Not yet done — still required before this reaches main:** the mock
profile's growth is a synchronous burst, not real streaming output, so
this hasn't exercised the actual reported bugs (typing while scrolled
back into a *live* response, paging away mid-stream, a permission prompt
staying visible) against a real agent. Needs the user watching a real
Claude/Codex session in the portable window for those three specific
checks before this settings flip + code change is proposed for main.

## SUPERSEDES the "command-row + headroom" plan below — Terminus-style rewrite, decided (2026-08-27, ~2am)

**The "command-row detection with permission-aware headroom" architecture
note further down this file (under "lost keystroke + viewport jump") is
superseded by this entry. Do not implement it as written — read this
first.**

**Two things converged independently and correct that plan:**

1. **`_SCROLL_MANIPULATION_ENABLED = False`** (`ai_terminal.py:1594`) is a
   real, deliberate kill-switch from **2026-08-18**, "per user directive":
   every `_set_viewport()` call in the file is currently a hard no-op.
   Traced every live-read site of `term._auto_follow` (not just the
   write choke-point) — both consumers (`_scroll_to_bottom` via the mouse-
   wheel handler, `_settle_viewport` via the render loop) terminate in
   `_set_viewport`. **Nothing in this engine moves the viewport right
   now.** Sublime's own native scroll behavior is 100% of what's visible.
   This means tonight's earlier PageUp/PageDown "auto-follow" fix (commit
   `a53fbd7`) does not do what its own commit message says — it still has
   a real, separate effect (pausing scrollback eviction via the
   `trim_paused` mirror), but the "yank back" mechanism it claims to fix
   doesn't exist under the current setup. The real yank-back must be
   Sublime's native scroll-to-caret reacting to wherever the render loop
   places the ST caret each frame.
2. **Independent architecture audit by Codex** (same evening, `Codex`
   tab, working from the same `ai/TODO.md` notes): cloned Terminus's real
   source and did a full comparison. Conclusion: GhostShell's caret/
   viewport system models "is the user reading history" as **persistent,
   mutable, actively-maintained state** (`_auto_follow`, 22 call sites of
   `_set_auto_follow`) that every event handler (keys, mouse, scroll,
   render, trim, selection, TUI detection) has to update correctly,
   instead of an *observation made at render time*. On top of that, it
   tries to reverse-engineer application UI from rendered text
   (`caret.py` pattern-matches `>`/`❯`, box borders, footer placement)
   rather than trusting the terminal engine's own authoritative cursor
   position — which is exactly the mechanism behind the stale-prompt
   permission-dialog bug noted elsewhere in this file. Full critique
   (unedited) preserved in the `Codex` tab's transcript this session;
   key claims independently re-verified against source before accepting
   them, not taken on faith.

**Terminus's actual algorithm** (`terminus/render.py`,
`TerminusShowCursorCommand`, ~20 lines total, verified directly against
the cloned source, not summarized secondhand):

```python
def focus_cursor(self, edit, terminal):
    # ST caret := raw PTY cursor position, mapped 1:1. No search, no
    # prompt/footer inference -- terminal.offset + screen.cursor.y/x are
    # authoritative values the terminal engine already tracks.
    pt = view.text_point(cursor.y + terminal.offset, col)
    sel.add(sublime.Region(pt, pt))

def scroll_to_cursor(self, terminal):
    last_y = view.text_to_layout(view.size())[1]              # true buffer end
    viewport_y = last_y - view.viewport_extent()[1] + line_height
    offset_y = view.text_to_layout(view.text_point(terminal.offset, 0))[1]  # top of live screen
    y = max(offset_y, viewport_y)   # true-bottom, floored so it never hides the live screen's start
    view.set_viewport_position((0, y), False)
```

Called as: `terminus_keypress` → `view.run_command("terminus_show_cursor")`
→ `terminal.send_key(...)`. That's the entire interactive contract. No
`_auto_follow`, no `_host_rest_y`, no `_compensate_trim_scroll`, no TUI-
vs-shell branching in the ordinary path, no copy-mode-as-prerequisite for
selecting text — Sublime owns selection/mouse-wheel/scrollbar
continuously, unconditionally.

**Why this resolves Claude's variable-footer problem without a separate
command-row/headroom mechanism:** Terminus never has this problem because
a plain shell's prompt genuinely *is* the last buffer line. GhostShell
already has a render-time trim step whose entire job is exactly this —
`_trim_display_rows` (`ai/terminal/render.py`) cuts trailing footer/blank
padding so the rendered buffer's last row lands at/near the live cursor.
**If that trim is correct, Terminus's literal "scroll to buffer bottom"
also keeps the command line in view — no command-row search, no dynamic
headroom sizing needed at the viewport layer at all.** The variable-footer
problem becomes: is `_trim_display_rows` correctly and consistently
trimming to the cursor across every frame shape (normal footer,
permission prompt, mid-stream output) — a render-layer correctness
question, not a viewport-anchoring one. This is *narrower and more
tractable* than the command-row + dynamic-headroom plan it replaces.

**Decided (user, explicit choice among three options — full rewrite,
incremental patching, or re-enabling the kill-switch as-is): pursue the
Terminus-style rewrite.** Not started — this session ended here due to
the hour and both Claude's and Codex's usage limits. Read GhostShell's
own `CURSOR_SYSTEM_HISTORY.md` before touching anything (it has the full
prior-art history of every previous attempt at this system and why each
was reverted or gated). Suggested shape for next session, adapted from
Codex's proposal and Terminus's actual code above, for the **normal
(non-`_tui_like`) profiles only** — fullscreen/mouse-tracking TUIs
(Grok, Qwen) need their own explicit, isolated mode, not shared branches
through the ordinary path, per Codex's point 6:

**Final reconciled constraints (Codex + Claude, both independently
source-verified against the cloned Terminus code, agreed live this
session — this wording supersedes any earlier phrasing above):**

- No cross-cutting inferred-intent state machine — not "zero state."
  Keep exactly **one** local viewport anchor (Terminus's
  `terminus_view.viewport_y` is the model: a single float, one writer).
  **One comparison** decides whether the view was already following
  (current viewport position vs. that anchor). That same single result
  controls **both** cursor-following and scrollback-eviction pausing —
  not separate `_auto_follow`/`trim_paused` flags maintained by different
  code paths.
- Raw PTY cursor only (`term.screen.cursor` mapped 1:1) — no
  `_find_prompt_row`/prompt-glyph/footer-shape recognition anywhere in
  the ordinary path.
- Sublime owns scrolling, selection, and ordinary mouse behavior
  unconditionally — no mode mutation, no synthesized PTY input for these.
- Fullscreen mouse-tracking TUI mode (Grok, Qwen) is an explicit,
  isolated **GhostShell-specific** architectural boundary — confirmed
  **not** Terminus-derived precedent (Terminus has zero alt-screen/mouse-
  tracking viewport branching; it's a plain-shell terminal). Do not cite
  Terminus as justification for that piece specifically, and do not
  leak its branches into the ordinary-profile path.

Concretely, for the **normal (non-`_tui_like`) profiles only**:

- Key/paste → position ST caret at the raw PTY cursor → encode → send
  to PTY.
- Render → the one viewport-anchor comparison above decides everything:
  if still following, show the raw PTY cursor and re-run Terminus-style
  scroll-to-cursor (`max(offset_y, viewport_y)`, see the algorithm
  above); if not, leave the viewport, selection, and scrollback
  untouched — including not trimming history out from under a reading
  user.
- Verify `_trim_display_rows` against real permission-prompt and mid-
  stream-footer frame captures (`.cast` files from tonight,
  `ai_terminal_asciinema_casts_for_troubleshooting_rendering/`) before
  assuming it's already correct.
- Candidates for removal from the ordinary-profile path once the above is
  live-verified: `_auto_follow`/`_set_auto_follow`, `_host_rest_y`,
  `_compensate_trim_scroll`, `_settle_viewport`, the periodic viewport
  clamp loop, prompt/footer caret inference in `caret.py`, `_user_owns_caret`
  as a prerequisite for selection. Keep: terminal parsing, history,
  geometry/encoding, rendering, colors, logging — separate concerns, not
  the source of the entanglement.
- `_SCROLL_MANIPULATION_ENABLED` should very likely just be deleted along
  with this, not re-enabled — its own docstring says flip it back "once
  redesigned, not patched further," and this *is* that redesign, not a
  patch.

Open/unresolved items only otherwise. Full dev-session history (root causes, fixes,
verification detail) lives in [TODO-archive.md](TODO-archive.md).

## Session baton (2026-08-27, later) — lost keystroke + viewport jump during a permission prompt; architecture lead from Ghostty's real source; diagnostic landed, no fix yet

**What the user saw, live, same evening as the gutter-reserve fix above.**
Two related incidents in the `Claude` tab: (1) mid-typing a reply, the
viewport jumped ~100 lines up into old conversation text; (2) separately, a
permission prompt was obscured (viewport not showing the live prompt row);
the user typed their intended response anyway, but **only the Enter
keystroke reached the PTY — the typed text itself was lost**, submitting
effectively empty input.

**Ruled out this session, with evidence, not guesswork:**
- **Copy-mode-turns-on-by-itself** (the other UNRESOLVED item below): the
  TEMP DEBUG call-stack logger in `AiTerminalToggleCopyModeCommand.run` is
  still live and printed nothing around this incident. Not the cause here.
- **`_tui_like()` mispinning the viewport**: checked — for the `Claude`
  profile specifically, `alt_screen` and `mouse_tracking` are both
  deliberately disabled (`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`,
  confirmed elsewhere in this file), so `_tui_like(term)` is almost always
  `False` for Claude. Keypresses should be taking the unconditional
  `_scroll_to_bottom()` branch in `AiTerminalKeypressCommand.run`
  (`ai_terminal.py` ~line 6388), not the fragile "only re-pin if drifted"
  TUI branch that uses `_host_rest_y()`. Rules out that specific mechanism
  for Claude; may still apply to `_tui_like` profiles (Grok, Qwen, etc.).

**New, concrete, not-yet-confirmed lead for the lost-keystroke half.**
`encode_key()` (the libghostty-vt key encoder called from
`AiTerminalKeypressCommand.run`) can legitimately return empty bytes
(`b""`) for "a key with no output," per its own docstring. Both the actual
PTY write (`term.send_string(code)`) *and* the `_scroll_to_bottom()` call
live inside `if code:` — so if something about the permission-prompt
frame's terminal-mode state (Kitty keyboard protocol flags,
modifyOtherKeys, app-cursor mode — all synced live by `encode_key` on
every call) made it return empty for plain characters but not for `\r`,
that would produce exactly this symptom: text silently dropped, Enter
still gets through. **Not yet proven** — no direct evidence this actually
fired during the incident, only that the mechanism exists and fits.

**Diagnostic landed (additive-only, zero behavior change).**
`AiTerminalKeypressCommand.run` now logs (rate-limited to once per view per
~2s) whenever a key that should normally produce output — a single
printable character, or enter/return/space/tab/backspace — comes back with
empty `code`: the key/modifiers, view id/name, and
`private_modes`/`alt_screen`/`mouse_tracking` snapshot at that moment.
TEMP DEBUG, same pattern as the copy-mode logger above — remove once this
is root-caused or ruled out. Next occurrence of "typed text vanished"
should show up here whether or not it's this exact mechanism.

**Architecture lead: what Ghostty and Terminus actually do, checked
against real source, not assumption.** `~/tools/ghostty/src/Surface.zig`
(~lines 4809-4884, the `.csi`/`.esc`/`.text`/`.cursor_key` input-action
handlers): every single keystroke that writes to the PTY **unconditionally
and synchronously scrolls the viewport to bottom first** — no "is this
really the footer," no drift threshold, no compensation math. The
cursor-key handler's own comment: *"We always scroll to the bottom for
these inputs."* Terminus does the same in spirit per the existing
`CURSOR_SYSTEM_HISTORY.md` note (`focus_cursor`, ~10 lines, raw PTY
position, no remapping/pinning/synthesis/override).

GhostShell's current design instead tries to *infer* correct scroll
position reactively from PTY **output** state — `_compensate_trim_scroll`,
`_settle_viewport`, `_host_rest_y`, caret-row detection via
`_find_prompt_row` — which is categorically more fragile than "scroll-to-
bottom is an unconditional side effect of the keystroke that sent input."
This is the real architectural gap behind both the viewport-jump family
below and tonight's permission-prompt incident. **Not attempted tonight,
deliberately** — per the explicit warning in the 2026-08-23 entry below,
a prior blind attempt at this exact class of change ("gate
`_compensate_trim_scroll` on `_auto_follow`") shipped and made jumping
*worse*, then had to be reverted. Any change here needs the empty-code
diagnostic (or an equivalent live capture) to land first, so a fix can be
verified against a real captured incident instead of guessed at again.

**Correction to the naive Ghostty port, from the user directly (important
— do not implement literal scroll-to-bottom):** Ghostty's target (buffer
bottom) works because a plain shell's prompt IS the last line, always.
Claude's is not: its statusline/footer area **changes height frame to
frame** (the same "6 lines below the prompt, several of which recompute
every render" behavior already documented in the 2026-08-21 entry's
`follow_ignore_trailing_lines` note). If the algorithm targets literal
buffer-bottom, the viewport jumps up/down on every footer height change
even with zero real scrolling need — a likely contributor to the
"jiggling on keystrokes" complaint, separate from the permission-prompt
incident. **The correct target is the command-line row itself** (the live
`❯`/input row, from `_find_prompt_row` or equivalent), positioned at a
fixed offset from the viewport bottom that reserves headroom for the
footer — not "scroll to the buffer's last line." `follow_ignore_trailing_lines`
is the existing, narrower version of this idea (a fixed per-profile
trailing-line count); the real fix generalizes it.

Second correction, same conversation: **that reserved headroom cannot be
sized for the normal footer alone** — approval/permission prompts render
as a noticeably larger block of text than the everyday statusline, so a
headroom tuned to the common case will still get eaten by a permission
prompt. The reserve either needs to be generous enough to cover the
prompt case too, or the algorithm needs to detect "a larger interactive
block is showing" (a menu/choice render, similar to what `_find_prompt_row`
already has to distinguish from a live prompt per the 2026-08-21 note on
resolved permission dialogs) and grow the reserve dynamically for it.

**Next steps, in order:** (1) wait for the empty-code diagnostic to catch
a real occurrence — confirms or rules out the lost-keystroke mechanism;
(2) if ruled out, `_find_prompt_row` locking onto a stale scrollback line
during a permission dialog (noted, unfiled, in the 2026-08-21 entry below)
is the next candidate for the "obscured prompt" half specifically; (3) once
either mechanism is confirmed, implement input-driven scrolling that
targets the **command-line row with reserved, permission-prompt-aware
headroom** (per the two corrections above) — not literal buffer-bottom —
as the replacement for the reactive output-side compensation system, or
determine it needs to coexist with `_tui_like` pinning for fullscreen apps.

**Two concrete fixes landed same session (not guesses -- traced code bugs,
mirroring patterns already proven safe elsewhere in this exact function):**

1. **PageUp/PageDown "dead key" while a TUI streams.** The default
   PageUp/PageDown branch in `AiTerminalKeypressCommand.run` (~line 6295,
   reached by Claude and most profiles -- not `page_keys_to_pty`) did ST-
   native page-scroll and returned *without* calling `_set_auto_follow(term,
   False)`. A second PageUp/PageDown branch further down (reached only by
   `page_keys_to_pty` profiles like Codex) already disengages follow, but
   that code was dead for every profile taking the first branch. Net
   effect, matching the `_page_keys_to_pty` docstring's own prior note
   ("native page then moves a one-frame buffer and the key looks dead"):
   the view scrolled up for one frame, the very next streaming render's
   auto-follow snapped it right back down before it was perceptible --
   PageUp read as completely unresponsive. Fixed: same
   `_set_auto_follow(term, False)` call added to the first branch, same
   intent as the mouse-wheel/click handlers that already disengage follow
   on any deliberate scroll-away gesture.
2. **Ctrl+Home/Ctrl+End did nothing.** Only `Ctrl+Shift+Home/End` had
   explicit unconditional ST-native "jump to buffer start/end" handling;
   plain `Ctrl+Home/Ctrl+End` fell through to the PTY-forward path, where
   no readline-style CLI does anything with it. Added the same unconditional
   `move_to bof/eof` handling (no Shift → no selection extend), mirroring
   the existing Ctrl+Shift pattern immediately above it. Deliberately did
   **not** touch plain Home/End (real line-editing use in a CLI) or plain
   arrow Up/Down (real command-history-recall use) -- both are genuine
   binary conflicts between "CLI wants this key" and "scrollback wants this
   key" with no universally correct default, not bugs to fix.

**Diagnostics/observability landed same session, both additive-only:**
- Rate-limited empty-`code` logging (above).
- `_update_debug_status()` (new, called from `_set_auto_follow`'s single
  choke point and the end of `_do_render`): a live status-bar readout —
  `follow=… tui=… cols×rows=… vp_y=…` — gated by
  `debug_status_bar_enabled` (`ai_terminal.sublime-settings`, default
  off). Motivated by a direct live report: `sublime.log_input(True)`-style
  console firehose (every keystroke/mouse move) has no way to correlate to
  what's on screen, unlike Ghostty's Inspector overlay; `view.set_status`
  is the cheap ST-native equivalent -- persistent text, updates live, no
  overlay/pane to build. **Future upgrade suggested, not built:** a
  minihtml panel/phantom (`view.show_popup` or similar) could give a
  richer multi-line live view instead of one status-bar line, if the
  single-line readout proves too cramped in practice.

All of the above: compiles clean, full suite 445 passed / 1 pre-existing
unrelated failure (`AiTerminalEndSessionCommand` not registered in
PluginLoader.py, present on baseline before this session too). Not yet
live-verified against a real Claude session — next session should restart
ST, enable `debug_status_bar_enabled` on a live tab, and confirm the
status line updates on PageUp/PageDown/Ctrl+Home/Ctrl+End and tracks
`follow` correctly during real streaming output.

## RESOLVED (2026-08-27) — Codex resize<->replay oscillation: 4-digit gutter reserve implemented

**Symptom.** Pinch-zoom (or any) font_size change reloads
`Preferences.sublime-settings`, which fires `_LayoutWatcher` on every open
`ai_terminal` tab. Live-caught with Codex: once a session had a
significant line count, the resize became self-sustaining --
`94x42 <-> 93x42` oscillating on its own with no further external
trigger -- driving Codex's "earlier messages" full-transcript dump to
replay repeatedly. Buffer ballooned to ~2MB with genuinely spliced/
garbled content from different points in history, at times stalling
Sublime's main thread (`get_console` returned "main-thread timeout after
5s") and desyncing the visible scroll position from live content
("stuck at line 4720", "can't get to bottom").

**Root cause.** `_measure()` computes `cols` from `view.viewport_extent()`,
which excludes ST's native line-number gutter. That gutter's width is a
function of the buffer's current total line count (widens by a digit at
999->1000, 9999->10000, ...). During an active full-history replay/rebuild,
total_lines crosses that boundary repeatedly, so `viewport_extent()` --
and therefore the measured `cols` -- genuinely changes mid-replay (not
measurement noise; the existing `accepted_cols` ±1 hysteresis in
`ai/terminal/layout.py` doesn't catch it). Resize fires -> replay
restarts at the new width -> line count crosses the boundary again ->
resize fires again.

**Fix applied.** New pure helper `gutter_digit_delta(total_lines,
scrollback_cap)` (`ai/terminal/layout.py`): returns
`reserved_digits - actual_digits`, where `reserved_digits` is the digit
width of the profile's `scrollback_history_size` cap (the buffer's real
ceiling, so that digit count never changes once reached) and
`actual_digits` is the buffer's current line-count digit width.
`_measure()` (`ai/ai_terminal.py`) subtracts `gutter_digit_delta(...) *
cw` from `usable_w` before computing `cols`, canceling the real gutter's
fluctuation so `cols` stays pinned regardless of the buffer's moving
line count. `_measure` now takes an optional `profile_name` (threaded
through all three call sites: `_LayoutWatcher._run`, `_spawn`,
`_reattach_broker_view`) to resolve the right cap via the existing
`_scrollback_size(profile_name)`. ST's own native gutter still resizes
for real; it just no longer moves `cols`.

Tests: `tests/test_layout.py` `GutterDigitDeltaTests` (boundary-crossing
cancellation, net-reservation invariant across a 1..cap sweep). Full
suite: 445 passed (1 pre-existing unrelated failure --
`AiTerminalEndSessionCommand` not registered in PluginLoader.py --
present on baseline too).

**Live-verified**, twice: (1) with `font_size` edits after the fix, the
resize storm no longer oscillates; (2) resumed the actual Codex session
that previously flooded to ~2MB and it stayed stable at 300+ lines
through a fresh `font_size` change that previously triggered the loop
every time.

## Session baton (2026-08-23) — viewport-jump: auto_follow gate tried and reverted, two findings sharpened, live capture still needed

Full detail in `ai_terminal_notes.md` (2026-08-23 entry) — this is the
short version. **Do not re-attempt gating `_compensate_trim_scroll` on
`_auto_follow`** — Grok tried it same day, shipped, made live jumping
worse ("never been this bad"), reverted (unconditional compensation
restored, `tests/test_compensate_trim.py` updated, 51 tests pass, this is
the current on-disk state, confirmed live-clean too).

Two things distinguished in Grok's surviving diagnostic capture
(`C:\Users\donal\data\logs\ai_terminal\vp_diag_id19.jsonl`), re-derived
independently rather than taken from Grok's own summary:

1. One real captured jump (-578px/34 lines) traces cleanly to
   `_compensate_trim_scroll`, and its arithmetic is correct (retired_total
   genuinely went 1272→1306). The open question is narrower than "is the
   eviction count right" (it is): `_settle_viewport`/`_scroll_to_bottom`
   runs synchronously in the same render frame right after compensate and,
   by the numbers in this capture, should have overridden compensate's
   landing spot (4571) back to the follow target (~4960) before the frame
   ever painted — but the log shows 4571 stuck, and the sampler crashed
   (`_VPW_LAST_VP` AttributeError) right at that instant, so there's no
   evidence either way on whether the same-frame snap fired. Next capture
   needs to survive past an eviction event to settle this.
2. A separate ~0.9s eased viewport drift (4961→5139px, ~22 steps) with
   `retired_total`/history length exactly constant and zero plugin-code
   viewport writes in the window (confirmed by grep, not inferred) — not
   caused by compensate or any of our own code. Two unattributed
   candidates: Sublime's own `view.show()` on focus/hover (documented
   elsewhere in the file as a real independent mechanism), or a genuine
   trackpad pan (the user-scroll detector only catches *decreasing* y, so
   a downward pan wouldn't register). `_hover_poll_tick` was checked and
   ruled out.

Do not ship a discriminator for either without a fresh, kept-alive live
capture across a real eviction — that is exactly the mistake that produced
the reverted auto_follow gate.

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
**Restart-verified 2026-08-21:** after a full Sublime restart, user
confirmed the ST vertical-line caret sits in the correct place on the
command line, and cursor-key positioning/insert is correct. No block
cursor shown and mouse click doesn't reposition the PTY cursor — both
expected, since `host_cursor_paint_enabled` and
`click_to_cursor_fallback_enabled` are currently `false` (confirmed at
the call sites, `ai_terminal.py:3404` and `ai_terminal.py:4333`), not a
regression.

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

**Current live values (2026-08-21, post-restart session):**
`caret_footer_pinning_enabled: true`, `host_cursor_paint_enabled: true`,
`click_to_cursor_fallback_enabled: true`, `user_owns_caret_enabled: true`,
`fast_caret_patch_enabled: false`. Changed from the prior baton's
all-off-except-pinning state by live-testing the first two gates directly
in this session's own terminal tab. This is still *not* the full Terminus
baseline and still not a completed bisection — `caret_footer_pinning_enabled`
and `fast_caret_patch_enabled` remain untouched because of the two
incidents below.

**`host_cursor_paint_enabled: true` — live-verified 2026-08-21.** Flipped
via `sublime.load_settings/save_settings` (the plugin's own settings API,
not a raw file edit). Screenshot before/after: prior state showed a thin
ST vertical-line caret at the prompt; after enabling, a solid block glyph
renders at the same position. No console errors.

**`click_to_cursor_fallback_enabled: true` — live-verified 2026-08-21,
first real test since this flag was added.** Direct verification of
`_route_click_to_cursor_fallback` (`ai_terminal.py:3868`), not just gate
wiring: typed known text into the live prompt via `ai_terminal_send_string`,
then invoked the real `drag_select` command with a synthesized `event`
dict targeting a point mid-string (Sublime's actual command dispatch, not
OS-level input simulation). Result: `screen.x` (the PTY's real cursor
column) moved from 18 to 7, matching the clicked target column (confirmed
via a 0.5s poll, since the move requires a real PTY round-trip: keys
written, child process processes them, re-renders).

Also found while diagnosing this: the guard condition (`screen.y != py`)
can fail because `_find_prompt_row`'s `❯` scan sometimes locks onto a
stale scrollback line (e.g. a resolved permission dialog's "❯ 1. Yes")
instead of the live input row — the first two click attempts silently
no-op'd for this reason before one where `py` and `screen.y` genuinely
matched. Not yet filed as its own bug; worth deciding whether
`_find_prompt_row` should exclude menu/choice lines from its scan.

**`user_owns_caret_enabled: true` — live-verified 2026-08-21.** Tests both
directions of `AiTerminalViewListener.on_selection_modified` (`ai_terminal.py:3731`)
against `_command_line_row_range` (`ai_terminal.py:3566`):
- **Outside the drawn command-line box** (clicked well up in scrollback via
  `drag_select`): `term._user_owns_caret` latched `True`, and the ST
  selection genuinely stayed put across 3 polls of live, actively-streaming
  render frames (`screen.dirty=True` each poll) — proving
  `user_owns_caret_enabled`'s `keep_selection` gate
  (`ai_terminal.py:6089-6093`) really does protect a manual selection from
  being overridden, not just that the flag gets set.
- **Inside the drawn command-line box**: `term._user_owns_caret` correctly
  flips back to `False`, handing control back to the PTY cursor. Verifying
  this directly via synthesized clicks proved unreliable — the box's row
  bounds drift by 2+ rows between computing a target point and the real
  Sublime-dispatched event firing, because this session's own output was
  streaming into the same tab being tested (a genuine live-scroll race, not
  a code bug). Resolved by instantiating `AiTerminalViewListener` and
  calling `on_selection_modified()` directly in the same call as setting
  `view.sel()`, eliminating the race; confirmed `_user_owns_caret` flips
  `False` when the selection is genuinely inside fresh-computed bounds.

Leaves only `caret_footer_pinning_enabled` and `fast_caret_patch_enabled`
untouched from the prior baton's state, per their two documented incidents
above (dropped scrollback content; unreliable diff-patch under an unstable
cursor row) — not retested this session, deliberately.

**Isolated-tab testing method established, 2026-08-21 — use this instead of
testing in a live conversation tab.** Live-testing gates directly in the
Sublime tab this very conversation renders through caused repeated
collateral damage this session (a manual selection left mid-scrollback, a
whole-buffer select, a caret pinned at row 0 that yanked the user's
viewport to the top of the tab). Fix: spawn the existing `Testing Agent`
profile (`tests/mock_agent_cli.py`, no flags — Codex-style full-transcript
replay on every Enter, controllable via `ai_terminal_send_string` with
`5
`/`s
`/etc.) in its own tab via `_spawn(window, path,
profile="Testing Agent")`, and drive/inspect that tab's buffer instead.
Bisection gate settings are global (`ai_terminal.sublime-settings`), so
this tab runs under the exact same live gate state as any other profile —
no loss of coverage, zero risk to a real conversation.

**Splatter-bug follow-up, live-plugin repro attempt (2026-08-21).** The
isolated `tests/test_splatter_stress.py` subprocess (see above) only
exercises `Screen`/`GhosttyParser` directly, not the actual
`AiTerminalRenderCommand`/`view.replace` render path — this was a first
attempt to close that gap using the live plugin, safely, in the isolated
Testing Agent tab. Found a **real duplicate-line artifact at initial
spawn**: right after `_spawn`, before any input was sent, the buffer
already contained `TURN-01 L11`/`L12` each appearing twice consecutively.
Sending 5 more turns immediately after (`ai_terminal_send_string` with
`"5"` then `"
"` — this mock is line-buffered, not raw-keypress
despite its docstring) added 5 clean turns with **zero** further
duplication, and a full-buffer scan for the other splatter symptom (stray
high-codepoint characters / long ─ runs) came back clean too. **Read as:**
the duplication is specific to the very first replay/render right after
spawn, not a steady-state per-turn bug — consistent with the TODO's
existing "Not applied (on purpose)" note on `update_replace_scroll`/history-
append dumps needing their own failing parser test, but not yet connected
to that code path with certainty. Worth a dedicated repro (spawn, capture
raw PTY bytes for just the first render, replay through `GhosttyParser`
in isolation) before concluding more.

**SUPERSEDED by a real splatter repro, same session, minutes later.**
Continuing to drive the Testing Agent tab (spawn + one `5
`, 7 turns
total) surfaced genuine intra-line character-splatter, caught live by the
user watching the tab and confirmed in `term.screen.history` directly
(not just an ST-buffer/render-layer artifact — the corruption is baked
into the Python `Screen.history` deque itself, index 30 of 216):

    30 '› user prompt TURN-01ent reply line 7011'

The real line is `› user prompt TURN-01`; `ent reply line 7011` is the
tail of an unrelated `• TURN-07 L11 mock agent reply line 7011` line,
spliced directly into it. TURN-07 did not exist yet when TURN-01 was first
retired — this is old, already-retired content getting corrupted by a
much later write, not a fresh line born wrong.

**Narrowed past the point of vague ctypes suspicion:** checked
`term.screen.retired_total` (2) against total history length (216) —
almost every line came through `_sync_scrollback`'s **full-rebuild path**
(`ghostty_engine.py:606-608,623`: `s.history.clear()` then rebuild every
row via `terminal_grid_ref`/`_cell_from_grid_ref`, `notify=False`), not
the incremental `start=last` diff path (that path's own
`s.history.append(rstrip_cells(cells))`/`_retire_line` both build a fresh
Python list per row — audited this session, no aliasing bug in either).
This mock replays the full transcript on every Enter (Codex-style
home+dump), so a full rebuild runs on every turn. Since the rebuild is
single-threaded, entirely inside one `_sync_scrollback` call, under
`term._lock`, with no Python-level list aliasing in the path — the
splice must be happening in the native call sequence itself:
`self._g.terminal_grid_ref(self._term, pt, ctypes.byref(ref))` /
`_cell_from_grid_ref(ref, palette)` (`ghostty_engine.py:613-619`)
returning wrong content for some `(x, y)` during a 216-row rebuild loop.
**This is now the concrete, reproducible trigger condition the TODO's
priority-6 ctypes audit ("if an isolated stress test reproduces the
splatter and points at the native boundary") was waiting for** — a full-
transcript-replay TUI (Codex-style, or this mock) driving a large
scrollback rebuild, not raw concurrent feed/render load (which
`test_splatter_stress.py` tested and found clean).

**Next step, concrete and no longer speculative:** write an isolated
subprocess test (same shape as `test_splatter_stress.py`) that feeds a
`GhosttyParser`/`Screen` pair a Codex-style repeated full-transcript-dump
pattern (CSI ?2026h, CSI H, N turns of content, CSI ?2026l, repeated with
growing N) and asserts every retired history line matches its known
source text exactly. If it reproduces there, the bug is confirmed inside
`terminal_grid_ref`/`_cell_from_grid_ref` or the native library itself,
not this plugin's Python code, and the fix path is either a workaround
in `_sync_scrollback` (defensive re-validation before overwriting
already-retired history) or a report upstream to libghostty-vt. 

**Isolated repro attempt result, 2026-08-21 — did NOT reproduce the exact
splice, but found a DIFFERENT, real, more serious bug: silent content
loss.** Wrote `tests/test_splatter_replay_rebuild.py`: feeds a
`GhosttyParser`/`Screen` (80x24, `history_cap=2000`) the exact
`mock_agent_cli.py` `ReplayAgent` byte pattern (seed 3 turns, then 5 more
turns each re-sending the WHOLE growing transcript). At these dimensions,
no cross-turn splice occurred (the `spliced` check — a history line
containing more than one `TURN-NN` tag — came back empty), so the exact
live splice is dimension/timing-sensitive and not yet reproduced
byte-for-byte in isolation.

**What it found instead, deterministically, every run:** `TURN-00`'s
entire 29-line block (prompt + 28 replies) vanishes completely from both
`screen.grid` and `screen.history` the moment the FIRST post-seed turn is
added (turn 3's full-transcript re-dump) — not evicted gradually, gone in
one step. The numbers do not reconcile: `parser._last_scrollback_rows`
(native-reported total) jumps from 64 to 157 after that one feed, but
Python's rebuilt `history + grid` only totals 117 lines (93 history + 24
grid) — **40 rows are unaccounted for**, despite `history_cap=2000` being
nowhere near exceeded (`_enforce_history_cap` never had a reason to evict
anything at 93 lines). `screen.retired_total` stays 0 throughout (matching
the live session's near-zero count) — confirms this is entirely the
full-rebuild path (`ghostty_engine.py:606-608,623`), which faithfully
mirrors whatever `terminal_grid_ref` reports at sync time. If native's own
scrollback already evicted TURN-00 internally before Python ever asks, the
rebuild is just reporting reality accurately — meaning the actual loss
happens **inside libghostty-vt itself**, either not honoring the
`max_scrollback` passed to `terminal_new` (`ghostty_engine.py:84-90`),
or hitting an internal limit when a single `terminal_vt_write` call
contains a very large paste-style dump (the seed dump is ~87 lines onto a
24-row screen; the first post-seed dump is ~116 lines in one call).

**Next step:** vary one variable at a time to isolate the trigger — (a)
confirm/deny `max_scrollback` is honored by checking native scrollback
capacity directly (if the API exposes it) rather than inferring from
Python-side symptoms, (b) shrink `_LINES_PER_TURN` to see if a
smaller single-dump byte volume avoids the loss (byte-volume-triggered
vs. row-count-triggered), (c) try feeding the seed and first growth turn
as SEPARATE smaller `feed()` calls instead of one another to see if
splitting the write avoids it. This is a different, likely more consequential
finding than the original character-splice symptom — a full-replay TUI
(Codex-style) with a long conversation could silently drop entire early
turns from scrollback/logging, not just show a cosmetic glitch. Worth its
own priority-1 slot alongside the splice, not folded into item 6's
audit as a footnote.

**BOTH bugs root-caused, 2026-08-21 — this is no longer speculative.**
Isolated the mechanism for both symptoms by querying native rows directly
via `terminal_grid_ref`/`_cell_from_grid_ref`, bypassing `_sync_scrollback`
entirely, immediately after the seed(3)+turn(3) repro above.

**Content-loss bug — plain Python logic bug, easy fix, NOT native.**
Direct query of native rows y=0-9 (right after the second feed, when
Python-side `screen.history` was already missing TURN-00) still returned
TURN-00's exact correct text. The native library never lost it. The loss
is entirely inside `_sync_scrollback`'s `self._replace_scroll` branch
(`ghostty_engine.py:590-601`, the "home+2026 dump" reconstruction path,
which fires because this replay pattern IS exactly a home+sync dump):

    if self._replace_origin is None:
        self._replace_origin = last          # e.g. 64, the row count before this dump
    origin = self._replace_origin
    ...
    s.history.clear()                        # wipes rows [0, origin) too
    start = origin                            # ...but only rebuilds [origin, scrollback_rows)
    notify = False

`s.history.clear()` discards everything, including the already-correct
rows `[0, origin)`, but the rebuild loop right after only re-fetches
`[origin, scrollback_rows)` — rows before `origin` are never repopulated.

**Attempted fix, 2026-08-22 — WRONG, reverted, do not repeat this exact
approach.** Tried "truncate `s.history` back to exactly `origin` entries
instead of clearing, then rebuild `[origin, scrollback_rows)`" (replacing
`s.history.clear()` with `while len(s.history) > origin: s.history.pop()`).
This DID fix the content-loss repro (verified: TURN-00 present, zero
missing lines) — but was verified against a home-grown check that only
tested set-membership ("is this line present somewhere"), not duplicate
counts. Running the full existing test suite (should have been step one,
not an afterthought) immediately showed the real cost:
`HomeReplaceScrollTests::test_2026_home_dump_does_not_duplicate_*` and
`test_fifty_testing_agent_dumps_keep_one_copy` all broke (e.g. `LINE-00`
now appears twice instead of once). Reverted via `git checkout --`; all
415 tests pass again; **no lasting harm, but no fix landed either.**

**Why "just don't clear" is wrong, understood only after the revert:** a
home+dump doesn't just add new scrollback rows — CSI H repositions the
cursor into the CURRENTLY VISIBLE grid and overwrites it in place. Only
once that overwrite exceeds one screen height does genuine scrolling
resume, and what scrolls off at that point is the OVERWRITTEN (new) text,
not what used to be there. When a dump re-sends content whose early lines
match what's already sitting in the *tail* of `s.history` from a prior
scroll (the common case for any repeated full-transcript replay, e.g.
Codex-style, once the transcript has grown past one screen), this
overwrite-then-rescroll genuinely creates a second, distinct native
scrollback row with duplicate text — not a Python bug, correct raw
terminal behavior. `s.history.clear()` was doing real, necessary
deduplication work for that case, not just being lazy. The `[0, origin)`
prefix that WAS safe to preserve in the content-loss repro (a 24-row
screen with a much larger, still-growing transcript) is NOT safe to
preserve in general — the actual boundary is closer to "the last
`screen.rows` or so lines of the prior state, which is what gets
overwritten-and-possibly-rescrolled by any home+dump," not the whole
`[0, origin)` prefix.

**FIXED, 2026-08-22 — landed by Grok Build in a separate isolated tab,
handed the task with full context (this section's own analysis, the exact
repro, and the requirement to verify against the full existing test suite
before claiming done). Independently re-verified, not just trusted.**

Grok's diagnosis went further than the analysis above: it found that a
naive "keep `[0, origin)` verbatim" ALSO breaks on the pre-existing
`test_2026_home_dump_does_not_duplicate_transcript_in_history`-style tests
once the screen is narrow enough that lines wrap mid-word (a 20-column
screen splits `LINE-02` across two physical rows as `'LINE-0'` + `'2'`),
because a full-row-text prefix comparison can't line up correctly against
wrapped, spliced overflow text.

**The fix:** a new `merge_replace_scroll_history(old_rows, new_rows,
splice_window)` (`ghostty_engine.py`, top of file) that:
1. Aligns on the dump's *first* overflow line — finds the leftmost old row
   that matches it exactly or as a leftover-tail splice (`_rows_match`,
   `allow_splice=True`) — rather than assuming `origin` itself is a
   trustworthy boundary.
2. Keeps every old row before that alignment point (genuinely predates
   this replay).
3. Drops the reproduced suffix (this dump re-creating its own transcript).
4. Repairs the first `splice_window` (= `screen.rows`) overflow rows: if a
   new row is old text with a spliced-on tail, keeps the clean old copy
   instead.

`_sync_scrollback`'s replace-scroll branch now captures `replace_old =
list(s.history)` instead of calling `s.history.clear()` immediately,
builds `new_rows` from the native fetch as before, then does
`s.history.clear()` + re-populates via `merge_replace_scroll_history(...)`
once both sides are known.

**Verification (independently re-run, not just trusted from Grok's own
report):**
- `python -m pytest tests/ -q` → **423 passed** (up from 415 — 8 new
  tests: `ReplaceScrollMergeTests` unit tests for the merge helper itself,
  plus new `HomeReplaceScrollTests` cases for the growing-replay and
  wrapped-splice scenarios). Includes the originally-failing
  `tests/test_splatter_replay_rebuild.py` repro from this session, which
  now passes.
- Re-ran the exact original 8-turn content-loss repro script independently
  (not reusing Grok's own test code) with an explicit duplicate-count
  check this time (the gap that made my own reverted attempt look correct
  when it wasn't): `missing=0 dupes=0 total_lines=233`. Zero content loss,
  zero duplication, for the scenario that started this whole
  investigation.
- Confirmed via `git diff` that Grok did not touch `TODO.md` (as
  instructed) — the diff there is entirely this session's own prior
  writeup.

This closes the content-loss bug. The splice bug (the OTHER symptom found
this session, native-level character splicing under real threading) is
separate and remains open — see that section above; it was not part of
Grok's task and this fix does not address it (though the merge logic's
own splice-repair step is a related but different mechanism: it corrects
a *known, structural* overwrite-splice this dump's own overflow produces,
not the unexplained live splice from real concurrent PTY timing).

**Splice bug — confirmed real and native-level, reproduced deterministically,
independent of any Python-side logic.** The same direct native query (no
Python history/branch logic involved at all) returned, for row 64 right
after the second feed:

    64: '› user prompt TURN-00ent reply line 2005'

The real content at row 64 should be `• TURN-02 L05 mock agent reply line
2005` (confirmed from rows 60-63's correct sequential content). What came
back instead is row 0's ENTIRE real text (`› user prompt TURN-00`, 22
chars — a long-since-scrolled-away line from the very first frame)
prepended directly onto the TAIL of row 64's real text, with the middle
of row 64's own text missing entirely. This is a buffer-length/offset
error signature (a partial copy from one location followed by a copy
resuming at the wrong offset in another), inside the ctypes call chain
`self._g.terminal_grid_ref`/`_cell_from_grid_ref` (`ghostty_engine.py:
613-619`) or libghostty-vt itself — not this plugin's Python code, which
was proven uninvolved by querying around it entirely.

**Reproduction is now fully deterministic and scriptable** (seed 3 turns,
add 1 more turn, both via the exact `ReplayAgent`/`encode_replay_frame`
byte pattern, then walk `terminal_grid_ref` for y=0..scrollback_rows-1
directly) — this closes out the open question in priority item 6 below:
the isolated stress test (concurrency-shaped) found nothing, but THIS
shape (large single-write home+dump replays) reliably reproduces both the
content-loss and the splice. Next session: (1) apply the one-line content-
loss fix and verify with a regression test, (2) narrow the splice further
by bisecting frame size / row count to find the exact threshold that
triggers the buffer error, then either work around it in
`_sync_scrollback` (re-validate/re-fetch a row before trusting it, if a
cheap sanity check exists) or file it upstream against libghostty-vt with
this exact repro script attached.

Repro was found in the isolated "Testing Agent" tab (see method note
above) — zero risk to any real conversation, safe to keep iterating
there.

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

**Correction, same session, 2026-08-21 — checked against actual Ghostty
source (`~/tools/ghostty`), not just inference. The "file upstream" framing
above was too strong; revising it.** Verified our ctypes bindings
(`GhosttyGridRef`, `GhosttyPoint`/`GhosttyPointCoordinate`/`_GhosttyPointValue`
in `ghostty_vt.py`) match the real C struct layouts in
`include/ghostty/vt/grid_ref.h` and `point.h` field-for-field, including
padding — not a struct-alignment/ABI bug on our side. `POINT_TAG_SCREEN`
("full screen including scrollback") is also the semantically correct tag
for what `_sync_scrollback` is doing.

Traced `ghostty_terminal_grid_ref` into the actual Zig source
(`src/terminal/c/terminal.zig:810-821`): it's a thin wrapper around
`t.screens.active.pages.pin(zig_pt)` — Ghostty's core page-list pin
mechanism, used throughout the whole terminal (selection, mouse hover,
tracked refs), not some obscure corner. A fundamental bug there is
genuinely unlikely given how battle-tested it is, which is a fair
objection to "Ghostty has a bug" on its face.

**What's much more likely, and still worth pursuing:** `grid_ref.h`'s own
docs say plainly: "the grid reference APIs are not meant to be used as the
core of a render loop. They are not built to sustain the framerates needed
for rendering large screens. Use the render state API for that." (This
plugin already does use the render_state API — `_cell_from_render_cells`
— for the *active* grid; `_cell_from_grid_ref`/`grid_ref` is used
specifically for scrollback/history rebuilds.) `_sync_scrollback` calls
`grid_ref` in a tight loop across potentially hundreds of rows on **every
single replay-dump** for a Codex-style full-transcript-replay TUI — a
genuinely unusual, heavy-duty bulk-query pattern most Ghostty consumers
(mouse hover, one-off selection lookups) never stress this hard. The
untracked-ref lifetime contract ("valid only until the next mutating
terminal call... snapshot immediately") is also worth re-checking against
whether repeated `pin()` calls for a large scrollback range could
themselves trigger page-list maintenance (allocation, defragmentation)
that the docs might not fully spell out as "mutating" from the caller's
perspective.

**DECISIVE, 2026-08-21 — the minimal C repro was done, and it clears
Ghostty entirely.** Built `repro.c` (scratch, not committed) against
`~/tools/ghostty`'s local build via `zig cc`, linking directly against
`zig-out/lib/ghostty-vt.lib` — confirmed byte-identical to GhostShell's
vendored `ai/terminal/bin/ghostty-vt.dll` (matching SHA256). Zero Python,
zero ctypes: plain C calling `ghostty_terminal_vt_write` /
`ghostty_terminal_grid_ref` / `ghostty_grid_ref_graphemes` directly,
replicating the exact seed-3-turns-then-add-1-turn home+dump pattern and
walking every row via `POINT_TAG_SCREEN`. Result:

    cols=80 rows=24 scrollback_extent(max_y)=181
    TURN-00 content found anywhere in scrollback: YES
    total splices found: 0

**Neither the content loss nor the splice reproduces in pure C against the
real library.** This proves both bugs are entirely within GhostShell's own
Python/ctypes usage — not Ghostty, not libghostty-vt, full stop. The
content-loss bug was already independently root-caused as a pure Python
logic bug (the `s.history.clear()` + `start=origin` bug in
`_sync_scrollback`'s replace_scroll branch, see above) — this C repro is
additional confirmation, not new information, for that one. The splice is
the one still needing work: it was observed via a hand-rolled manual
Python walk script (not committed, passed `palette=None` and skipped
style resolution) that differs from `_cell_from_grid_ref`'s real code path
(which calls `grid_ref_style` before `grid_ref_graphemes`, then resolves
colors against a real palette) — that reimplementation gap is now closed:
re-ran the identical seed(3)+turn(3) scenario calling the REAL
`_cell_from_grid_ref` (real palette, real `grid_ref_style` call included)
directly against `parser._term`. **Also zero splices.** So the splice has
now failed to reproduce in THREE separate careful, controlled attempts:
pure C, Python via the real function with 1 addition, and the earlier
`test_splatter_replay_rebuild.py` pytest run with 5 sequential additions
(that one only ever found the content-loss mismatch, never a splice).

**Where this leaves the splice bug, honestly:** the ORIGINAL live
observation is still a confirmed real event, not dismissed — it was read
directly out of `term.screen.history[30]` in the actual running plugin
process, not inferred from a rendering artifact. But its trigger has not
been isolated: my first ad-hoc manual-walk script (the one that DID show a
splice) skipped `grid_ref_style`/palette resolution, so it can no longer
be trusted as clean evidence of a specific mechanism — it may have had its
own bug, separate from the real splice. **What's left as the most likely
remaining explanation:** the live incident happened under REAL threading
(the actual PTY reader thread feeding real conpty-scheduled output over
real wall-clock time, at real terminal dimensions ~107x47, via 1+5
separate `_on_data` calls arriving close together) — none of which any
single-threaded, synchronous-script repro (C or Python) has replicated.
**Next step, if pursued further:** don't try to force this via another
isolated script — go back to the isolated "Testing Agent" tab (method
established above) under the real plugin's real threading, and watch for
recurrence directly, since that's the one condition common to the only
confirmed sighting and absent from every attempt that failed to
reproduce it.

### Still open, in priority order

1. ~~Restart Sublime.~~ **DONE 2026-08-21.**
2. ~~Re-run the multi-line-prompt cursor test post-restart.~~ **DONE
   2026-08-21, PASSED** — see item 1 above ("Restart-verified"). Gate state
   unchanged (`caret_footer_pinning_enabled: true`, rest `false`); leaked
   stress-test threads reaped by the restart.
3. ~~If pursuing the splatter bug further~~ **PARTIAL, 2026-08-21.** Wrote
   `tests/test_splatter_stress.py`: isolated subprocess (not injected via
   `eval_python`), replicates the real production lock pattern (one writer
   thread `parser.feed()`, one reader thread `screen.render_cells()`, both
   under one shared lock, matching `term._lock` in `ai_terminal.py`'s
   `_on_data`/render path exactly). Run via
   `python -m pytest tests/test_splatter_stress.py -v -s` with the
   process's own 90s watchdog (`os._exit`) as a second safety net beyond
   pytest's own runner, since a hang under load was the prior lead.
   **Result: PASSED.** 10s run, 2278 feed iterations / 6 render iterations
   (writer starves reader under a tight loop + shared lock — expected, not
   itself a bug), zero splatter, zero hang, both threads exited cleanly.
   **This is a negative result under one specific stress shape, not proof
   the bug is fixed or Python-level.** It rules out plain lock contention
   causing a hang, and this exact tight feed/render interleave causing
   splatter. It does NOT rule out: bursty/paced writes closer to real PTY
   timing, a third thread (recorder/logger also reading `screen.` state),
   or corruption specifically inside libghostty-vt's ctypes boundary under
   different timing than this test exercises. If the bug resurfaces live,
   next step is widening this same isolated-subprocess test to match the
   actual burst pattern from a `.cast` capture of the incident, not
   auditing ctypes calls in the abstract.
4. ~~`trim_display_rows` is not gated~~ **RESOLVED, 2026-08-21 — no new
   gate needed.** Checked the actual call site (`ai_terminal.py:3382-3396`):
   `_trim_display_rows(rows, cy)` is called with the *same* `cy` that
   `caret_footer_pinning_enabled` already governs one line above it (line
   3386-3387, `_adjust_display_caret`). There is no code path where
   `trim_display_rows` sees an independent cursor value — it is already
   fully downstream of the existing gate. A dedicated 6th toggle would
   duplicate `caret_footer_pinning_enabled` with zero added bisection
   power. Leave as-is; the settings-file comment on
   `caret_footer_pinning_enabled` documenting the dependency is sufficient.
5. ~~Complete the actual bisection~~ **DONE, 2026-08-21 — via a new
   mechanism, safely.** The real blocker on every prior attempt was that
   these five settings are global — flipping them off for testing risked
   the live conversation tab itself (confirmed: doing this live, briefly,
   dropped `caret_footer_pinning_enabled` under Tab 1 and its
   `screen.history` immediately hit the 300-line cap — reverted within
   seconds, no lasting damage, but real risk). Fixed properly instead of
   worked around: all five `_setting_bool(...)` call sites
   (`ai_terminal.py:3386,3404,3440,4333,6092`) now pass
   `profile_name=_term_profile_name(term)` — `_setting_bool` already
   supported profile overrides (used elsewhere, e.g.
   `osc_title_updates_tab`), these five just weren't wired to use it.
   Verified live via `importlib.reload()`: both `_Terminal` instances
   (Claude tab, Testing Agent tab) survived the reload with PTYs intact,
   and the same setting now resolves differently per tab
   (`caret_footer_pinning_enabled` → `True` for Claude, `False` for
   Testing Agent). Added a per-profile override block to the `"Testing
   Agent"` profile in `ai_terminal.sublime-settings` (all five `false`) —
   this profile is now a permanent, safe, isolated Terminus-baseline
   control tab; the global values (whatever they are for real agent
   profiles) are never touched by testing there again.

   **Bisection result, all five false, driven via the isolated tab:** fed
   5 more replay turns (`mock_agent_cli.py`, matching the splatter
   investigation's method) under the all-off baseline. `nonblank grid
   rows` held exactly steady (46 before, 46 after) while history grew
   normally — **the documented "300 lines missing, ~22-26 lines visible"
   incident (item 2 above) did NOT reproduce** with this mock agent. Read
   as: that incident is likely specific to real Claude Code's exact
   footer/cursor-parking byte pattern, not a universal hazard of the
   Terminus baseline for any TUI — worth confirming next session by
   running an actual `Claude` profile through the SAME isolated-tab
   all-off test (still zero risk, since it'd be a throwaway tab, not the
   conversation's own tab) before concluding the two-incidents caution
   from item 2 needs to stay a blanket "leave these two enabled forever."
6. **Audit `ghostty_vt.py`/`ghostty_engine.py`'s ctypes calls** for
   thread-safety, if (3)'s isolated stress test reproduces the splatter and
   points at the native boundary rather than Python-level logic. **Update,
   2026-08-21: partially superseded** — a pure-C repro against the same
   DLL (see item 3's "DECISIVE" update above) already cleared Ghostty/
   libghostty-vt itself for both the content-loss and splice bugs found
   this session. This audit item is really about GhostShell's OWN ctypes
   call sequencing now (GIL/threading across `_sync_scrollback`'s bulk
   `grid_ref` loop), not the native library.

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

4-digit gutter reserve done — see "RESOLVED (2026-08-27)" above (turned
out to need the scrollback-cap's actual digit width, not a bare 4). The
replace_scroll heuristic stays unstarted.

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
