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

- [ ] Not applicable as-is: GPU/Metal/OpenGL rendering, custom shaders,
      native macOS/GTK shell, quick-terminal dropdown, terminal inspector
      GUI, sixel/Kitty graphics protocol, tmux integration, split-pane
      windowing, crash telemetry — these all rely on Ghostty's standalone
      GPU-surface app model, so none of that code carries over to rendering
      inside a Sublime Text buffer. Equivalent features (e.g. split panes,
      session persistence) aren't ruled out — they'd just have to be built
      new against Sublime's own APIs rather than reused from Ghostty. No
      action taken; logged for scope reference only.

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

### Follow-up (2026-08-10): plain Up/Down/Left/Right still "tied to" the PTY when off the command line

After the fixes above were live-verified (restart happened), a new complaint
surfaced: doing typical ST-style selection/movement in *response* text — not
the live command line — still behaves like it's on the command line. Plain
arrow keys (no modifiers) are forwarded to the PTY unconditionally in
`AiTerminalKeypressCommand.run` regardless of where the ST caret actually is
(see the block right after the Ctrl+Shift+Home/End handling, ~line 4726 in
`ai_terminal.py`), so Up/Down from inside scrollback/response text still
triggers the shell's readline history recall instead of moving the ST caret.

**Root-cause research (web + reading `~/tools/ghostty` source directly):**
there is no reliable signal to consume here, from either side.

- Real ghostty only knows "where the command line is" via **OSC 133**
  semantic-prompt markers (`ESC]133;A/B/C/D`) emitted by the *shell's own*
  integration script (`~/tools/ghostty/src/terminal/osc/parsers/
  semantic_prompt.zig`, `page.zig`'s `Row.SemanticPrompt` enum). Ghostty does
  not infer prompt boundaries from cursor position at all — it is 100%
  dependent on the child shell cooperating.
- Our own libghostty-vt bindings (`ai/terminal/ghostty_vt.py`,
  `ghostty_engine.py`) do not parse or expose OSC 133 / semantic-prompt state
  at all today — there is nothing to read even if the child emitted it.
- More importantly: it wouldn't apply to this case anyway. OSC 133 is a
  *shell* prompt protocol; a live Claude Code CLI session isn't a shell
  prompt, it's a TUI managing its own input box. Confirmed via `gh issue
  view` (not just search summaries, which overstated this) on
  anthropics/claude-code#1465, #22528, #26235, #32635 — all *requesting*
  Claude Code emit OSC 133 for exactly this "which row is the input" gap,
  all closed by the inactivity bot with **zero maintainer engagement** over
  more than a year, none implemented. One commenter on #26235 tried writing
  iTerm2 mark sequences directly to `/dev/tty` from a hook as a workaround —
  the Claude Code TUI swallowed the escape sequence entirely, and marks set
  via OS-level menu automation got wiped by the TUI's own redraws. So Claude
  Code's TUI doesn't just fail to emit boundary markers, it actively
  discards externally-injected ones too.

**Conclusion:** there is no oracle to consult — this is a structural gap in
Claude Code's TUI itself (and terminals generally, absent shell
cooperation), not a missed API call on our side. The best available
approximation, if this is ever revisited, is a heuristic already in the same
spirit as `_tui_like()`: compare the ST caret's row against the PTY's own
live cursor row (already tracked every render via `term._last_caret_off` /
`term.screen.y`) and only forward plain arrows to the PTY when they match.
Known weakness: a wrapped multi-row command line would make Up/Down
ambiguous for some of its rows. Not implemented — logged here so it isn't
re-litigated from scratch; no action taken.

### Follow-up (2026-08-10): resolved via cmux-style copy-mode toggle, plus a feature backlog

Researched `~/tools/cmux` (a larger, more mature terminal-multiplexer project
that also embeds Ghostty) for how it handles the same "is the cursor on the
live command line" ambiguity, and for other easy ports.

- [x] **Copy-mode toggle** — DONE 2026-08-10. cmux doesn't use a caret-vs-PTY-
      row heuristic either; it sidesteps the ambiguity entirely with an
      explicit mode switch (`toggleTerminalCopyMode` keybinding). Ported the
      same idea instead of the heuristic sketched above: `ctrl+alt+c`
      (`Default.sublime-keymap`) runs the new
      `AiTerminalToggleCopyModeCommand` (`ai_terminal.py`), which flips
      `term.copy_mode` (new `_Terminal.__init__` attr, default `False`).
      While on, `AiTerminalKeypressCommand.run` intercepts plain (no ctrl/
      alt) up/down/left/right/pageup/pagedown/home/end — including with
      shift, for selection — and routes them to native ST `move`/`move_to`
      instead of the PTY (new block right before the existing Shift+Arrow
      handling). Escape exits copy mode and re-pins the viewport to the
      live prompt via `_scroll_to_bottom` + `term._auto_follow = True`,
      same as toggling the command again.

      **Live-verified 2026-08-10, and it broke on first contact**: after the
      user actually restarted ST to test this (and the rest of the baton
      below), ai_terminal's PTY input was completely dead — plain typed
      characters did nothing, only nav keys worked (as native ST movement).
      Root cause was an addition that went beyond the copy-mode toggle
      itself: `AiTerminalKeypressCommand.run` had also been gated on a
      passive `caret_detached` signal (`view.sel()` not matching
      `term._last_auto_caret_pos`) *in addition to* `term.copy_mode` — and
      when detached, **every** key was swallowed, not just navigation.
      Confirmed live via `sublime-mcp eval_python`: `term.copy_mode` was
      found `True` on the live session for an unknown reason (init sets it
      `False`, never reproduced how it flipped), and even after forcing it
      back to `False`, `_last_auto_caret_pos` was stuck stale (`0`) against
      the real selection (`1601`) — any PTY-driven scrollback trim/redraw
      shifts absolute buffer positions, so this equality check drifts
      after nearly any render and re-locks the swallow-all-keys path with
      no visible feedback (status message scrolls away instantly). A real
      terminal forwards typed input to the child process regardless of
      caret position — that's not optional terminal behavior, so the
      passive auto-detach gate was flat wrong to add.
      **Fixed**: removed `caret_detached` from the gate entirely —
      `AiTerminalKeypressCommand.run` now only enters the ST-domain
      nav/swallow block when `term.copy_mode` is `True` (i.e. only via the
      explicit `ctrl+alt+c` toggle or Escape to exit it). Applied first as
      a live in-process monkeypatch (`sublime-mcp eval_python`, replacing
      `AiTerminalKeypressCommand.run` on the loaded class — NOT
      `importlib.reload()`, see [[feedback_no_manual_plugin_reload]]) to
      unblock the user's live session immediately, then landed the same
      fix on disk in `ai_terminal.py` so a real restart picks it up too.
      Confirmed working live afterward: plain typing reached the PTY again
      on the command line. `caret_detached`/`cur_regions` are no longer
      computed at all in this method — only `last_auto` survives, used
      solely by the Escape-while-copy_mode branch.
  - Also surfaced: cmux's Rust bindings (`cmux-tui/crates/ghostty-vt/
    src/terminal.rs`) suggest libghostty-vt itself may persist OSC 133
    semantic-prompt state per row (`GHOSTTY_ROW_DATA_SEMANTIC_PROMPT`,
    queried via `ghostty_row_get`) — separate from cmux's own live-typing
    `PromptSemanticTracker`. This may mean the "our bindings don't expose
    OSC 133 at all" premise above is only half true (the C library might
    track it; our ctypes layer in `ghostty_vt.py` just doesn't wrap the
    call yet). Not verified against the actual header — worth checking
    before ever building the row-comparison heuristic for real, since a
    real semantic-prompt signal would be strictly better than a heuristic.

- [ ] Other easy-to-port features/intelligence spotted in cmux, not yet
      started, roughly ranked:
  1. Port cmux's small `PromptSemanticTracker` OSC-133 write-path hook
     (`terminal.rs`, ~50 lines, `finish_osc` callback mapping `A/N/P→Prompt`,
     `B→Input`, `I→InputUntilEndOfLine`, `C/D→Output`) into
     `ghostty_engine.py`, once/if the libghostty-vt exposure question above
     is resolved.
  2. Command-block segmentation (`OSC133CommandParser.swift`, a pure
     idle→prompt→command→output state machine) — could power a "jump to
     previous/next command" navigation in scrollback.
  3. CR-redraw line folding (`foldLine`) — collapses `\r`-progress-bar spam
     to its final state; useful for `render.py` copy/log-export so a
     copied progress bar isn't hundreds of stale redraw lines.
  4. Auto-scroll suppression once the user manually scrolls up
     (`userScrolledAwayFromBottom` in `GhosttyTerminalView.swift`) — close
     to `term._auto_follow` already, may just be a naming/behavior gap
     check against what we have.
  5. Exit-code-based failure flagging (`TerminalCommandBlock.failed`) — a
     gutter marker for failed commands, cheap once (2) above exists.
  6. Bounded escape-sequence buffering (`maxEscapeLength`) — a defensive
     cap worth copying into GhostShell's own escape parser so a malformed/
     huge OSC sequence can't grow a buffer unboundedly.
  Alt-screen-based "always forward arrows" was also on the original
  candidate list but turned out to already be existing behavior here
  (nothing intercepts plain arrows in `_tui_like()` alt-screen apps today) —
  no action needed.

### Follow-up (2026-08-10): triaged the backlog against GhostShell's actual code

- Item 1 (OSC 133 row tracking): confirmed real — checked
  `~/tools/ghostty/include/ghostty/vt/screen.h` directly.
  `GHOSTTY_ROW_DATA_SEMANTIC_PROMPT` / `ghostty_row_get()` exist and are
  unwrapped in our `ghostty_vt.py`. But the C API only distinguishes
  prompt vs. non-prompt rows (no input/output split like cmux's own
  tracker), and Claude Code's TUI still never emits OSC 133 at all (see
  the gh-issue research two sections up) — so wrapping it would only help
  plain-shell profiles (bash/cmd), not the primary Claude Code case that
  copy-mode above already fixed. Demoted in priority; not started.
- Item 4 (auto-scroll suppression): already implemented. `term._auto_follow`
  (`_Terminal.__init__`, flipped throughout `ai_terminal.py`) is exactly
  cmux's `userScrolledAwayFromBottom` pattern, just inverted naming. No
  work needed — removed from the backlog.
- Item 6 (bounded escape-sequence buffering): not applicable. GhostShell
  doesn't hand-parse escape sequences — the real libghostty-vt C library
  does that. This was a concern specific to cmux's own hand-rolled Swift
  OSC parser layer (`OSC133CommandParser.swift`), which GhostShell has no
  equivalent of. Removed from the backlog.

Remaining real candidates, in original rank order: item 2 (command-block
segmentation / jump to prev-next command) and item 3 (CR-redraw line
folding for clean copy/export). Neither started.

## Click-to-reposition fallback session baton (2026-08-11)

Restart test after the caret-visibility work above surfaced a new gap:
clicking with the mouse to plant the cursor mid-text on the live command
line did not move the TUI's own cursor — only ST's local selection moved.
Root cause: over a plain PTY there is no "set cursor to column N" from
outside; an app only moves its own cursor via keystrokes it recognizes, or
(if it opted in) DEC mouse-tracking reports (`CSI ?1000/1002/1003h`) it
parses itself. `_route_mouse_click` (`ai_terminal.py`) already handles the
tracking-app case; nothing existed for apps with no tracking at all.

**Surveyed which agents actually support DEC mouse tracking** by replaying
all 470 recorded asciicast sessions under
`~/data/logs/ai_terminal_asciinema_casts_for_troubleshooting_rendering`
(613 MB) and regex-matching `CSI ?<modes>h/l`, including the combined-
parameter DECSET form (`?1003;1006h`) that a first-pass regex missed and
had to be corrected for (caught Grok/jcode as false negatives initially).
Findings recorded as per-profile comments in `ai_terminal.sublime-settings`:

- **Never enable tracking** (no PTY-side receiver exists, ever): Claude
  Code, Gemini CLI, Antigravity (`agy`), Codex, Kimi, Kiro CLI, Junie.
- **Do enable it** (real mouse-report path, already handled by
  `_route_mouse_click`): Grok (`?1003;1006h`), jcode (`?1003;1006h`),
  OpenCode (`?1000/1002/1003/1006h`), Mimo (`?1000/1002/1003/1006h`),
  Qwen-code (`?1002/1003/1006h`), gotui (`?1002/1003/1006h`), Vibe
  (`?1000/1003/1006h`).

**Implemented and committed** (`c3d0860`, on `main`, 1 ahead of
`origin/main` — not pushed): `_route_click_to_cursor_fallback()`
(`ai_terminal.py`, near `_event_to_pty_cell`) synthesizes Left/Right
keypresses (via the existing `_get_key_code`, DECCKM-aware) to move the
app's real cursor to the clicked column — but ONLY when the hardware
cursor is already confirmed sitting on the live `>` prompt row (via
`caret.py`'s `find_prompt_row`/`input_start_col`/`field_right_limit`, so it
never guesses off a footer-parked cursor during a spinner). Wired into
`AiTerminalKeyInterceptor.on_text_command`'s `drag_select` handling, gated
to fire only when the click will NOT be forwarded as a real DEC mouse
report (covers both `mouse_handling` off — the default/every profile above
that doesn't override it — and the rarer case of an app that hasn't
enabled tracking yet). Compiles clean, existing 51/51
`tests/test_terminal_core.py` pass.

**Live-verified 2026-08-11**: after restarting ST, click-to-reposition on a
live Claude Code prompt line tested and passes — clicking mid-text moves
the real app cursor to the click point as intended.

## Debug instrumentation baton (2026-08-11) — ROOT-CAUSED AND FIXED

Two TEMP DEBUG blocks were added to `ai_terminal.py` to chase two separately
reported bugs. An independent code review of the pending commits (not the
instrumentation's own output — neither bug had reproduced yet under
logging) spotted both root causes directly in the render/selection code, so
both are now fixed and the instrumentation removed:

1. **Paint freeze** ("backspace reaches the live app but the ST view stops
   visually updating until a later keypress, then catches up all at once"):
   `_selection_paint_blocked` blocked repainting for *any* non-empty
   selection with no time bound at all. Fixed: bounded to 2.5s
   (`_SELECTION_PAINT_BLOCK_MAX_S`) — a stale/abandoned selection can no
   longer freeze the view indefinitely.
2. **Spurious copy_mode-adjacent caret freeze** (reported as copy_mode
   toggling on its own during Claude responses/permission prompts): the
   real bug was one level down — `on_selection_modified` latched
   `term._user_owns_caret = True` unconditionally whenever
   `_command_line_row_range` found no drawn box, which is the *normal* case
   for plain shells (cmd.exe/PowerShell/bash draw no box at all), so the
   very first selection event in such a shell froze the caret permanently.
   Compounding it: `_do_render`'s own `_clear_view_selection` calls and the
   copy-mode Escape handler's `sel.clear()/add()` fired
   `on_selection_modified` as an unguarded side effect, re-latching the
   same flag. Fixed: `on_selection_modified` now falls back to a new
   `_live_cursor_row()` helper (hardware cursor row comparison, resurrected
   from the logic removed in `0b7aa19`) instead of latching when no box is
   found, and both internal selection mutations are now guarded by
   `term._in_render` so they're never mistaken for a user gesture.

## Copy-mode "turns on by itself" report (2026-08-11), Kiro profile — UNRESOLVED

User reported testing the Kiro profile and hitting copy mode ON without a
deliberate ctrl+alt+c press, needing ctrl+alt+c (the toggle) to get back to
normal command-line typing. Investigated live via `sublime-mcp`:

- Confirmed by full-file grep: `term.copy_mode` is only ever set `True` in
  `AiTerminalToggleCopyModeCommand.run` (`ai_terminal.py`), and that command
  has no menu/palette entry and no other `run_command("ai_terminal_toggle_copy_mode")`
  call anywhere in the repo — the only way to reach it is the literal
  `ctrl+alt+c` keybinding (`Default.sublime-keymap`).
- Ruled out via user confirmation: not a deliberate press, not an AltGr/
  accented-character composition (which on Windows arrives as a synthetic
  ctrl+alt+<letter> keydown and would explain a silent trigger) — plain
  ASCII typing.
- Ruled out by reading the code: cursor/caret-placement logic
  (`_command_line_row_range`, `_live_cursor_row`, `on_selection_modified`)
  only ever touches `term._user_owns_caret`, a cosmetic caret-snap flag —
  never `term.copy_mode` — so "cursor planted off the command line" cannot
  by itself flip copy_mode, contrary to the user's working theory.
- Checked `ai_terminal.sublime-settings` and `_Terminal.__init__` for a
  startup-default explanation (user's other theory: "a setting which is on
  at startup") — `self.copy_mode = False` is hardcoded, no settings key
  feeds it.
- The Kiro tab from the session that triggered this was already closed by
  the time this was investigated, so no live state or console evidence from
  the actual incident was recoverable; `get_console_log` showed nothing
  copy-mode-related, just normal spawn/usage-sweep lines.

**Not fixed — root cause not found.** Added TEMP DEBUG logging (call-stack
dump) to `AiTerminalToggleCopyModeCommand.run`, gated on the ON transition,
to catch the actual trigger next time this reproduces. Remove once
root-caused.

Also fixed in the same pass (found during the same review, all in
`ai_terminal.py`):
- `_route_click_to_cursor_fallback` now reads `term.screen` under
  `term._lock` (every other grid consumer already did) and no-ops on an
  implausible delta instead of risking a torn read / large visible cursor
  jump.
- `_BOX_BORDER_CHARS` widened to sharp/heavy/double corner glyphs, and a
  border row now only needs ≥60% border chars instead of 100% — a titled
  border like `╭─ Claude ─────╮` used to fail to match, which fed back into
  bug 2 above via a false "no box" read.

103/103 tests pass, compiles clean. Findings #3/#4 from the same review
(a `kill()`/handle-leak and a `_close_handles`/exit-watcher race, both in
the PTY teardown path) were deliberately NOT fixed in this pass — an open
Devin PR (`devin/1786491335-error-handling`) rewrites that exact region
(`start`/`_watch_process_exit`/`read`/`write`/`_close_handles`), so fixing
it here risked a merge conflict. Revisit once that PR is reviewed/merged
or closed.
