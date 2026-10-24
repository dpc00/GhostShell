---
name: tern-sdk-no-daily-update-2026-10-18
description: tern-sdk (tools\tern-sdk, clone of stencil-hq/tern-sdk) is NOT updated daily and nothing is scheduled; check it on request or when GhostShell terminal support is being worked on, and act only on monumental changes
metadata:
  type: feedback
---

**Donald, 2026-10-18:** when a note said "tern-sdk daily update" he answered that he is only interested in keeping GhostShell's support current if monumental changes are happening there. No daily update, no scheduled job, no polling (his 2026-10-07 rule: no scheduled GitHub checks).

**State when written:** `C:\Users\donal\tools\tern-sdk` is a plain clone (origin https://github.com/stencil-hq/tern-sdk). Local HEAD 13d3cca (2026-10-12 "chore: synced the sdk"); upstream had one newer commit f3f9fee (2026-10-16), 5 lines in `rust/`, not pulled. `tools\tern-spec` and `tools\tsp-scratch` go with it and were kept.

**State 2026-10-24 (he asked "what has changed in tern-sdk", then to update GhostShell):** clone at 9e9c074 (2026-10-19), nothing newer upstream. Everything up to 9e9c074 is reviewed against GhostShell: all 46 node kinds and protocol v1 match; `aside` is already announced when the view can open a column (tsp_host.py:68,171); chart `min`/`max`/`marks` and bar `tone` implemented (f4c5327); `hovercard` shown as the tooltip text via `_tooltip` because Sublime has no hover event for phantom content (a true floating card cannot be built). Start the next review at 9e9c074: `git diff --stat 9e9c074 origin/main`. He wants the maintenance done by me, not tracked by him.

**How to apply:** do not pull or schedule. When he asks about Tern, or when GhostShell terminal-protocol work starts, run one `git fetch` and report in two lines: commits behind, and which top-level folders changed (`git diff --name-only HEAD..origin/main`). Treat as monumental only a change to the protocol or spec parts GhostShell implements (the spec/document model, the ANSI/escape handling), not a version bump or a one-file change. Say so and let him decide.
