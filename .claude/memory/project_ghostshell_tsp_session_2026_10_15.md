---
name: ghostshell-tsp-session-2026-10-15
description: 2026-10-14/15 GhostShell TSP session: what shipped (aside opt-in, in-tab settings, ANSI palette, prefs change events, scroll fixes), how it was tested in portables, what is open or deliberately left; mouse caret in omp composer is low priority by his word
metadata:
  type: project
---

**His rules from this session:** do not split or change his window layout or groups (the Settings sheet must stay in the omp tab); do not move his focus or run tests in his main window unless he says go (he got keyboard focus stuck on tab 2 twice); tell him before any window-opening test; he is often resting: decide, act, report in plain words.

**Shipped in GhostShell (all pushed, last commit 8714598):**
- Aside column is opt-in: profile setting `tsp_aside_column` (default false). Off = the hello does not announce `aside`, omp sends `/settings` as a full prefs page in `main`, no group is ever added.
- A `prefs` change event (switch, stepper, choice, Reset) now reaches the program: `TspHost.send_event` used to drop every `change` that was not an `el` form control (`click_control` returned None). Events that name an `item` pass through.
- Settings page opens at its top, and typing in its search box no longer pins the view to the bottom (`_prefs_page` in `tsp_view.py`).
- ANSI colours: `_terminal_lines` read the nonexistent `tsp_html._ANSI16`, so unthemed colours were black; defaults now come from `tsp_style._ANSI`.
- After an aside open/close a view that was at the end is pinned and clamped back to the end (`_pin_on_aside_change`, `_clamp_soon`).
- `close_aside()` on tab close and surface end; reveal and scroll on windowed lists verified; Python 3.8 host runs GhostShell (portable 4200).

**Test method that worked (no effect on his window):** portable 4215 with a junction `D:\st_portable_4215\Data\Packages\GhostShell` -> `projects\GhostShell`, a User `ai_terminal.sublime-settings` with `broker_registry_dir` pointing at its own folder (so it cannot see his brokers), drive it over its MCP port 9512 (`/mcp`, tool `batch` -> `eval_python`), SDK client from `tools\tern-sdk\python` (use `connect(..., timeout=10.0)`, the 1 s default loses to a cold broker). Real clicks: `SetWindowPos` topmost, `SetCursorPos` + `mouse_event`; screenshot size equals window rect. Remove junction with `rmdir`, recycle the rest. Never overwrite a file you are about to read (`open(p,'w').write(open(p).read())` empties it).

**Open or left on purpose:**
- Clicking in omp's composer to place the caret does not work, and never did: GhostShell sends no `edit`/`undo`/`send` events at all. He says Home/End/arrows are enough and it is NOT a priority. Do not start it unasked.
- A detach/reattach after a change that touches both `ai_terminal.py` and `terminal/*.py` once left the omp tab with no TSP host and raw JSON on screen (cause unproven, likely a stale `terminal/tsp_host.py`; `TspHost(..., aside=)` TypeError is the guess). Use a full Sublime restart after such changes.
- After a reattach `hello_seen` is False and `hello_app` empty (by design: replayed history is stripped); no visible effect found.
- Tab shows the name "Windows PowerShell" after a reattach; cosmetic, not investigated.
