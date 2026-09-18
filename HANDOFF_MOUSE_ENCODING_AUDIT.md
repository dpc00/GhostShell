# Handoff: GhostShell mouse-encoding investigation

Written 2026-09-18 by a Claude Code session running low on weekly usage, for
whichever agent (Claude, or otherwise) picks this up next. Read this whole
file before touching code — it tells you what's already verified vs. what's
still a hypothesis.

## Context: what GhostShell is

GhostShell (this repo) is a Sublime Text plugin that embeds a terminal for
driving AI coding-agent CLIs (Claude Code, Codex, etc.) inside Sublime tabs.
Core terminal logic lives under `terminal/`, wired into Sublime via
`ai_terminal.py` (a very large file — grep, don't read linearly).

Key architectural fact: GhostShell does **not** reimplement VT/escape-sequence
parsing. It vendors the real ghostty terminal engine as a compiled DLL
(`terminal/bin/ghostty-vt.dll`, built from ghostty commit
`634957c8e67cad5040f54cef57de5502450d1f5f` — see
`terminal/GHOSTTY_VT_PROVENANCE.md`) and calls into it via ctypes bindings in
`terminal/ghostty_vt.py`. `terminal/ghostty_engine.py` wraps that into a
higher-level Python object GhostShell's Sublime code uses.

Two local reference repos exist on this machine for comparison:
- `~/tools/ghostty` — the canonical ghostty source (Zig). This is the actual
  engine GhostShell vendors, so its Zig source is ground truth for what the
  DLL does and what C API it exposes.
- `~/tools/cmux` — a maintained, Ghostty-based **macOS terminal built
  specifically for AI coding agents** (manaflow-ai/cmux, Swift). This is the
  closest real-world analog to GhostShell's own mission (vertical tabs,
  notifications, wrapping Ghostty for agent-driven sessions), so it's the
  right reference for "how does a competent Ghostty-based embedder handle
  this."

## What was asked and what was found

The user asked me to compare GhostShell against ghostty and cmux to find real
bugs. I ran a read-only comparative audit (via a subagent) covering
`terminal/ghostty_vt.py`, `terminal/ghostty_engine.py`, `terminal/screen.py`,
`terminal/mouse.py`, `terminal/render.py`, `terminal/caret.py`,
`terminal/layout.py`, `terminal/history_scan.py`, and the mouse-routing code
in `ai_terminal.py` (~lines 2320-2560, 5560-5960, 8420-8490 at time of audit
— line numbers may have drifted).

### Confirmed finding: mouse protocol encoding is hand-reimplemented instead of using the vendored native encoder

- **What's wrong:** `terminal/mouse.py:43-88` (`encode_mouse`) hand-builds
  SGR/X10 mouse escape bytes with manual bit-packing and string formatting.
  `terminal/screen.py:142-160` (`mouse_tracking`/`mouse_sgr` properties)
  hand-resolves the active mouse tracking mode by inspecting a Python-side
  `private_modes` set GhostShell maintains itself. `ai_terminal.py`'s
  `_route_mouse_click` (`_MOUSE_HOLD` state machine, roughly lines 5845-5891
  at audit time) hand-rolls same-cell/double-click motion dedup and idle
  release timing, because Sublime never gives GhostShell a real mouse-up
  event. That function's own code comment admits repeated live-breaking
  fixes — a strong tell that this subsystem is fighting its own
  architecture.

- **Why it's wrong:** the same vendored `ghostty-vt.dll` already exposes a
  native mouse encoder — see `~/tools/ghostty/include/ghostty/vt/mouse/
  encoder.h` (and `event.h`): `GhosttyMouseEncoder`,
  `ghostty_mouse_encoder_setopt_from_terminal()` ("sets tracking mode and
  output format from terminal state"), and
  `GHOSTTY_MOUSE_ENCODER_OPT_TRACK_LAST_CELL` (native motion dedup by last
  cell) — solving exactly the two problems `mouse.py`/`ai_terminal.py`
  reinvent in Python. It also covers URXVT and SGR-pixel mouse formats,
  which `mouse.py` doesn't implement at all.

  GhostShell already does the *correct* thing for keyboard input: the
  `ghostty_key_encoder_*` C API is bound in `terminal/ghostty_vt.py` (around
  line 811-834 at audit time) and used properly in `ghostty_engine.py`
  (~lines 277-370). Mouse is the one input path that didn't get the same
  treatment.

- **Confirmed against cmux:** `~/tools/cmux/Sources/GhosttyTerminalView.swift`
  never encodes a mouse escape sequence client-side. Every `mouseDown`/
  `mouseUp`/`mouseMoved` handler forwards raw position + button straight into
  `ghostty_surface_mouse_pos`/`ghostty_surface_mouse_button` (see roughly
  lines 6532-6580, 7073-7092, 7449-7471) and lets native ghostty own mode
  resolution, encoding, and dedup entirely. This is independent confirmation
  that "push raw events into native, don't encode client-side" is the
  intended pattern for a Ghostty-based embedder.

- **Why this matters:** this plausibly explains the *pattern* of already
  open, hard-to-pin mouse bugs (see project memory:
  `ghostshell_omp_tab_scroll_hop_bug.md` — viewport snaps back into
  scrollback during busy TUI output; `project_continue_wt_export_mouse_
  injection.md` — WT export leaks mouse records past the mouse_handling
  gate). It's not necessarily the direct cause of either specific bug, but
  it's an architectural gap: any Sublime-side event-delivery quirk (jitter,
  coalesced events, no real mouse-up) becomes a bug the hand-rolled Python
  state machine must separately get right, when the native engine would
  handle it for free.

- **Confidence:** confirmed by reading both sides (GhostShell's actual code
  + the real header in the vendored ghostty commit + cmux's actual Swift
  source). **Not yet reproduced as a specific live failure** — no single
  input sequence was captured that proves a concrete crash/misbehavior from
  this gap alone. Treat it as "verified architectural discrepancy, unverified
  as sole root cause of any one specific bug ticket."

### What was checked and ruled out

- `screen.py:98-119` `resize()` looks like it truncates/discards rows
  without reflow on resize — **not a live bug**. `ghostty_engine.py:205-230`
  always forces `_last_scrollback_rows = -1` after `self.s.resize()`, so the
  next `_sync_scrollback()` does a full rebuild from native ghostty's real
  reflowed scrollback. There's a code comment there documenting a 2026-09-02
  regression where a prior "optimization" skipped this rebuild and was
  caught live — so this path is already correctly guarded.
- WT-export mouse leak (`project_continue_wt_export_mouse_injection.md`) —
  code paths confirmed to exist (`ai_terminal.py` ~8439-8486, mode 9001
  check), but this is inherent to handing the session off to a real external
  `wt.exe` process, which negotiates its own mouse mode directly with the
  child — outside GhostShell's Python gate by design. Already tracked, not
  a new discrepancy.
- Wide-char/grapheme row-cell iteration (`ghostty_engine.py:513-538`) —
  looked like a possible column-misalignment risk but no confirmed bug found
  without executing/building; not included as a finding.

## Suggested next step (not started)

Migrate `terminal/mouse.py` and the mouse-tracking-mode resolution in
`terminal/screen.py` to use the native `GhosttyMouseEncoder`/
`GhosttyMouseEvent` C API via `terminal/ghostty_vt.py` ctypes bindings,
mirroring the pattern already used for `ghostty_key_encoder_*`. This would
likely let you delete or drastically shrink `ai_terminal.py`'s
`_route_mouse_click`/`_MOUSE_HOLD` heuristic state machine, since native
`GHOSTTY_MOUSE_ENCODER_OPT_TRACK_LAST_CELL` handles the same-cell/motion
dedup the heuristic is trying to approximate.

Before starting: this is a "fix the class of bug" change, not a hotfix — it
touches a subsystem with a history of fixes breaking other cases live (per
`ai_terminal.py`'s own comments). Read `~/tools/ghostty/include/ghostty/vt/
mouse/encoder.h` and `event.h` in full first to get the exact function
signatures and option flags before writing any ctypes bindings. Test against
real mouse interaction in Sublime (click, drag-select, double-click,
scrollwheel in alt-screen apps) — this is UI-facing and per project
conventions must be verified live, not just by reading a diff.

## Where to find more context

- Project memory for this repo lives at
  `C:\Users\donal\.claude\projects\C--Users-donal-projects-GhostShell\memory\`
  — `MEMORY.md` is the index. Relevant entries:
  `project_mouse_encoding_reimplemented_not_native.md` (this finding, just
  written), `ghostshell_omp_tab_scroll_hop_bug.md`,
  `project_continue_wt_export_mouse_injection.md`,
  `project_ghostshell_mission_infallible_control.md` (states the actual
  goal: reliable AI-agent control of Sublime, don't paper over dispatch bugs
  with workarounds — relevant framing for how to approach this).
- This was a **read-only audit** — no code was changed as part of it.
