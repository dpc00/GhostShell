---
name: buildview-ghostshell-study
description: Where the 2026-09-29 Buildview-to-GhostShell study lives and its ranked ideas (patch render, placement memory, scroll-follow setting); nothing was changed in GhostShell.
metadata:
  type: reference
---

Donald asked (2026-09-29) to study Buildview for what could apply to GhostShell; the pertinent aspect is **Buildview's view-to-view piping** (`pipe_views.py`), not the rest of the package and not GhostShell's own PTY pipeline. Result: `C:/Users/donal/projects/GhostShell/ai/BUILDVIEW_STUDY.md` (rewritten, commit 4b7f839; Donald wants GhostShell notes in the GhostShell repo). GhostShell code was NOT modified or run.

Live-tested findings (portable ST 4215, 9 scenarios + benchmark): read-head delta copy is constant-cost (0.45 -> 0.61 ms per step up to 400k chars, vs 0.94 -> 10.66 ms for re-copying everything); the pipe is idempotent (a skipped event loses nothing) and the destination is rebuildable from the source; but it silently loses text if the destination is closed, and corrupts permanently if the source is truncated/edited in place or the user types into the destination (position-mirrored writes, no size check). Suggested hardening: check dest.size() == read-head, rebuild from source on mismatch.

Test script (outside the repo, per GhostShell AGENTS.md rule 16): `%TEMP%/pipe_study_keep.py`. See [[feedback_study_the_named_mechanism]].
