# Deviations from Terminus

Rule (user, 2026-09-20): Terminus is the reference. Every deviation must have a plain-language
justification here. "No justification found" is a defect, not an acceptable entry.

Status key: VERIFIED = read in both code bases, file:line given. UNVERIFIED = claim from an agent transcript.
Terminus checkout: ~/tools/Terminus, commit 0cccd3f (2025-12-16), 4,029 lines in the core.

## 1. Viewport positioning

**Terminus (VERIFIED).** One function, `scroll_to_cursor`, `terminus/render.py:354-361`. It runs once
after each render, scheduled with `set_timeout` (`render.py:322`), and calls `set_viewport_position`
once (`render.py:361`). No self-rescheduling loop and no clamp/pin/settle logic in render.py,
terminal.py, view.py or event_listeners.py.

**GhostShell (VERIFIED).** Several separate writers of viewport position in `ai_terminal.py`:
`_pin_terminal_viewport` (6020), `_pin_viewport_rest` (7735), `_pin_viewport_rest_dip_only` (7750),
`_compensate_trim_scroll` (7841), `_scroll_to_bottom` (7910), `_settle_viewport` (8067),
`_vp_pan_to_tui_scroll` (10667), and the self-rescheduling `_clamp_vp_loop` (10706, `_CLAMP_POLL_MS = 500`).
Eight call sites go through `_set_viewport` (2359); one, in `_compensate_trim_scroll` (7894),
calls `view.set_viewport_position` directly and bypasses the kill switch.

**Justification: NONE FOUND YET.** Agent claims (UNVERIFIED): the loop exists to stop TUI
apps and the host fighting over scroll position (Claude/omp/Grok, 2026-09-18/19). Grok changed
it from 8ms to 500ms on 2026-09-19 (commit 57624a0) with no noticed harm.

**Open defect (user report, 2026-09-20).** With Claude Code CLI in a tab, the text jiggles one line
up and back down during command-line typing. Cause not yet identified.

## 2. Kill switch is a code constant, not a setting

`_SCROLL_MANIPULATION_ENABLED = True` (`ai_terminal.py:2356`). Its comment says "NOT yet the
default anywhere live", but it is True. The comment is stale. As a constant it cannot be tuned
without editing code, which conflicts with the "everything editable via settings" rule.

## 3. History cap (VERIFIED)

Terminus `scrollback_history_size` default 10000 (`render.py:123`). GhostShell caps at 300.
Justification (UNVERIFIED, from omp/Claude 2026-09-19): 300 was tuned so the whole buffer fills the
minimap. Not yet checked against the code comment.
