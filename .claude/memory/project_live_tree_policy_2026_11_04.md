# Live-tree policy and TSP input, 2026-11-04 (imported from omp's memory bank 2026-11-11)

Dated record from omp's mnemopi bank.

## Junction = live code (policy, still in force)

`AppData/Roaming/Sublime Text/Packages/GhostShell` is a JUNCTION to the repo working tree, so any
uncommitted edit there is live code, and saving it triggers a plugin reload under a live TSP
session (blank tab, panels lost). Policy set 2026-11-04: **never edit the live tree; work on a
branch off the release tag and keep the live package on released code.** At the time, the release
was v0.2.17 = 378c51c; unreleased commits 2e21049, 5be17ea, 68b3eb3, 80ab426 and the keyboard-first
pair had been force-pushed away, local tag `pre-midnight-rescue` (add8573) still held them, and
stash@{0} held an unapplied static-tab-mark patch. (Release state has moved on — 0.2.20 released
2026-11-10 — but the policy does not change.)

## Keyboard priority (owner, 2026-11-04)

Top priority: ai_terminal tabs running omp with TSP native rendering must accept keystrokes. Do
not add unrequested "keyboard first" behavior — ESC-before-first-key and paint-throttling after a
keystroke were shipped in 04e032a and 565a545, swallowed keys in TSP/omp tabs (ESC+char became
Alt+char; `_do_render`'s early return also skipped the TSP native repaint so typed text never
appeared), and were reverted at 09a1ccf/add8573. Verify any input/render change in the live Sublime
(sublime-mcp eval_python or screenshots); eval_python needs owner approval the omp MCP client
cannot request.

## TSP input latency (unfixed as of 2026-11-04 — check current state before acting)

Max keystroke rate falls as the conversation grows: each render tick runs on the plugin main
thread and rebuilds O(panels) work — `TspView.render` builds a fresh `sublime.Phantom` plus
`tsp_html.page()` for every panel each tick (terminal/tsp_view.py:652-655), `_set_text` rebuilds
the whole caption document and runs `ai_terminal_render` as a full buffer replace (:449-466,
:656), and `_log_tsp_document` sweeps the entire document through `tsp_text.render_lines` twice a
second (ai_terminal.py:5840-5860). Cadence `_RENDER_MS=100` with `_RENDER_MIN_INTERVAL_MS=30`.
Fix direction: cache per-part Phantoms keyed by HTML hash, tail-only document writes, append-only
text log.
