---
name: study-the-named-mechanism
description: When Donald asks me to study package X "for aspects that could apply to GhostShell", he means the ONE mechanism he points at (for Buildview: its view-to-view piping), not a broad survey and not tracing GhostShell's own internals.
metadata:
  type: feedback
---

On 2026-09-29 I answered "study Buildview for aspects that could apply to GhostShell" with a wide survey (placement, scroll modes, settings, session log) and started tracing GhostShell's own PTY pipeline. Donald corrected twice: "the pertinent aspect to study is the piping, not the other things", then, when I began reading GhostShell's PTY read loop, "not that piping, Buildview's piping" (interrupted the tool call).

**Why:** the request names a package because of one mechanism; the rest is noise and reading GhostShell's own pipeline is a different task.

**How to apply:** for "study X", identify the single mechanism he means (ask one short question if it's genuinely ambiguous), study THAT in depth (read the code, test its limits live, measure), and only afterwards say briefly how it could carry over. Don't trace or critique GhostShell's internals unless he asks. Put GhostShell-related notes in the GhostShell repo, see [[reference_buildview_ghostshell_study]].
