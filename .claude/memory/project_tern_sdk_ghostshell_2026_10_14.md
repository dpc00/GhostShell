---
name: tern-sdk-ghostshell-2026-10-14
description: stencil-hq/tern-sdk is the program side of TSP; used as a test client for GhostShell's host; what was added (el/media/block, forms, listen:false, styles, flow, palette, reduce-motion), what is left, how to run an SDK example in a tab
metadata:
  type: project
---

**Finding (2026-10-14, his question "does tern-sdk help GhostShell omp support"):** tern-sdk (Rust/Python/Go/TS) is the PROGRAM side of the Tern Surface Protocol; GhostShell is the TERMINAL side (terminal/tsp_*.py). It cannot replace GhostShell code (Python SDK needs 3.11+, ST hosts are 3.3/3.8). It is a test client and a second spec: docs at docs.stencil.so/tern/protocol/<page>.md (and /tern/elements/el.md), clone at `C:\Users\donal\tools\tern-sdk`.

**Done and pushed (GhostShell origin/main), each tested live with SDK examples through GhostShell tabs, omp 18.11.0 re-checked each time (app omp, 42 kinds, no errors, native view draws):**
- 6854721 el/media/block kinds, offered to every program except omp.
- 541e7e6 radio/checkbox state, `change` event, `values` on actions in a form.
- 24d9ea3 host keeps hello_app and kinds_offered.
- 3ac1aff `listen:false` surfaces get no acks, events or errors (table.py recording: only the hello reply goes back).
- 60a5149 `s` stylesheets: tsp_style.py applies a safe subset (colour, background, padding, margin, radius, weight, style, align, decoration; var(--sf-c-*) via theme) to el nodes; flex, var(--x), box-shadow, :checked dropped. "styles" announced.
- cf0e14c flow surfaces: on close with keep (default for flow) the document is written as plain text into the output so it stays in the scrollback; el tables aligned in text, inline cells in HTML. "flow" announced. NOT checked: a flow program run from a shell prompt in a tab (the SDK-run tab closes itself on exit); checked by replaying the recorded table.py session.
- aee68b0 `t` redraws at once and errors on an unknown surface; hello reduceMotion true when tsp_animation_ms is 0; "program-palette" and "reduce-motion" announced (no motion events: the setting does not change mid-session).
- Real pointer: with SetCursorPos/mouse_event (script click.ps1 in the session scratchpad, window screenshot gives coordinates) a click on Large sent change size=l and a click on Create sent action submit values size=l; program closed and exited.

**His decision (2026-10-14):** do not gate features to non-omp programs; announce features as they are added (omp shares the hello), so omp must be re-tested after every change.

**Not done, on purpose:** `scroll` feature and the scroll/reveal ops (accepted, do not move the view: needs node id to screen position mapping in tsp_view); `aside` (Sublime cannot dock a sheet beside a pane); el text inputs and `block` (the spec says el `input` is only checkbox/radio and `block` is a plugin concept, nothing to draw). Both left unannounced.

**How to run an SDK example in a GhostShell tab (no settings edit):** in sublime-mcp batch eval_python (print output; it shows only prints), `m = sys.modules['GhostShell.ai_terminal']`; `pn, argv, env = m._resolve_profile_launch('omp')` (adds the newer ConPTY folder; skipping it makes the hello vanish); `env['PYTHONPATH']` = sdk `python\src`; optional `env['TERN_TSP_RECORD']` = a JSONL path; `v = m._terminal_view(w, name=m._next_ai_name(w, prefix='TernTest'), profile_name=pn)`; `w.focus_view(v)`; `m._spawn_into_view(v, cwd, pn, [r'C:\Program Files\Python314\python.exe', r'examples\ask.py'], env)`. Check `m._TERMINALS[v.id()]._tsp` (hello_seen, hello_app, kinds_offered, documents, errors, events_sent). For omp itself use argv from `_resolve_profile_launch`. Hot-reload with importlib.reload of GhostShell.terminal.<module> (tsp_style must be import_module'd first the first time). A tab whose program exits closes itself. End a tab: focus it, `ai_terminal_end_session`. One example per eval_python call (5 s cap).

## Plan, his decisions and progress (carved out of the sublime-mcp baton on 2026-10-15)

## UPDATE (2026-10-14, later, after /compact; his window is 1M, not 150k): the plan below is DONE (items 1, 4, 5, 6, 8; 2 and 3 skipped by him; 7 aside not possible). Pushed 3ac1aff, 60a5149, cf0e14c, aee68b0; details and what is left in project_tern_sdk_ghostshell_2026_10_14.md. Nothing of this plan remains except optional scroll/reveal.

## START HERE (2026-10-14, end of the tern-sdk session, stopped at ~220k context): "add everything" for TSP in GhostShell (OLD PLAN, see UPDATE above)
Done and pushed: GhostShell 6854721 (el/media/block), 541e7e6 (radio/checkbox state, change events, form values), 24d9ea3 (hello_app, kinds_offered). omp re-tested: app 'omp', 42 kinds, no errors, native view draws. Details and how to run an SDK example in a tab: note project_tern_sdk_ghostshell_2026_10_14.md (read it first; SDK clone in tools\tern-sdk). His order: "keep going, add everything. That is the point of this." Plan, in this order, one commit + push per item (GhostShell AGENTS.md), advisor reviewed it:
1. **listen:false bug FIRST (real defect today).** The host acks every frame; a surface opened with `listen:false` must get NO acks, events or errors (surfaces.md "Not listening"). table.py uses `show` = listen:false, so those acks land on the pty input (would be typed into a shell prompt). Check with TERN_TSP_RECORD for table.py: no `"dir":"in"` lines after the hello reply.
2. **Gate new features like kinds:** offer them only to programs that are not omp (record in `kinds_offered`/a `features_offered` field); omp's hello must stay as is (42 kinds, features blobs/settle/adopt/dock).
3. **Announce a feature only after it really works** (a stub is worse than leaving it out): `program-palette` (host stores `palettes`; tsp_html.Theme takes a palette; check tsp_view applies it), `reduce-motion` (tie to tsp_animation_ms), `scroll` (_op_scroll and _op_reveal look like no-ops; the view must really move).
4. **`styles` (`s` verb):** keep the sheet per surface/name; minihtml has no var(), flex, box-shadow, :checked+span; translate a safe subset onto el classes (colour, background, padding, margin, radius, weight, text-align), drop the rest, do not claim Tern look.
5. **`flow`:** closed flow surface stays in place as command output (surfaces.md). Idea: on close with keep, write tsp_text.render_lines(doc) into the terminal engine as output so it becomes ordinary scrollback.
6. **el text inputs, block, media:** read input.md and the elements pages first (events of a text `input`); do not guess.
7. **`aside`** last; if Sublime cannot dock a sheet beside the pane, leave it unannounced and say so.
8. **Final checks:** real-pointer click on Create (SetCursorPos method from the popup note; feeding navigate does not count); omp regression after every change to tsp_view/tsp_html.
**HIS DECISION (same day, after reading the plan): do 1, do NOT do 2 and 3, do 4, 5, 6, 7, 8.** So: no gating of features to non-omp programs and no "announce only when it works" step; implement the features and announce them in the hello as they are added (his word; omp regression check in item 8 still applies, since the hello is shared with omp). He asked what "spec pages" are: the Tern protocol documentation at docs.stencil.so/tern/protocol (told him in plain words).
How to work: ONE prompting tool call per message (this session broke that rule several times); save spec pages to the scratchpad with one PowerShell call and Grep them, never dump them into context (each cost ~5k; docs at https://docs.stencil.so/tern/protocol/<page>.md); one SDK example per eval_python call (5 s cap); write small tern_sdk test scripts in the scratchpad and judge by the TERN_TSP_RECORD file. Handshake/surfaces/feature meanings were already read this session: features table is in handshake.md (aside, scroll, styles, flow, program-palette, reduce-motion).
