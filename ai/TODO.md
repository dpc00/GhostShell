# ai_terminal TODO

## One-line command-text jiggle while typing — LIVE VERIFIED FIXED (2026-08-30)

The latest Codex asciicast keeps the editable row fixed at terminal row 42
and parks the hardware cursor at row 45 after each character; it contains no
row insertion or resize matching the visible hop. GhostShell was nevertheless
calling `_scroll_to_bottom()` before every printable-key echo, even while
already following. That pre-echo viewport calculation could use the prior
Sublime layout height, then the render settled against the updated height one
line away. The key path now performs a viewport write only when returning from
scrollback. Full suite: 503 passed, 2 skipped. After a full Sublime Text
restart, the user resumed Codex and confirmed normal command-line typing has
no jiggle.

Handled investigation history is retained in
[TODO-resolved-2026-08-30.md](TODO-resolved-2026-08-30.md) and
[TODO-archive.md](TODO-archive.md).

## Detachable broker survival across Sublime restart — LIVE VERIFIED FIXED (2026-08-30)

Diagnostics proved direct brokers remained children of Sublime's non-breakaway
Windows job (`parent_pid` was Sublime and `in_job=True`), so Windows killed
them on Sublime exit before reattachment could run. `CREATE_BREAKAWAY_FROM_JOB`
cannot override a job that does not permit breakaway. Brokers are now created
through the Windows Task Scheduler service using a short-lived task registered
for the current interactive user. The task is removed immediately after the
broker consumes its randomized one-use `.launch` file; arguments, child
environment, and secrets stay in that file and the broker deletes it. A
production-shaped probe started under Task Scheduler rather than Sublime and
remained alive after its launcher exited.

Live verification completed after a full Sublime Text restart: the Codex
session reattached in place and continued responding, and the PowerShell
session in Tab 2 was also live and responsive immediately after restart. The
10-cycle Scheduled Task broker integration test passes as well. Full suite:
510 passed, 3 skipped.

**Separate live recovery result, 2026-08-30:** the original Codex tab stopped
responding while its broker remained alive. Opening another Codex tab did not
replace it. **Revive Frozen Tab** detached the failed client and reconnected
the original tab to the same broker process without using Codex `resume`;
conversation execution continued in place. This live incident verifies the
frozen-tab recovery path independently of full Sublime restart persistence.
