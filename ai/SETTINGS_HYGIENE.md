# Correcting and portabilizing `ai_terminal.sublime-settings`

Guide for an agent asked to clean up, generalize, or "spruce up" this
package's shipped settings file — remove personal machine paths, fix
profiles, make it work for a user who isn't donal. Written 2026-09-01 after
doing exactly this; see `ai/PROFILE_AND_RECORDING_INVARIANTS.md` for the
adjacent code-level contract on profile data itself.

## The one thing that will silently wreck a live setup if you get it wrong

Sublime Text's settings cascade (`Packages/<pkg>/Foo.sublime-settings` <
`Packages/User/Foo.sublime-settings`) replaces a top-level key **wholesale**
across files. It does **not** deep-merge nested dicts.

`ai_terminal.sublime-settings`'s `"profiles"` key holds ~27 named profiles.
If you write a `Packages/User/ai_terminal.sublime-settings` containing
`"profiles": {"OnlyOneAgent": {...}}`, that file's `"profiles"` value wins
entirely — every other profile (Claude, Codex, Bash, everything) disappears
from the merged view for that user. This is not hypothetical: it was about
to happen mid-session before being caught by checking `_all_profiles()` in
`ai_terminal.py` first.

**Before editing the shipped file's `profiles`, `context_files`, or any
other top-level key that a real user's `Packages/User` file might also set**:
copy the user's *current* `ai_terminal.sublime-settings` byte-for-byte into
their own `Packages/User/ai_terminal.sublime-settings` first (on Windows:
`%APPDATA%\Sublime Text\Packages\User\`). That freezes their live behavior
completely unchanged, decoupled from whatever you do to the shipped file
next. Verify with a byte-diff, not by eye.

(`AiTerminalSyncAgentProfilesCommand`'s generated-file merge in
`_all_profiles()` is a *different*, safe mechanism — it merges by profile
*name* in Python across two separate settings files/objects. That's not the
same as the native per-file cascade above, and doesn't have this trap.)

## Where personal paths belong, and where they don't

- Shipped `ai_terminal.sublime-settings` (this repo): generic only. Bare
  PATH-based commands (`"launch_command": ["jcode"]`), not absolute paths
  to one person's install location.
- `Packages/User/ai_terminal.sublime-settings`: where an individual's real
  absolute paths belong — a shim not yet on PATH, a personal fork build, a
  side-project's launcher profile. Never shipped, never committed.
- `agent_catalog.py`'s `CATALOG` is already the source of truth for the
  generic starting point of each known agent (bare command, spawn_env,
  quirk notes) — check there before hand-writing a profile; its own
  docstring already states "no personal paths, no secrets" as the rule.
- `profile_availability.py`'s `command_exists()` is the actual portability
  mechanism (PATH lookup for bare commands, file-exists for absolute ones)
  that makes a generic profile "just work" per-machine with zero settings
  edits. Prefer relying on it over hardcoding a path, unless the CLI has a
  documented PATH-caching problem (see Antigravity/Junie's own profile
  comments for the real example: a shim installed after Sublime's process
  already started is invisible to that process's PATH until restart).

## Verifying a change didn't break anything

In order, cheapest first:

1. `python -m pytest tests/ -q` — full suite, should stay green (currently
   ~516 passed, 3 skipped as of this doc).
2. Re-parse the edited `.sublime-settings` as JSON-with-comments (strip
   `//` line comments respecting string literals, strip trailing commas,
   `json.loads`) — confirms no syntax breakage from a text edit. A `.py`
   syntax error would be caught by pytest already; a `.sublime-settings`
   syntax error would not, since nothing in the test suite loads it as ST
   would.
3. If you changed something a fresh install would exercise (profile
   defaults, first-run behavior, the DLL fetch): actually build one. Copy
   `Packages/Sublime Text` to a scratch location, add an empty `Data/`
   folder next to `sublime_text.exe` (this makes that copy portable — it
   ignores `%APPDATA%` entirely, fully isolated from the live install) and
   `git clone` this repo fresh into `Data/Packages/GhostShell`. Launch it
   as its own process. This is what actually caught the two silent-failure
   bugs fixed 2026-09-01 — reading the code did not.
4. **Never uninstall or touch the live installed package** to test this.
   If an agent's own terminal session is itself running inside the live
   GhostShell install (check for a Sublime window whose open tabs include
   an active agent session before doing anything destructive), a real
   uninstall/reinstall there can kill that session out from under itself.
   The portable-copy method above tests the exact same install mechanics
   with zero risk to the live one.

## Don't defer to old comments/tests just because they're old

A docstring or a test asserting "never do X" from six weeks ago may be
correct, or may be stale reasoning nobody's revisited. Re-derive *why*
independently before keeping or reversing it — cite the current, fresh
reason in the commit message, not just "the comment said so." Example from
this session: `_pick_cwd_then()`'s "never show a picker, tell the user to
set a working directory instead" was kept as-is, but only after
re-deriving independently that it distinguishes a one-click shortcut
(`Tools → Shells → X`) from the two-step wizard (`Launch Agent…`) — a
reason that holds up on its own, not because the docstring said so.
