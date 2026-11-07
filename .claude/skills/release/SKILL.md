---
name: release
description: Cut a complete GhostShell release — the RELEASE_NOTES section, the pinned DLL reference and the notes committed in the tagged commit, then the GitHub release — with the two git commands that prove nothing was left behind. Use before and after every tag.
---

# Cutting a GhostShell release

0.2.18 went out with its notes complete but its own memory note written after the tag, so the tag
shipped the previous index. The rule below exists because that keeps happening.

## The invariant

> The tagged commit contains everything the release claims.

The `docs/RELEASE_NOTES.md` section, any pinned artifact reference, and every note written for that
release are committed **in** the release commit, and the tag points at it.

## Sequence

1. **Write the notes first**, in one commit:
   - `docs/RELEASE_NOTES.md` — a `## <version>` section under `## Unreleased`. It says what changed,
     what was measured (numbers, not adjectives), what was proved live and on which build, and what was
     **not** verified and why. Rollback instructions belong here when a binary is swapped.
   - `.claude/memory/` — a dated `project_<topic>_<date>.md` note for anything a future session needs,
     plus its line in `MEMORY.md` and a fresh `STATUS <date>` line at the top.
   - `README.md` when a user-visible behaviour or requirement changed.
2. **If a native library or other pinned artifact changed**, in the same commit: `RELEASE_DLL_URL` and
   `EXPECTED_SHA256` in `terminal/ghostty_vt.py`, the published release that hosts the file, and the
   rollback copy kept under `terminal/bin/`. Port and artifact ship together — never port the binding
   while the shipped DLL is the older ABI; that cost the owner his editor twice.
3. **Tag that commit** (`v<version>`), push `main` and the tag, then create the GitHub release whose
   body is the `## <version>` section.
4. **Prove it, and treat any output as a failed release**:
   ```
   git status --short                  # nothing
   git log --oneline v<version>..HEAD  # nothing
   ```
5. Say it is done only with that evidence, and list what was verified live (which build, which
   actions) and what was not, with the reason.
