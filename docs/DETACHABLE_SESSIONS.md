# Detachable agent sessions

GhostShell keeps terminal sessions alive independently of Sublime Text. This
is on by default (top-level `"detachable": true` in
`ai_terminal.sublime-settings`) for every profile, plain shells included, not
just long-running agents -- a profile can set `"detachable": false` to opt a
specific one out (e.g. a throwaway shell not worth a background broker
process for).

If Sublime Text closes or its terminal view becomes unusable, the child
process and its ConPTY can remain alive while a later GhostShell client
reconnects to them.

## Components

- `_BrokerPty` in `ai_terminal.py` is the Sublime-side client. It connects to
  three local named pipes for output, input, and control messages.
- `tools/agent_broker.py` owns the ConPTY and child agent process. It accepts
  one client at a time and retains a bounded raw-output replay buffer.
- `tools/spawn_outside_job.ps1` starts the broker independently of Sublime's
  process job. It uses a short-lived interactive Windows Scheduled Task.
- A JSON record under the broker registry directory identifies each live
  broker by pipe name, PID, profile, working directory, and child command.

The temporary scheduled task is only a launch trampoline. Broker options,
child arguments, and the environment are written to a randomized, one-use
`.launch` file. The broker consumes and deletes that file, after which the
launcher unregisters the task. The continuing broker normally runs through
`pythonw.exe`, so it does not create a DOS console window.

## Lifecycle

1. GhostShell creates a unique pipe name and one-use launch file.
2. The scheduled-task launcher starts the windowless broker and waits until
   the broker consumes the launch file.
3. The broker creates the ConPTY, starts the requested agent, publishes its
   registry record, and serves its named pipes.
4. GhostShell connects and renders output normally.
5. If the client disappears, the broker keeps the ConPTY and agent alive and
   makes fresh pipe instances available.
6. A new client reconnects, receives retained raw output followed by a replay
   boundary marker, reconstructs the terminal screen, and resumes live I/O.
7. An explicit session end sends `KILL` over the control pipe. The broker then
   stops the child and removes its registry record.

The broker replay buffer is recovery context, not a full transcript. Its
default is 2 MiB and it is clamped to 1–256 MiB. Change
`broker_scrollback_bytes` globally or on an individual profile if necessary.

## User recovery commands

`Ai Terminal: Recover Session...` is the one recovery command -- it used to
be two (Revive Frozen Tab / Recover Orphaned Session), which required
knowing which applied: whether the disconnected tab was still open (just
frozen) or already gone. That distinction is plumbing, not something a user
should have to reason about, so the command makes it itself: if the
*focused* tab is itself a frozen detachable session (`_is_broker_pty(term.pty)
and not term.pty.is_alive()`), it revives that tab in place -- same as the
old Revive Frozen Tab. Otherwise it searches the on-disk broker registry,
known terminal objects, and as a fallback broker process command lines, then
offers live sessions not already attached to a usable tab -- same as the old
Recover Orphaned Session. Production brokers start with `--launch-file` and
do not put `--pipe-name` on the process command line, so the registry is the
source of truth for that half.

**A tab counting as "usable" only checks the Sublime view, not the pty
underneath it** -- confirmed live 2026-09-02: after a WT hand-off, the
handed-off tab (open, but frozen -- its pty is dead) still counts as a
"usable view" to the orphaned-broker search, so that broker never appeared
in the list at all. Running Recover Session with that exact tab focused is
what actually works in that situation (the command's own "is *this* tab a
frozen match" check runs first and doesn't care whether the view still
counts as "usable" elsewhere); the list is reached only once the tab itself
is gone.

A registry record is only trusted after `_broker_process_matches()`
(`ai_terminal.py`, mirrored in `tools/recover_console.py`) confirms the
recorded PID is not just alive but plausibly *that* broker: its image name
must be `python.exe`/`pythonw.exe` and its actual process start time
(`GetProcessTimes`) must sit within ~5 minutes of the record's `created_at`.
A bare PID-alive check is not enough -- Windows recycles PIDs, and a broker
that crashed or was killed without reaching its own registry cleanup leaves
a stale record behind indefinitely; days later an unrelated process can land
on that same PID and would otherwise look "live" forever, hanging recovery
for ~10s before it fails against a pipe that no longer exists.

Windows Terminal cannot host the already-running agent process directly. A
child like Grok or Claude is bound to the broker's ConPTY at `CreateProcess`
time (`PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE`); WT always creates its own
ConPTY for whatever it launches. There is no public API to hand an existing
HPCON to a WT tab (`wt claude` is always a new, unrelated process).

`Ai Terminal: Open in Windows Terminal` gets you the equivalent result a
different way: it launches `wt.exe` running `tools/recover_console.py`
against the current tab's broker pipes -- a raw named-pipe relay, not a new
agent process, so it's the same live session, same conversation state,
nothing restarted. The broker accepts only one connected client at a time,
so this is a *handoff*, not a second view: the Sublime tab detaches (the
dialog says so before it runs) the moment WT's relay connects.

Detaching closes this tab's own read handle via `CancelIoEx`, which the
reader thread cannot otherwise tell apart from the child actually dying --
left alone (this really happened, live, before it was fixed) that made the
tab auto-close ~1.5s later as a false "process exited", *and* that close
sent a real `KILL` to the still-live broker WT was attached to, ending the
very session just handed off; it only survived by luck (a pipe WT itself
happened to be holding blocked the KILL). `_Terminal
._expected_termination_reason`, set to `"handoff"` immediately before the
detaching `kill()` call, is what tells both the reader thread (skip the
false-exit auto-close reasoning entirely -- the tab now stays open, showing
`[detached -- session handed off, still running]`) and `on_close` (skip
`KILL`; nothing local is ending) that this is a hand-off, not a death. The
tab is left in the same state as a frozen one -- `Ai Terminal: Recover
Session...`, run with that same tab focused, reconnects it in place; if the
tab itself gets closed by hand afterward, the same command's list-based
fallback finds it too. The session otherwise lives on purely in WT (and
the broker) until you close the WT window. Sublime -> WT -> Sublime,
same broker PID throughout, was proven live on 2026-09-02 -- against the
version of this fix that (incorrectly, since corrected) auto-closed the
handed-off tab rather than freezing it; the "tab stays open" behavior
itself is source-verified (`tests/test_broker_recovery.py`) but not yet
re-confirmed live since.

Deliberate control over "kill vs. detach" x "keep tab vs. lose tab" is also
available directly, grouped under the "Ai Terminal: Session" submenu in
Tab Context.sublime-menu, not just as a side effect of handing off to WT.
Of the four combinations, one (neither kill nor close) needs no command --
that's just not clicking anything -- so the submenu has the other three
plus a read-only fourth:

- `Kill Session (Keep Tab Open)` ends the agent for real but leaves the tab
  (and its transcript) open -- for when the process is done but the
  transcript is still wanted, to read, copy, or Save As, before closing the
  tab yourself.
- `Close Tab (Keep Session Alive)` is the mirror image: closes the tab,
  agent keeps running in the background, recoverable later the same way a
  WT hand-off is.
- `End Session (Kill + Close)` does both together, in one action -- the
  same combination a plain tab close already does, just explicit and
  reachable without needing to physically close the tab.
- `Session Info...` is non-destructive: opens a read-only scratch tab (not
  `sublime.message_dialog` -- confirmed live 2026-09-02 that its fixed
  small system font made a multi-line technical readout hard to read) with
  everything known about the session. Human-relevant facts lead: profile,
  running/frozen status, last output (`_human_ago()`, updated on every
  `_on_data` call -- absolute start time alone doesn't answer "is this
  still doing something"), working directory, child command. Pipe name and
  broker PID are demoted to a "Details" footer -- also 2026-09-02 feedback,
  the original ordering led with exactly the two fields least useful for
  deciding what to do with a tab. Child command, broker PID, and start time
  are read from the on-disk registry record via
  `_read_broker_registry_record()`, so they're absent if the broker already
  exited and removed its own record. The Details footer also carries
  Sublime's own view/sheet/window identifiers (`_sublime_view_info_lines()`
  -- requested directly, best-effort per field since e.g. `view.sheet()`
  isn't guaranteed for every view kind): View ID, Buffer ID, Sheet ID,
  Window ID, and Group/Index.

Kill Session, Close Tab, and End Session all share the same
`_expected_termination_reason` mechanism (`"killed"` / `"closed"`), just
with different follow-through than the WT command's `"handoff"`.

## Resize does not reflow scrollback -- `Rewrap Full Conversation`

Resizing a terminal tab never fixes already-mismatched scrollback wrapping.
`Screen.resize()` (`terminal/screen.py`) only clips characters off history
rows when narrowing and does nothing to history when widening -- there is
no "these N terminal rows were originally one logical line" bookkeeping for
a real reflow to work from, so old rows just keep whatever wrapping the
child app originally drew them at. Confirmed live 2026-09-02: split-view
then resize left visibly mismatched scrollback ("newspaper column width"
remnants next to correctly-wrapped recent output), reproduced deliberately
by resizing narrow then wide and diffing the same historical region across
both captures.

Reconnecting (`_reattach_broker_view`'s default bootstrap mode) does not
fix this either, and deliberately so: `GhosttyParser.feed_bootstrap`'s own
docstring says a restored view "already owns readable historical text" and
skips reprocessing it, advancing only the native VT engine during replay.
That is the real ~10s -> ~1s speed win a past session made to detachable
reconnect -- but the corollary, confirmed by reading the code (not
recovered from any surviving comment -- nothing in the current codebase
documented the tradeoff this explicitly before now), is that a session
which has ever been reconnected can never have its old scrollback
correctly rewrapped by an ordinary reconnect again, because bootstrap mode
never reprocesses it.

`Ai Terminal: Rewrap Full Conversation (slow)` is the deliberate, on-demand
override: detaches the tab (`pty.kill()`, agent untouched, same guarantee
as every other command here), clears the view's text, and reconnects with
`_reattach_broker_view(view, pipe_name, force_full_replay=True)`. That
flag flips `_reattach_bootstrap` off and forces `restored_text = ""`
regardless of what was in the view, so the broker's retained replay buffer
gets fed through the real parser (`_on_data`/`parser.feed`, not
`feed_bootstrap`) at the view's *current* size, rebuilding `Screen.history`
from scratch -- the same real derivation a first-ever connection gets.
Correct (the child's own redraw, replayed fresh, comes out wrapped for the
current width throughout), but reprocesses everything the broker retained,
so it is slow for a large session -- this is why it is an explicit command
and not what an ordinary resize does.

**First live run killed the session and closed the tab instead.** The
detaching `pty.kill()` left `term._expected_termination_reason` at its
default `None` -- the exact bug this same mechanism exists to prevent,
reintroduced here by forgetting to set it. The old reader thread's
now-unguarded `_maybe_close_dead_view` fired ~1.5s later and closed the
view; by then the new connection had already registered a fresh
`_Terminal` against that same view id (a fast reconnect, or a slow one on
a small session), so `on_close` found that live, legitimate replacement --
whose own reason was also `None` -- and sent *it* a real `KILL`. A stale
timer from the detach reached across and killed the brand new session
that had already replaced it. Fixed: `term._expected_termination_reason =
"rewrap"` set before `pty.kill()`, same as every other detach here; a new
`"rewrap"` branch in `_read_loop` also skips the notice write entirely
(the view is about to be cleared and reconnected in place, so a notice
would just race the new connection's own writes). Not yet re-verified
live.

`tools/recover_console.py` also still works run by hand, as an emergency VT
relay for when Sublime itself is unusable (crashed, frozen UI, etc.) and the
in-app command isn't reachable: close the Sublime *window* (do not close the
tab) so the broker detaches, then run
`python tools/recover_console.py --pipe-name <name>` (or `--list` to find
it). This is the same relay the in-app command launches through WT.

A deliberate single-tab close ends that tab's underlying detachable session
-- unless something already told it not to (a WT hand-off, Kill Session, or
Close Tab (Keep Session Alive), all above). Closing a Sublime window is
treated differently: GhostShell detaches its local client so restored tabs
can reconnect later.

Recovery is not persistence across Windows restart, sign-out, child-agent
exit, or an explicit session end. A broker can only restore output still held
within its bounded replay buffer.

## Live verification

On 2026-08-30, a full Sublime Text restart preserved and reattached both an
active Codex session and a PowerShell session. Both tabs were responsive
immediately after restoration. This verifies the production restart path in
addition to the isolated Scheduled Task integration test below.

On 2026-09-02, `Ai Terminal: Open in Windows Terminal` was run against a live
Claude session: Sublime's tab detached, a real `wt.exe` window rendered the
relayed session cleanly (screenshot-verified, no corruption), and closing
that window followed by reattaching (then still the two-command split)
reattached it to a Sublime tab -- same broker PID throughout, confirmed via
the lifecycle log. The same day, a second live round trip against a real
Grok session (`Open in Windows Terminal` -> close WT -> reattach) is what
surfaced the "usable view doesn't check pty liveness" gap: Recover Orphaned
Session's list never showed the Grok broker at all, because the frozen tab
it left behind still counted as "usable"; focusing that tab and running
Revive Frozen Tab worked. `Recover Session...` (this section, above) merges
both into the one command that tries that first -- built same day, not yet
re-confirmed live under its new name.

## Automated verification

The normal suite does not create scheduled tasks:

```powershell
python -m pytest tests -q
```

The live integration test creates a real temporary Scheduled Task, broker,
ConPTY, `cmd.exe` child, registry record, and named pipes. It verifies:

- the launch file and temporary task disappear;
- the broker PID remains unchanged across reconnects;
- shell state survives each client replacement;
- replay completes for every new client;
- abrupt death of a separate client process releases all pipe endpoints;
- a new client recovers after that simulated editor crash; and
- explicit shutdown removes the broker registry record.

Run the default 10-cycle live test from PowerShell:

```powershell
$env:GHOSTSHELL_RUN_SCHEDULED_BROKER_TEST = "1"
python -m pytest tests/test_scheduled_broker_integration.py -v -s
```

Run a longer soak by setting the cycle count:

```powershell
$env:GHOSTSHELL_RUN_SCHEDULED_BROKER_TEST = "1"
$env:GHOSTSHELL_BROKER_TEST_CYCLES = "100"
python -m pytest tests/test_scheduled_broker_integration.py -q
```

The live test is Windows-only and opt-in because it mutates Task Scheduler
briefly. Its `finally` cleanup explicitly ends the test broker even after an
assertion failure. Broker lifecycle diagnostics are appended to
`~/data/logs/ai_terminal/agent_broker.log`.
