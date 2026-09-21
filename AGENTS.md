# Rules for every agent working in GhostShell

Read this before touching anything. These rules come from the owner. They apply to every agent
(Claude, Codex, Grok, Vibe, omp, Copilot, Augment, Devin, any other).

## Leave nothing behind

1. Commit each piece of finished work as soon as it is done. One clear message that says WHAT changed
   and WHY. Never leave work uncommitted at the end of a session.
2. Push to `origin/main` when your work is done.
3. Stage only files you changed yourself. Do not commit another agent's or the owner's uncommitted
   changes without reading them and saying what they are.
4. Do not leave scratch files in the repo (test queries, temp notes, tool config files). Delete them
   or put them outside the repo. Your session's temporary files belong in your own scratch directory.
5. Never run `git restore`, `git checkout -- <file>`, `git reset --hard`, `git stash drop` or any
   command that discards uncommitted work. Another agent's or the owner's unsaved work may be there
   (this destroyed real work on 2026-09-17).

## How the program must be built

6. Terminus (https://github.com/randy3k/Terminus) is the reference design. Every difference from
   Terminus must be listed in `docs/DEVIATIONS_FROM_TERMINUS.md` with a plain-language justification.
   Do not add a difference you cannot justify.
7. Everything is editable through a setting, every setting must actually do something, and every
   behavior is tested. A beginning Python programmer must be able to read and fix it.
8. Follow the electronic-equivalent structure: inputs (sources), outputs (sinks), and named blocks
   between them. See `docs/HARDWARE_MODEL_DIAGRAM.md` and `docs/ghostshell-data-flow.dot`.
9. Use standard Python naming (PEP 8). Anything using `ctypes` must name every item properly and
   document the purpose of every line.
10. Do not add a timer or polling loop when a Sublime event can do the job.

## Safety

11. `ai_terminal.py` reloads live in Sublime when saved. After you edit it, Sublime must be
    restarted. Before restarting, check for running sessions and never kill one.
12. Do not record a guess as a fact. In `docs/` mark every finding VERIFIED (with file:line or a test)
    or UNVERIFIED.
