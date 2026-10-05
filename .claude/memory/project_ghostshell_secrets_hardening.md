---
name: project_ghostshell_secrets_hardening
description: "User wants to bring the LLM (tonylchang/sublime-llm) package's secret-handling design into GhostShell's ai_terminal.py, which currently scatters raw API keys/tokens across spawn_env dicts in ai_terminal.sublime-settings"
metadata: 
  node_type: memory
  type: project
  originSessionId: 6baf5f30-f111-4757-8168-5593e86f5789
  modified: 2026-09-09T07:27:13.913Z
---

On 2026-09-09, during the package-skill-generator random-package testing
campaign, reading the LLM package's `sublime_llm/secrets.py` and
`logging_setup.py` surfaced a genuinely well-designed credential-handling
pattern. The user reacted strongly ("I actually find the whole deal
distasteful") to the contrast with how GhostShell (`ai_terminal.py`,
`ai_terminal.sublime-settings`) currently handles secrets -- profile
configs have raw `spawn_env` dicts with plaintext API keys/tokens sitting
directly in the settings file, no resolution cascade, no enforced file
permissions, no log redaction.

**Why**: the user wants GhostShell's credential handling brought up to
the same bar, not left as an afterthought now that a clear, reusable
reference design exists in the same ecosystem (another Sublime Text
plugin, tested the same night).

**How to apply**: when next asked to work on GhostShell/ai_terminal.py,
or if the user brings up "secrets"/"credentials"/"API keys" in that
context, remember this is flagged as real, wanted follow-up work, not
just an observation. The concrete pattern to port (from
LLM's `sublime_llm/secrets.py` + `logging_setup.py`):
1. Resolution cascade: env var first, then an external config file
   stored OUTSIDE Packages/User (so Package Control sync / Dropbox /
   dotfiles repos can't leak it), then a legacy fallback, then the
   settings file itself only if explicitly opted in.
2. Enforce file permissions on every write AND re-check on every read
   (chmod 600 file / 700 parent dir on POSIX; warn if permissions drift
   looser than expected).
3. Placeholder detection so a copied example config with
   "YOUR_API_KEY_HERE" doesn't read as "configured".
4. Centralized log redaction: register every resolved secret value in a
   process-wide set, and have a single logging Filter scrub any log
   record containing a registered secret (plus a regex fallback for
   key-shaped strings like `sk-...`/`Bearer ...`) -- so a future bug in
   any one code path that accidentally logs a raw key still gets caught,
   rather than relying on every call site to remember not to.

This is a real, deliberate refactor of a big, delicate, actively-used
file (`ai_terminal.py`, already the subject of a live-tested tab-close/
session-kill fix this same session) -- treat it as its own dedicated
pass, not a quick patch bundled into something else. See also
[[project_gadzillion_package_test_campaign]] for the context this came
up in.
