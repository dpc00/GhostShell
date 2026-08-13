# ai_terminal TODO

Open/unresolved items only. Full dev-session history (root causes, fixes,
verification detail) lives in [TODO-archive.md](TODO-archive.md).

## Copy-mode "turns on by itself" report (2026-08-11), Kiro profile — UNRESOLVED

`term.copy_mode` flipped ON without a deliberate `ctrl+alt+c` press during
a Kiro session. Root cause not found — ruled out: deliberate press, AltGr/
accented-character composition, cursor-placement code (never touches
`copy_mode`), a settings-driven startup default (`self.copy_mode = False`
is hardcoded). The triggering Kiro tab was already closed by investigation
time, so no live state/console evidence from the actual incident was
recoverable.

TEMP DEBUG logging (call-stack dump on the ON transition) is still active
in `AiTerminalToggleCopyModeCommand.run` (`ai_terminal.py`), waiting to
catch the actual trigger next time this reproduces. Remove once
root-caused. See archive, "Copy-mode 'turns on by itself' report" section,
for everything ruled out so far.

## cmux feature-port backlog — not started

Two items survived triage against GhostShell's actual code (see archive,
"resolved via cmux-style copy-mode toggle" section, for the other 4 items
that were already implemented, not applicable, or demoted):

1. **Command-block segmentation** (`OSC133CommandParser.swift` in cmux — a
   pure idle→prompt→command→output state machine) — could power a "jump
   to previous/next command" navigation in scrollback.
2. **CR-redraw line folding** (`foldLine` in cmux) — collapses `\r`-progress-
   bar spam to its final state; useful for `render.py` copy/log-export so
   a copied progress bar isn't hundreds of stale redraw lines.

## page_keys_to_pty — minor follow-up verification gaps

From the 2026-08-13 full-profile audit (see archive for the complete
per-profile writeup). Not urgent, just not individually confirmed:

- `Claude --chrome` / `Claude →⇢⇨ Ollama --chrome` — inherited the fix
  status of the base `Claude` profile (unaffected either way; Claude
  itself needed no fix) but not separately re-verified.
- `OpenCode --mini` / `OpenCode →⇢⇨ Ollama --mini` — got
  `page_keys_to_pty: true` by inference (same binary family as the
  audited base `OpenCode` profile), not individually live-tested.
