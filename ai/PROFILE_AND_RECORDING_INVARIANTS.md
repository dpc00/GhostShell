# Profile and recording invariants

This is the short contract for changing GhostShell profiles or session
recording. Historical experiments belong in `TODO.md` or
`ai_terminal_notes.md`; operational rules belong here and in tests.

## Profiles are data

`terminal/profile_schema.py` defines the accepted profile keys and types.
Validation runs at plugin startup and after settings changes. An explicit
profile in `ai_terminal.sublime-settings` replaces a generated profile of the
same name; generated data never overwrites hand-maintained data.

Boolean and numeric settings resolve in this order:

1. Profile value, when present.
2. Global value in `ai_terminal.sublime-settings`.
3. The call site's documented default.

`launch_command` is a non-empty argument array or a platform-to-array object.
`spawn_env` contains strings only. Recording is GhostShell behavior, not child
process behavior: use `record_asciicast`, not `AI_TERMINAL_LOG_LINES`. The
environment variable remains a compatibility input for existing profiles.

## Recording ownership

For an ordinary terminal start, `_Terminal.prepare()` creates recording files
before the child starts so its first output cannot be missed.

For broker reattachment, the order is different and load-bearing:

1. Connect to the existing broker off Sublime's main thread.
2. Confirm that the restored view is still valid.
3. Win the terminal-registry race.
4. Call `prepare(reattach=True)`.
5. Start the reader and consume the broker replay.

Failed, canceled, or duplicate connection attempts must create no recording
files. A successful reattach creates a new correlated `*_reattach.cast` and
`*_reattach.log` segment. It does not append to the pre-restart segment.

## Cast validity

Cast names include microseconds so simultaneous sessions cannot truncate one
another. `CastRecorder.open()` does not publish its handle until the v3 header
has been written, flushed, and fsynced. A failed header write removes an empty
partial file. Once open, every event is serialized under the recorder lock.

The cast is the raw terminal event record. The `.log` is only the latest
rendered Sublime-tab snapshot; it is not an authoritative session transcript.

Continuous one-file recording across a Sublime restart requires moving cast
ownership into `tools/agent_broker.py`. Do not simulate continuity by appending
a reattached client's replay to the old client-owned cast: that duplicates the
broker's retained output and can still omit output beyond its replay limit.
