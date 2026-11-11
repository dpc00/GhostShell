# ai_terminal queue freeze — 2026-11-04 (imported from omp's memory bank 2026-11-11)

Dated record from omp's mnemopi bank; superseded facts (release state) are marked, invariants and
diagnostics are current.

## The freeze and the fix (HISTORY: fixed in 0.2.18's lineage, commit 80ab426)

`ai_terminal.py`'s async PTY writer ran `await queue.Queue.get()` on the plugin's asyncio loop,
which is stepped on Sublime's main thread. `_Terminal.__init__` creates a blocking `queue.Queue`;
if `_ensure_writer` fails to replace it with `_AioQueue` for an async (broker/omp) PTY, the plugin
host blocks forever and Sublime goes "Not Responding" within a second of starting — unfixable
without restarting.

- **Fix (80ab426):** the queue type must match the writer; `_aio_write_loop` refuses a non-async
  queue. The invariant stands even though release versions moved on: any writer that awaits must
  own an async queue, checked at start, never silently tolerated.
- **Diagnostic that worked:** `py-spy dump --pid <plugin_host-3.14.exe>` — the main thread showed
  `_aio_write_loop -> queue.Queue.get -> threading.wait`. py-spy is pip-installed at
  `~/AppData/Roaming/Python/Python314/Scripts/py-spy.exe`.

## Session-kill rule (still in force)

Never kill a GhostShell broker session by killing Sublime Text, and never kill a session without
being told to — the owner's rule.

## Known issue referenced the same day

Windows Terminal detach of an omp tab loses colours and line-drawing: `tools/recover_console.py`
routes TSP sessions through `_TspRelay`, which draws the surface as plain text with cursor escapes
and no SGR (documented in `docs/RELEASE_NOTES.md`).
