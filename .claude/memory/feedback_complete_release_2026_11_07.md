---
name: feedback-complete-release-2026-11-07
description: HIS ORDER (2026-11-07) after ten incomplete releases from agents - nothing the release claims may be left out of the tagged commit; no checker scripts, put the rule in skills, guides and notes
metadata:
  type: feedback
---

# "this is the 10th time an agent has done an incomplete release" (2026-11-07)

His words, after 0.2.18 shipped with its release notes complete but its own memory note (and the
sublime-mcp guide and skill) committed *after* the tag:

> "this is the 10th time an agent has done an incomplete release"
> "you are supposed to learn to do the right thing, not make scripts that will be forgotten tomorrow"
> "don't make scripts, make skills, guides and notes"

## What that means in practice

- **The rule, not a checker.** I wrote two `tools/release_check.py` scripts and he deleted them by
  refusing them: a script nobody runs is forgotten by the next session. The rule goes into the skill
  (`/.claude/skills/release/SKILL.md`), the notes and the guide.
- **The invariant:** the tagged commit contains everything the release claims — the notes section,
  every guide/skill/note written for it, and the pinned-artifact references.
- **The proof, two commands, run before saying it is done:**
  ```
  git status --short                  # must print nothing
  git log --oneline <tag>..HEAD       # must print nothing
  ```
  Anything printed means the release is incomplete: that commit is not in the tag.
- **Sequence:** prose and notes first → version strings / pinned artifact refs in the same commit →
  tag → publish → prove → report what was verified live and what was not, with the reason.

## Why it kept happening

Each time the work was real and the tag was early: the document existed on disk (his editor even
served the new guide through `get_help`, because the live package is the working tree), so it looked
finished. The tag is the only thing a stranger reads, and it was a commit behind.
