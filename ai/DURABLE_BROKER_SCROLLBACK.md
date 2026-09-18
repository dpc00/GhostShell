# Durable broker scrollback — design (not yet implemented)

Status: design only. Do not edit `tools/agent_broker.py` or the reattach
path until this document is accepted and the first test in Phase 1 exists.

## Goal

After a Sublime Text restart, reconnecting to a still-running detachable
broker should reconstruct host scrollback from the **same unmodified PTY
bytes** the broker already tees to live clients — not from the restored
view's plain text. `_seed_restored_history` should become a last-resort
fallback (empty replay), not the common path.

This is a bounded reconstruction window, not a session archive. See
[ARCHITECTURAL_BOUNDARIES.md](ARCHITECTURAL_BOUNDARIES.md) §Supported
restart promise.

## Superlogical alignment

Superlogical's announced mux architecture (Hashimoto, launch-week replies;
summarized with quotes in
[termio-sh/termio research brief](https://github.com/termio-sh/termio/blob/main/docs/design/20260805-_research-superlogical-codex-brief.md),
2026-08-08 update):

- *"we take the PTY bytes, we tee them off to all the clients, and we send
  them raw like SSH"*
- Asked whether the server parses too: *"Yes, the server parses too. But
  the teeing happens ahead of the server."*
- On screen-diff transport: *"The issue with the screen diffing is less
  performance and more making it very difficult to allow native
  scrollback, selection."*

Company thesis ([superlogical.com](https://www.superlogical.com/)): a
durable session you can close and reconnect to, with native-feeling
scrollback rather than tmux-style nested parse/regenerate.

GhostShell already tees. `_OutputServer.feed` appends raw ConPTY bytes to
`_Scrollback` and writes those same bytes to the connected client. The
broker does not parse or regenerate VT. Parsing happens on the client in
libghostty-vt (`GhosttyParser.feed` / `feed_bootstrap`). That matches
"tee ahead of parse." Superlogical also parses on the server, in
parallel; GhostShell does not need a second parser in the broker.

## Current path (evidence)

Live reconnect while Sublime stays up:

1. `_OutputServer.run_forever` (`tools/agent_broker.py`) takes
   `_client_lock`, writes `_scrollback.snapshot()` then `_REPLAY_END`
   (`b"\x1b]777;GhostShellReplayEnd\x07"`), then publishes
   `_client_handle`.
2. `_BrokerPty.read` strips the OSC marker and calls
   `on_replay_complete`.
3. `_Terminal._on_data` feeds those bytes through the real ghostty
   parser. Mode state (DECSET, mouse tracking, alt-screen) is
   reconstructed because libghostty sees the original sequences.

ST-restart reconnect (`_reattach_broker_view`):

1. The **broker process is still alive**. Detachable sessions are
   launched outside Sublime's job (`docs/DETACHABLE_SESSIONS.md`). The
   in-memory ring therefore still exists. The client still receives
   `snapshot() + _REPLAY_END`.
2. `_reattach_broker_view` nevertheless copies `view.substr(...)` into
   `Screen.history` via `_seed_restored_history`.
3. `_reattach_bootstrap = True` makes `_on_data` call
   `GhosttyParser.feed_bootstrap` — native `terminal_vt_write` only, no
   Python cell sync. That is the documented ~10s → ~1s win
   (`5d7a88c`, restated in `DETACHABLE_SESSIONS.md`).
4. `finish_bootstrap` syncs the **active grid** and then sets
   `_last_scrollback_rows` to the native scrollback count **without**
   calling `_sync_scrollback`. Subsequent incremental syncs therefore
   skip importing the native history that replay just built.
5. `_on_broker_replay_complete` pops the seeded tail that overlaps the
   recovered grid and keeps the older plain-text rows.

So the ST-restart fidelity loss is not "the bytes were gone." It is
that bootstrap **refuses to materialize** native scrollback into
`Screen.history` and substitutes restored view text instead. That text
has no attributes, the wrong wrap, and a cap of
`history_cap + rows` painted lines rather than whatever the 2 MiB ring
still holds.

`_Scrollback` itself (`tools/agent_broker.py`):

```
append(data)   # extend bytearray; drop prefix past max_bytes
snapshot()     # copy of the bytearray
```

Default cap is `broker_scrollback_bytes` = 2 MiB, clamped 1–256 MiB
(`ai_terminal._broker_scrollback_bytes`). Independent of
`scrollback_history_size` (line cap on `Screen` / libghostty
`max_scrollback`).

## What disk durability does and does not buy

| Event | In-memory ring | On-disk mirror |
|---|---|---|
| Client disconnect, Sublime still up | Survives (broker lives) | Redundant |
| Sublime restart, broker still up | **Survives** | Redundant for replay |
| Broker process crash / kill | Child + ConPTY die with it. Disk is a dead transcript, not a reattach source. | Might be inspectable; cannot resurrect the session |
| Windows restart / logoff | Session is gone either way (`DETACHABLE_SESSIONS.md`) | Same |

**Do not implement disk as the ST-restart fix.** The bytes already
survive that restart. Implementing a rolling file and leaving
`_seed_restored_history` as the host-history source would not change
what the user sees.

Disk is still worth doing, later, as a **mirror of the same ring**:

- inspectability without attaching a client
- a crash-consistent copy next to the registry record
- option to rebuild `snapshot()` from the file if we ever drop the
  in-memory copy (not required now)

It must not become a second transcript format. Asciicast and the tab
text log already exist and answer different questions
(`PROFILE_AND_RECORDING_INVARIANTS.md`).

## Design

Two cooperating changes. Either can ship without the other. The
visible ST-restart fix is (A). The requested durability is (B).

### A. Reattach imports native scrollback (the fidelity fix)

Keep `feed_bootstrap` during the replay burst (no per-chunk Python
sync). Change the **boundary**:

1. `_reattach_broker_view` does **not** call `_seed_restored_history`
   when it is about to consume a broker replay (the common case).
2. `GhosttyParser.finish_bootstrap` syncs the grid **and** imports
   native scrollback: set `_last_scrollback_rows = -1` and call
   `_sync_scrollback()` (same full rebuild resize already uses).
3. `_on_broker_replay_complete` no longer pops seeded tail rows.
   `_restored_rows_seeded` goes away on this path.
4. Fallback: if replay is empty (legacy idle-boundary client, or a
   broker that connected before any output), `_seed_restored_history`
   may still seed from the restored view so a blank native terminal
   does not wipe readable text. Empty replay is the only remaining
   caller.

Speed: one native-history walk at the OSC boundary, not a `feed()` of
every chunk. That preserves the bootstrap win. `_sync_scrollback` is
FFI-heavy; cap is `scrollback_history_size` (default 300), already the
native `max_scrollback`. Do not raise that cap as part of this work.

`feed_bootstrap` stays. Its contract becomes: "advance native VT
during broker replay; `finish_bootstrap` publishes grid **and**
history." Update its docstring. The existing unit test
`test_broker_bootstrap_advances_native_terminal_without_sync` still
holds for `feed_bootstrap` itself.

Do not parse the replay a second time in Python. Do not splice view
text with native rows. Native ghostty after unmodified replay is
authoritative for both grid and history, matching the live `feed()`
path.

### B. Durable ring behind `_Scrollback` (the file)

Keep the class API:

```
_Scrollback(max_bytes, path=None)
append(data) -> None
snapshot() -> bytes
close() -> None          # flush + close; idempotent
```

`snapshot()` remains the attach hot path and stays a copy of the
in-memory window. The file is a write-through mirror, not a second
source the client reads.

**Location.** Same directory as the registry record:

```
<broker_registry_dir>/<pipe_name>.scrollback
```

Default dir is `%LOCALAPPDATA%\GhostShell\broker_sessions`. Pass the
path from `main()` next to `--registry-file`. Do not put it under the
package, the Sublime session, or `~/data/logs` (those are diagnostics
/ casts).

**Format.** Raw circular payload plus a small header, so overflow does
not rewrite 2 MiB on every chunk:

```
magic   4  b"GSB1"
max     4  uint32 LE, payload capacity in bytes
start   4  uint32 LE, index of oldest byte in payload
length  4  uint32 LE, valid bytes (0..max)
payload max bytes
```

`append` extends the in-memory `bytearray` (existing trim rule) and
writes the new bytes into the circular payload, then updates
`start`/`length` in the header. Do not `fsync` on every chunk — that
would stall the ConPTY reader. `fsync` on: client attach (before
`snapshot()` is sent), broker shutdown, and every N bytes (propose
256 KiB) or 1 s, whichever comes first.

**Lifecycle.**

- Broker start: create/truncate the file. A new broker is a new
  ConPTY; **never** load a leftover file into a new process's replay
  (that would mix a dead session's bytes into a live child).
- Broker clean exit: `_remove_registry` also unlinks the `.scrollback`
  file.
- Broker crash: leftover file may sit next to a stale `.json`.
  Recovery already refuses a registry record whose PID is not that
  broker. Unlink orphan `.scrollback` files when a stale registry
  record is discarded, or when a new broker is created with that pipe
  name (pipe names are unique per spawn).

**Cap.** Same `scrollback_bytes` already passed on the command line.
Do not invent a second setting. Do not tie the byte cap to
`scrollback_history_size`; redraw-heavy TUIs burn bytes without
producing history lines. The 2 MiB default stays the reconstruction
window.

**Who reads the file.** Only `_Scrollback` in the **same** broker
process, and tests. `_reattach_broker_view` does not open it.
`recover_console.py` does not open it. Clients still receive replay
over the named pipe.

## Non-goals

- Exact unlimited scrollback after Sublime exits.
- Loading a `.scrollback` file after the broker/child are dead and
  presenting it as a live session.
- Moving asciicast ownership into the broker (already called out as a
  separate decision in `PROFILE_AND_RECORDING_INVARIANTS.md`).
- Screen-diff or snapshot/diff wire protocol.
- Multi-client fan-out. The broker is still one client at a time.
- A second VT parser in `agent_broker.py`.
- Raising `scrollback_history_size` or `broker_scrollback_bytes`
  defaults.
- Sublime UI automation, extra windows, or OS-level input to verify.

## Phases (small, testable, in this order)

Do not edit the live broker until Phase 1 is green. Each phase is its
own commit.

### Phase 1 — `_Scrollback` tests, no behavior change

Extract nothing yet if a test can import the class. Prefer adding
`tests/test_broker_scrollback.py` that imports `_Scrollback` from
`tools/agent_broker.py` (or a tiny helper module if importing the
broker module is too side-effecty on non-Windows — today the module
`sys.exit`s when `os.name != "nt"` at import). If that import barrier
blocks unit tests on the design of the ring, split `_Scrollback` into
`tools/broker_scrollback.py` **as the first code change**, leaving
`agent_broker.py` as a one-line wrapper. That split is mechanical and
does not change bytes on the wire.

Tests that must fail on a plausible bug:

- append under cap → snapshot equals concatenation
- append over cap → snapshot is the trailing `max_bytes` (not a
  mis-trimmed prefix, not wrapped garbage)
- concurrent append vs snapshot does not release a torn buffer
  (lock is already there; pin it)
- empty snapshot is `b""`

No named pipes. No ConPTY.

### Phase 2 — client reattach uses native history

Files: `terminal/ghostty_engine.py`, `ai_terminal.py`,
`tests/test_ghostty_engine.py`, `tests/test_launcher_flow.py`.

- `finish_bootstrap` imports native scrollback (test: replay of a
  scroll-off line plus a grid line; after `finish_bootstrap`,
  `Screen.history` contains the scrolled-off line with cells from
  ghostty, not an empty history).
- `_reattach_broker_view` skips `_seed_restored_history` on the
  replay path.
- `_on_broker_replay_complete` no longer pops seeded rows.
- Keep `_seed_restored_history` and its two launcher-flow tests, but
  retarget them at the empty-replay fallback only. Delete
  `test_replay_boundary_replaces_only_restored_active_grid_tail` if
  that path is gone — do not re-pin it to the new text.

Update the `feed_bootstrap` / `_reattach_broker_view` docstrings so
they stop claiming the restored view is the history authority.

### Phase 3 — disk mirror

Files: `_Scrollback` (+ `tools/broker_scrollback.py` if split),
`tools/agent_broker.py` `main()` / `_remove_registry`,
`tests/test_broker_scrollback.py`.

- construct with a `path`; appends appear in the file
- process-crash simulation: close without `close()`, reopen the same
  path in a **test-only** loader and recover `snapshot()` bytes. This
  loader is not used by a new live broker (see Lifecycle).
- overflow wraps the payload; recovered snapshot still equals the
  in-memory trailing window
- `close()` / registry removal unlinks the file
- attach-time `fsync` happens before `snapshot()` is written to the
  client (assert via a fake file object in tests, not by reading
  disk durability of NTFS)

Wire `path` from `main()` only after the class tests are green.
`_OutputServer` keeps calling `append` / `snapshot`; it should not
learn about files.

### Phase 4 — docs

`docs/DETACHABLE_SESSIONS.md`: one short paragraph that ST-restart
replay now materializes native history from the teed bytes, that the
ring is optionally mirrored next to the registry, and that the file
is not a resume source after broker death. No new user setting.

## Verification

- `python -m pytest tests/ -q` after each phase. Do not treat the
  two already-failing
  `tests/test_launcher_flow.py` broker-reattach PID-stamp tests as
  part of this work; they are unrelated (`_stamp_broker_pid_when_known`
  vs `_maybe_reattach_broker` scheduling) and are not in the files
  this design touches except possibly `ai_terminal.py` in Phase 2 —
  do not "fix" them on the way through.
- Phase 2 needs a real DLL (existing ghostty engine tests skip
  without it).
- Live Sublime restart remains the stop-rule check in
  `ARCHITECTURAL_BOUNDARIES.md`: one newly created detachable session,
  usable screen, no corruption. If that fails, keep process
  reattachment and report partial history; do not add merge
  heuristics.
- No new Sublime windows, no computer-use, no OS-level input.

## Decision summary

1. Tee stays in the broker; parse stays in libghostty on the client.
2. ST-restart already receives the in-memory snapshot. Fix host
   history by importing native scrollback at `finish_bootstrap`.
3. Mirror that same ring to
   `<broker_registry_dir>/<pipe>.scrollback` as a circular file, after
   the client fix, without letting the client read it.
4. `_seed_restored_history` remains only for empty replay.
