# ai_terminal TODO

## Ghostty feature parity review (2026-08-10)

- [x] Bold/italic/underline rendering — DONE 2026-08-10. Style bits (already
      parsed from libghostty-vt in `ghostty_engine.py`, previously discarded)
      now flow into scope names (`ai.fb.<fg>.<bg>.s<id>`, `colors.py`) and
      into generated `.sublime-color-scheme` rules via `font_style`
      (`ai_terminal.py:_make_fb_rule`). Bold was silently unrendered too
      (same root cause) so it got fixed in the same pass.
      Strikethrough is NOT implemented and can't be: Sublime's color-scheme
      `font_style` has no strikethrough value (only bold/italic/glow/
      underline/stippled_underline/squiggly_underline — confirmed via
      Sublime's own docs). The SGR bit is still parsed and simply dropped,
      same as before.
      Legacy fallback `parser.py` (dead code, nothing imports `Parser` at
      runtime — `GhosttyParser` is the live engine) was NOT touched.

- [x] OSC 0/2 window/tab title support — DONE 2026-08-10. libghostty-vt
      already parses OSC 0/2 internally and exposes it via
      `GHOSTTY_TERMINAL_DATA_TITLE`; added the ctypes binding
      (`ghostty_vt.py`: `GhosttyString` struct + `TERMINAL_DATA_TITLE`),
      `GhosttyParser.get_title()` (`ghostty_engine.py`, queried once per
      `_sync()` since the returned string is only valid until the next
      write), and a render-tick hook `_maybe_apply_osc_title()`
      (`ai_terminal.py`) that renames the ST tab via `view.set_name()`.
      Gated OFF by default behind `"osc_title_updates_tab"` in
      `ai_terminal.sublime-settings` (global or per-profile override, same
      pattern as `mouse_handling`/`pin_viewport`) — existing profile-name
      tab titling is unaffected unless a user opts in.
      OSC 1 (icon name) is not handled — Sublime tabs have no separate icon-
      name slot, and libghostty-vt's TITLE data id is documented as OSC 0/2
      only.
      Verified directly against the real DLL (not just unit tests):
      `GhosttyParser.get_title()` correctly returns None before any OSC
      sequence, then the right string after OSC 0 and OSC 2, unaffected by
      plain text. NOT verified live in Sublime Text (would require a plugin
      reload, which crashes ST per prior experience) — check next normal ST
      restart with `python3 -c "print('\033]0;test title\007')"` after
      setting `osc_title_updates_tab: true`.

- [x] Bracketed paste mode — DONE 2026-08-10. Turned out to be a latent bug,
      not a missing feature: `AiTerminalKeyInterceptor.on_text_command`
      (`ai_terminal.py`, paste branch) already wrapped every Ctrl+V paste in
      `ESC[200~...ESC[201~` unconditionally, regardless of whether the
      running program had opted in via DECSET `?2004h`. That meant pasting
      into anything that never asked for bracketed paste (cmd.exe,
      PowerShell, a plain REPL) inserted literal `~200~`/`~201~` garbage.
      libghostty-vt already tracked the opt-in correctly (confirmed:
      `screen.private_modes` gains/loses `2004` exactly on `ESC[?2004h`/
      `ESC[?2004l`, verified directly against the DLL) — it just wasn't
      being consulted. Fix: gate the wrap on `2004 in term.screen.private_modes`.

- [x] Cursor style (DECSCUSR) — DONE 2026-08-10, resumed after the key-
      handling detour. Implemented blank-cell shape-switching only, no
      blink, exactly as scoped:
      - `ghostty_vt.py`: `RENDER_STATE_DATA_CURSOR_VISUAL_STYLE` (=10 on the
        *render-state* enum — do not confuse with `TERMINAL_DATA_CURSOR_
        STYLE`, also =10 but on the *terminal* enum, which is the SGR text
        style applied to newly-typed characters, a different thing) +
        `RENDER_STATE_CURSOR_VISUAL_STYLE_{BAR,BLOCK,UNDERLINE,BLOCK_HOLLOW}`.
      - `ghostty_engine.py`: queried once per `_sync_grid()` (render_state
        is already updated there) into `Screen.cursor_shape` (new attr,
        `screen.py`, default `"block"`), via `_CURSOR_SHAPE_NAMES` map.
      - `render.py`: `paint_host_cursor()` gained a `shape=` param;
        `_HOST_CURSOR_GLYPHS` maps block/bar/underline/hollow to
        █/▏/▁/▯. Only affects the blank-cell (EOL/empty-prompt) case — a
        real character under the cursor still uses colour-reversal
        unconditionally, per the architectural limit noted above (no shape
        concept applies there).
      - `ai_terminal.py`: call site now passes `shape=term.screen.cursor_shape`.
      No setting needed — this just makes the existing synthesized cursor
      match what the app actually asked for instead of always being a block.
      Verified directly against the real DLL: fed `ESC[1 q` through
      `ESC[6 q` (DECSCUSR block/underline/bar, blink and steady variants)
      and `ESC[0 q` (reset) — `screen.cursor_shape` tracked all of them
      correctly (block→block, underline→underline, bar→bar, reset→block).
      "hollow" is a real libghostty-vt state but has no standard DECSCUSR
      code (0-6) that reaches it, so it won't come up in practice; the glyph
      mapping still exists for it as it's mechanically free to include.
      NOT live-verified in the running ST process, same standing reason as
      the rest of this session's changes (see baton above).

## Key handling / selection session baton (2026-08-10)

Long back-and-forth (user: "many pointless discussions on the subject")
about keyboard navigation and selection inside terminal views feeling
broken compared to a normal ST buffer. Root causes found and FIXED (all in
`ai_terminal.py`, all compiled + 144/144 pytest-passing, **none live-
verified in the running ST process** — user has an ~1400-line-context live
Claude Code session open in a terminal tab and is explicitly UNWILLING to
restart ST or trigger a plugin reload to test these; that verification is
deferred indefinitely until the user chooses to restart on their own):

- **PageUp/PageDown** were being forwarded to the PTY by default and, for
  readline-style CLIs, landed as something that looked like Home/End
  instead of scrolling. Fixed: PageUp/PageDown now always do native ST
  page-scroll (`move` by `pages`, same motion as dragging the minimap)
  *unless* `_tui_like(term)` is true (fullscreen alt-screen app — vim/less/
  htop legitimately want raw PageUp/PageDown for their own pagination).
  This is a plain default-behavior fix, not gated behind any setting.
  `_home_end_native_enabled`'s docstring was updated to reflect that
  PageUp/PageDown mostly bypass it now (still reachable only for a
  TUI-like profile that also opts into `home_end_native`).

- **Shift+Left/Right/Up/Down did nothing** (silently forwarded to the PTY,
  same as plain arrows) — no way to extend a selection via keyboard at all.
  Researched Terminus (`randy3k/terminus` on GitHub, fetched via `gh api`)
  to see what a known-good prior terminal package did: Terminus *also*
  forwards Shift+Arrow to the PTY unconditionally in its default keymap —
  it never had keyboard-based selection extension either; its only
  selection path was mouse drag (never intercepted, so it worked via plain
  ST mouse handling). So this is a deliberate NEW improvement past Terminus
  parity, per explicit user request, not a restoration of lost function.
  Fixed: Shift+Arrow (no Ctrl/Alt) now unconditionally does native ST
  `move` with `extend: True` (characters for left/right, lines for up/
  down) — never forwarded to the PTY, including while positioned over the
  live command line. Plain arrows (no Shift) are untouched and still go to
  the PTY so editing a typed command still works.
  Known trade-off, accepted explicitly rather than silently: a fullscreen
  TUI that binds its own meaning to Shift+Arrow will no longer see it —
  unconditional per the user's explicit ask ("must work on command-line
  also"), not gated on `_tui_like()`.

- **Ctrl+Shift+Home/Ctrl+Shift+End** got the same unconditional treatment:
  `move_to` `bof`/`eof` with `extend: True`, never forwarded. Plain Home/
  End and Shift+Home/Shift+End (no Ctrl) were intentionally left alone —
  still default to reaching the PTY (gated behind `_home_end_native_enabled`,
  default off) since a readline-style CLI has a real use for plain Home/End
  (jump to start/end of the typed command).

- **Ctrl+C copy-vs-interrupt was ALREADY correct** and did not need a fix —
  confirmed by both reading Terminus's Windows keymap (`ctrl+c` →
  `terminus_copy` gated on ST's built-in `selection_empty` context) and by
  live-testing against the user's actual running "Claude" terminal view
  (id 30, via `sublime-mcp eval_python`, NOT a reload — just selecting text
  and invoking the `copy` TextCommand, which the plugin's existing
  `AiTerminalKeyInterceptor.on_text_command` already intercepts correctly
  when `view.sel()` is non-empty). User independently confirmed the same
  result by testing manually. No code change was made for this item.

- **Caret visibility: revisited and FIXED 2026-08-10, superseding the
  entry below this one (kept struck through for the record of how the
  reasoning evolved).** Original call was "selection highlight is visible
  once you're extending one, judged sufficient, caret-color not touched" —
  the user rejected this outright: a selection highlight only exists while
  actively extending one; a plain click, a completed selection's resting
  endpoint, or any moment without an active drag was still a fully
  invisible caret, which was the actual original complaint, not just the
  extend-selection case. User's own framing, verbatim: "if I plant the
  cursor in the response, I want to see it... like I am editing a
  document," then two more hard requirements: "anything a CLI does to
  dictate the position of my cursor should be ignored" / "retain the
  user's control as a default," and "if the CLI want's to know where my
  cursor is, don't tell it."
  Three-part fix, all in `ai_terminal.py`:
  1. `_HOST_CARET_HEX = "#FFCC00"` (amber) replaces the old "caret matches
     background" rule everywhere it was enforced — `_BASE_SCHEME`, the
     on-disk-scheme repair path (`_init_dynamic_color_scheme`), and the
     flush path (`_flush_pending_rules`). `block_caret` stays `False` (thin
     bar, not block) — this now means "normal editing caret shape," not
     "trying to hide it," comment updated accordingly.
  2. `AiTerminalRenderCommand.run` used to snap the caret to the PTY's live
     cursor position (`cursor_offset`/`cursor` args) on every single render
     frame unless there was an active selection — i.e. a CLI's output
     silently yanked the user's cursor back on every frame, exactly what
     was asked to be ignored. Fixed: added `term._last_auto_caret_pos`
     tracking. The render loop now only auto-follows the PTY cursor on the
     very first frame ever, or any later frame where the live caret still
     sits exactly where the render loop itself last placed it. The instant
     the user moves the caret away by any means (click, drag, shift-select,
     native ST nav), `cur_regions != [(last_auto, last_auto)]` goes true
     and it is permanently treated as user-owned from then on — a CLI can
     never claim it back. Logic verified in isolation with 4 scenarios
     (first render / unmoved-since-auto-placement / user-clicked-away /
     active-selection) — all decide correctly.
  3. "Don't tell the CLI where my cursor is" — checked, already true, no
     code change needed: nothing in `ai_terminal.py` ever sends
     `view.sel()`/ST caret position to the PTY. Cursor-position-report
     escape sequences (DSR/CPR, `ESC[6n`) are answered entirely inside
     libghostty-vt using its own internal virtual terminal cursor
     (`TERMINAL_DATA_CURSOR_X`/`_Y`), which has no relationship to ST's UI
     caret at all.
  Compiles clean, 144/144 pytest-passing, NOT live-verified (same standing
  reason as everything else in this baton).

- ~~Caret visibility was raised but NOT changed. The ST caret color is
  still intentionally `#000000` (matches background = invisible) in
  `_BASE_SCHEME`... judged sufficient and caret-color was NOT touched.~~
  SUPERSEDED — see the entry directly above. Kept here only so a future
  reader can see the reasoning was challenged and why it changed, not
  silently rewritten.

**Next step whenever the user is ready:** restart Sublime Text (or trigger
its own file-watcher plugin reload — NOT `importlib.reload()` via
`eval_python`, see [[feedback_no_manual_plugin_reload]]) and verify Shift+
Arrow, Ctrl+Shift+Home/End, PageUp/PageDown, the visible amber caret, and
that the caret stays put where the user leaves it instead of snapping back
to the PTY cursor, against the live session. No urgency was expressed for
this — the user just wants the modifications themselves to keep moving
forward without forcing that restart.
