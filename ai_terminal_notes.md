# ai_terminal Notes

Running technical notes on ai_terminal.py internals that aren't obvious from
the code alone. Newest entries at the top.

## 2026-08-23 — Viewport jump: compensate's arithmetic is right, the open
question is why the same-frame follow-snap didn't override it

Grok Build (session 01a03125) tried gating `_compensate_trim_scroll` to skip
its write during `_auto_follow` True, reasoning that last-row-overflow
evictions shouldn't yank the prompt off screen. Shipped, made live jumping
worse ("alarming... never been this bad" — the ordinary one-line-per-eviction
correction went uncorrected continuously instead of the rare large case).
Reverted same day: unconditional compensation restored, docstring records
why, `tests/test_compensate_trim.py` asserts the original behavior, 51 tests
pass. **Do not re-attempt the auto_follow gate — that lever is closed.**

Grok's own diagnostic harness (wrapped `View.set_viewport_position` /
`_Terminal.send_string` on view id 19) left one real incident captured in
`C:\Users\donal\data\logs\ai_terminal\vp_diag_id19.jsonl` before its
teardown call itself hung 26+ min and had to be force-stopped by the user
(live process confirmed clean afterward — no leftover hooks, revert confirmed
in place via `eval_python`). Re-reading that file cold (not Grok's own
summary of it) surfaces two separate things, not one:

**Finding A — the one real captured jump, arithmetically correct, but a
question the log can't answer.** t=1787535499.54 (19:38:19): `before=[0,
5149]`, `after=[0,4571]`, `dy=-578` (34 lines × 17px lh). Stack trace:
`_do_render` → `AiTerminalRenderCommand._run` → `_compensate_trim_scroll:5466`
→ `set_viewport_position`. `retired_total` genuinely went 1272→1306 (34 real
lines retired that frame, not inferred), so the -578px delta is correct for
what the function computes — eviction shifts every surviving line's buffer
index down by 34 regardless of where vp happened to be sitting, so
`vp - evicted*lh` is the right operation independent of past-end state. Do
NOT "fix" this by clamping vp to `layout_extent - viewport_extent` before
the subtract: worked the algebra on this exact capture and clamping the
baseline down to the legitimate max first makes the landing spot *further*
from true bottom (4484 instead of 4571), not closer — the past-end slack in
vp isn't the defect, so removing it before the eviction subtract only makes
the correction overshoot more.

The real open question: in the SAME `_run` call, right after compensate,
`_settle_viewport` → `_scroll_to_bottom` runs synchronously whenever
`do_follow and not content_fits` (true here — `_tui_like(term)` is False for
Claude, since it never sets alt_screen or mouse_tracking, so this is the
branch, and `real_h≈5883 >> ve[1]=821` so content_fits is False). Given the
numbers in this capture (`_follow_content_height` with
`follow_ignore_trailing_lines=6` → `5883-6*17=5781`; target `= 0 + 5781 -
821 = 4960`), `_scroll_to_bottom` should have overridden compensate's 4571
back to ~4960 in the same synchronous frame — no async gap, no second
render needed. But the log's next two lines (JUMP via `svp`, JUMP via
`sample`, same timestamp) both still show 4571, and the file ends 20s later
on a `sample_err` (`_VPW_LAST_VP` AttributeError killed the sampler right
at this moment) — so there is no way to tell from this file whether
`_scroll_to_bottom` actually fired and got missed by the crashing sampler,
or didn't fire because `do_follow` was false at that instant despite the
snapshot's `state.follow: true` (a `getattr` read at a slightly different
point than the live local `do_follow` inside `_run`). **This is the precise
thing a fresh capture needs to settle** — not "is compensate's math wrong"
(it isn't), but "does `_scroll_to_bottom`'s same-frame snap actually
override a large compensate write, or does something (do_follow being
false, content_fits being true, an exception mid-`_run`) let it stand."

**Finding B — a separate ~0.9s eased burst, unrelated to eviction.**
t=1787535308.75→309.62: vp climbs 4961→5139 (~178px) in ~22 small
interpolated steps. `retired_total`/`len(history)` exactly constant
(1272/300) the whole time — no eviction, so compensate cannot be the
source (its `evicted<=0` early-return would fire). Zero `svp` (our own
viewport-write) log entries in that window either — confirmed by grep, not
inferred — so no plugin code wrote the viewport during this burst. Two live
candidates, not attributed: (1) Sublime's own `view.show()` firing on
focus/hover between renders — already documented at `ai/ai_terminal.py`
~6354-6364 as a real, independent mechanism, and Sublime interpolates
position over several frames when `animate` defaults True, which fits a
22-step easing curve over 0.9s; (2) a real trackpad pan — no `key` events in
the window, smooth motion, and the existing user-scroll detector
(`vp[1] < term._last_vp_y - lh*1.5`) only disengages follow on *decreasing*
y, so a genuine downward pan (increasing y, as observed) wouldn't even
register as a detected user-scroll. `_hover_poll_tick` (~6293) was checked
and ruled out — it only sends synthetic mouse sequences into the PTY, never
touches the viewport.

Do not ship a discriminator for either finding without a fresh, kept-alive
capture across an actual eviction event — the auto_follow-gate lesson above
is exactly what guessing here costs.

## 2026-08-17 — Tab log is the Sublime paint only

User: what I see on the ST tab goes in the log. No JSONL. No other files
read while that is running. No guessing when a TUI line is "done".

After each paint, `SessionTextLog.observe()` gets the same string just
written to the view. Unchanged paints are ignored. A line is appended
when it was not on the previous paint. That is the whole process.

## 2026-08-17 — Session text log: write when it sits still

Superseded the same day. HOLD_S / box-stripping was another guess. Removed.

## 2026-08-17 — Session text log: finished lines, not scroll-off

User: everything the agent puts out gets logged. Scroll-off was the wrong
gate (Grok never retires lines). Per-chunk full-frame dumps were rejected
as a duplicate mess. No menu / extra plugin.

`SessionTextLog.observe()` runs after each PTY chunk (same stream as the
`.cast`). Holds each live line; writes it once when it is replaced, leaves
the screen, or the session closes. Prefix growth is one line. Flicker
shorter than 0.25s is dropped. `_on_retire_line` still writes immediately
for Claude scroll-off; consecutive-dedup covers overlap.

Needs a GhostShell plugin reload (not User/PluginLoader.py) and a new
tab to take effect. Tests: `tests/test_session_logs.py` observe cases;
full suite 416 passed.

## 2026-08-18 — Grok is not Claude scrollback; thinking is not in Tab 2

User corrections after the ST restart (Claude live-verify blocked until 11pm).
Grok session `01a012c9-158a-7732-b7c6-b6cfde409e98`. Also in
`.session-baton.json` under `grok_vs_claude_scrollback_2026-08-18` and
`thinking_not_logged_2026-08-18`.

### Grok vs Claude rendering

Claude is on the primary screen (`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`).
The conversation is real ST scrollback. A width SIGWINCH rewraps and replays
it; that can move the line-number gutter across a digit boundary and feed
the 32↔33 `accepted_cols` attractor.

Grok enters alt-screen (`?1049h`). `force_main_screen` only strips our
display flag. Grok paints one fixed frame. The Sublime buffer is the live
PTY grid (~42 rows). There is **no ST scrollback**. PageUp/PageDown go to
the PTY (`page_keys_to_pty: true`); Tab is Grok's own scrollback mode.
A Grok resize cannot change line-number column width via conversation
replay, because that replay never happens.

Grok's stable command line and statusline are real. They are not a proxy
for Claude's footer jiggle or the resize loop. Do not live-verify those
on Grok.

### "No-alt-screen" is not `alt_screen = never`

`~/.grok/pager.toml` already has `[terminal] alt_screen = "never"`.
`--no-alt-screen` / `alt_screen = "never"` is **still fullscreen**: same
CUP TUI on the primary screen. Grok docs: `--no-alt-screen` still counts
as fullscreen.

The lever that puts finished blocks into native ST scrollback is **minimal
mode**: `grok --minimal`, `/minimal`, or `[ui] screen_mode = "minimal"`.
GhostShell already has profile **Grok Build --minimal**. `config.toml` is
pinned `screen_mode = "fullscreen"`, so the default **Grok Build** profile
stays one-frame. Tradeoff: 16-color palette; `/theme` `/dashboard`
`/timeline` hidden.

**User rejected `--minimal`.** Reason: not all the status indicators
appear. Later the same day: they want **minimal without minimal bells
and whistles** — linear ST scrollback you can mouse, not the
experimental `--minimal` product (16-color, hidden status, hidden
`/dashboard` `/timeline` `/theme`). Grok has no such third mode.
`--no-alt-screen` / `alt_screen=never` is still the fullscreen TUI on
the primary screen (CUP), not Claude-style linear print.

User stated the entire reason for a terminal in ST is to mouse long
responses. Fullscreen Grok defeats that. They PageUp/PageDown and read
Tab 2 (STLogs daily markdown).

### Thinking / findings not logged

User: thinking is lost, not getting logged; findings disappear.

What is actually on disk:

- **Tab 2 / STLogs** (`~/data/logs/YYYY-MM-DD.md`): prompt, final answer,
  tools. No thinking. Grok hooks never fire `AfterModel`. The only
  thinking ingest in `STLogs/lib/ai_log_server.py` is Gemini-shaped.
- **Claude JSONL tailer** (`STLogs/lib/ai_logger.py`): `type == "thinking"`
  is `continue`. Qwen/Gemini flatteners drop `part.thought`.
- **Grok `chat_history.jsonl`**: `type: reasoning` with a short plaintext
  `summary[]` plus `encrypted_content`. Full thinking is not recoverable
  from disk. `events.jsonl` only has `phase_changed` /
  `streaming_reasoning` (no text).
- **GhostShell `SessionTextLog`**: writes lines that permanently leave the
  PTY viewport. Fullscreen thinking is painted then replaced; it never
  retires as ST lines.

STLogs ledger marks Grok Verified on prompt/response/tools. Thinking was
never a completion criterion. Logging thinking is STLogs work, not a
GhostShell render change.

**User 2026-08-18: Grok is not doing JSONL tail logging.** Was correct.
Adapter landed the same day in STLogs: walker takes
`~/.grok/sessions/**/chat_history.jsonl` only; converter handles user
(`<user_query>` unwrapped, synthetics skipped), assistant + tool_calls,
reasoning.summary (not encrypted_content), and tool_result. Same poller
now also tails Junie `events.jsonl`, Kiro `sessions/cli`, Vibe
`messages.jsonl`. Tests: STLogs 15 passed. Live lines appear only after
an STLogs reload and a newly appended record (first-seen offset is EOF).
Do not save GhostShell/`User` PluginLoader.py to activate this.

**Live-verified same evening, next Grok session.** Tab 2 is
`jsonl_tail_transcripts/2026-08-17.md`. User prompt, thinking
*summaries*, reply, and tools appear as the turn happens.

**User will not push `encrypted_content`.** Reason given: likely a
distillation lock. Closed. Do not decrypt, do not scrape the fullscreen
thinking frame, do not add AfterModel workarounds. Summaries are the
intended public trace.

## 2026-08-18 — Resize-loop hysteresis, trim, and follow-bottom trailer skip

Three related fixes. None of them belong in TUI-specific string matching
inside the engine. `PluginLoader.py` was not touched. Uncommitted at
time of writing. Tests: `python -m unittest tests.test_layout
tests.test_trim_display tests.test_render_caret tests.test_terminal_core
tests.test_ghostty_engine tests.test_screen tests.test_parser` → 211 OK.

Long-form diagnosis (cast numbers, CUP overflow, replace_scroll deferral)
is in `ai/TODO.md` under "Status-line resize/rewrap loop". This note is
the "what landed and why it is shaped this way" version.

### 1. `accepted_cols` — do not grow the PTY by exactly one column

**Symptom.** With line numbers on, a streaming tab rewrapped in a ~1s
loop. Line numbers off stopped it. Cast
`ai_2026-08-17_132410.cast` recorded 199 resizes oscillating `32x42` ↔
`33x42` (mean gap 1.253s = `_LayoutWatcher`'s 2-poll × 250ms gate on a
two-state attractor). A later `32→35` is the user turning line numbers
off (releasing a 3-digit gutter).

**Cause.** `_measure` reads `view.viewport_extent()`. Streaming
`view.replace` makes ST relayout (gutter digit width and/or H-scrollbar).
The next poll sees `cols±1`, we `resize`, ConPTY `SIGWINCH`s, the app
repaints, the other measurement wins. The 2-poll gate only kills
*transient* flicker during one resize, not a stable A↔B attractor.

**Fix.** `ai/terminal/layout.py` `accepted_cols(last, measured)`: first
measure is used; grow-by-exactly-1 is ignored; shrink-by-1 is applied
(locks to the size that fits); ±2 or more (real sash-drag) goes through.
`_LayoutWatcher._run` uses it after the existing 2-poll gate.
Tests: `tests/test_layout.py` `AcceptedColsTests`.

**Not done.** 4-digit gutter reserve in `_measure` (would make 99→100
never change measured cols). Hysteresis alone covers both the gutter
and H-scrollbar attractors.

**Live-verify.** After the 2026-08-18 ST restart the module is loaded
(`accepted_cols(32,33)→32` in-process). At 123/124 cols there is no
oscillation (one legitimate `resized PTY to 123x42`). User later dragged
to 29 then 38 with no 32↔33 chase, once the console panel was closed
(`_LayoutWatcher` skips measure while any other panel is active —
`get_console_win` had left the console focused). Narrow ~32-col
streaming test still the strongest remaining check. Do not disable
line numbers to "test" it.

### 2. `trim_display_rows` — do not keep a cursor parked far below content

**Symptom.** Claude's footer jiggled; extra blank / caret on the last
buffer line, retriggered on keystrokes in the TUI prompt.

**Cause.** `_do_render` used `last_real = cy`. Claude CUPs the footer
onto the last PTY row and emits a variable number of `\n` on the
primary screen (`force_main_screen`). Blank rows down to that parked
cursor were kept. After overflow, `❯` has often left the live grid, so
`adjust_display_caret` cannot remap and the ST caret stays on the last
line.

**Fix.** `ai/terminal/render.py` `trim_display_rows(rows, cy)`: keep the
last non-blank row; also keep a blank cursor row only when it is the
*next* line (empty shell prompt). A cursor two or more rows below
content is dropped. `_do_render` calls this instead of `last_real = cy`.
Tests: `tests/test_trim_display.py`.

**Not done.** History append from last-row overflow and empty-2026-then-
home dumps (`update_replace_scroll` / `_sync_scrollback`). Needs its
own failing parser test against verbatim cast tail frames.

**Live-verify.** Idle Claude: caret on the `❯` prompt, not the last
line; one trailing blank is dropped; hist/last_nb/view_rows stable over
a 2s poll. The remaining 1-line type-jiggle is the follow-bottom item
below, not unbounded blank growth.

### 3. `follow_ignore_trailing_lines` — generic snap-to-bottom skip

**Symptom.** Typing snaps the viewport to the bottom (`_auto_follow` +
`_scroll_to_bottom`, Terminus-style, intentional). The last one or two
rows under a custom statusLine appear and disappear per keystroke, so
"bottom" is a moving target and the view jiggles even when wide.

**Cause of the wobble (not a GhostShell bug).** Installed
`ccstatusline` 2.2.22 `probeTerminalWidth()` returns `null` on `win32`
unless `CCSTATUSLINE_WIDTH` is set. Claude Code's statusLine JSON has
no width field. Truncation / extra newlines under that block therefore
do not track the real PTY size. GhostShell does not set
`CCSTATUSLINE_WIDTH`. Upstream-reportable against
`github.com/sirmalloc/ccstatusline`; not a reason to sniff TUI chrome
in this engine.

**Fix (engine stays generic).** `_scroll_to_bottom` subtracts
`follow_ignore_trailing_lines` (int, default **0**) from the follow
height. The engine does not inspect line contents and does not
special-case any TUI. A profile that wants the last N rows left out of
the snap target sets the number. Claude-family profiles are set to `2`
in `ai_terminal.sublime-settings`; every other profile stays at 0.
Helpers: `follow_line_count` in `ai/terminal/layout.py`,
`_follow_ignore_trailing_lines` / `_follow_content_height` in
`ai_terminal.py`. Tests: `tests/test_layout.py` `FollowLineCountTests`.

An earlier draft that matched "auto mode" / "accept edits" / `⏸`/`⏵`
in the engine was removed on purpose.

**Needs a plugin reload** to take effect in a running ST. Do not save
`PluginLoader.py` while other agent tabs are live.

### Do not

- Do not disable line numbers as the resize-loop "fix".
- Do not reduce the `4.0*cw` wrap tax as a width fix (see wrap note in
  `.session-baton.json`).
- Do not put TUI-specific chrome matching in `ai_terminal.py` /
  `layout.py`. Profile settings only.
- Do not start the `update_replace_scroll` pass without a failing
  parser test against the verbatim cast frames.

## 2026-08-05 — Hover-motion (xterm mode 1003) forwarding via OS polling

**Problem:** Textual TUIs (e.g. pybackup's TUI) enable xterm mode 1003
"any-event" mouse tracking to drive hover-highlight — they need a report for
every cell the cursor crosses, with no button held. Sublime's plugin API has
no such event. `EventListener.on_hover` looked like the candidate, but
confirmed empirically (monkey-patching `sublime_plugin.on_hover`, moving the
mouse continuously for 10s) it fires only ~4 times in 10s (every 2.4-5.1s) —
a debounced settle signal, not a continuous stream. Checked LSP's own hover
popup implementation (`LSP.sublime-package` → `plugin/hover.py`) for a
counter-example: it uses the exact same debounced `on_hover` entry point, and
relies on a *native* ST flag (`PopupFlags.HIDE_ON_MOUSE_MOVE_AWAY`) for
move-away dismissal — i.e. it never receives a continuous stream either. So
there's no ST plugin event that fits.

**Fix:** `plugin_host` is a real, unsandboxed Python process, so it can call
Windows APIs directly via `ctypes`, independent of ST's own event dispatch.
Added a self-rescheduling `sublime.set_timeout` loop (`_hover_poll_loop` /
`_hover_poll_tick`, ~line 4763+, next to the existing `_clamp_vp_loop`) that:

1. `_hover_st_hwnd()` — `GetForegroundWindow()` + `GetClassNameW` check for
   `"PX_WINDOW_CLASS"` (ST4's window class, confirmed live via `EnumWindows`).
   Skips the tick entirely if OS focus isn't on an ST window.
2. `sublime.active_window().active_view()` → resolve the `_Terminal`. Skip if
   not a terminal, `mouse_handling` isn't enabled for its profile, or the app
   hasn't requested mode 1003 (`term.screen.mouse_tracking < 1003`).
3. `GetCursorPos` (screen coords) → `ScreenToClient(hwnd, ...)`. Confirmed
   live this client-relative pixel coordinate is exactly the space
   `view.window_to_text()` expects — the same space ST's own mouse-command
   `event["x"]/["y"]` args use.
4. `view.window_to_text()` + `view.rowcol()` → `_view_point_to_cell()`
   (existing helper, shared with click routing) → 1-based PTY cell, or
   `None` if off-grid.
5. Only sends when the cell differs from `_hover_last_cell[view_id]`
   (dedup) — forwards `_encode_mouse(BTN_RELEASE_X10, col, row, press=True,
   motion=True, sgr=...)`, the standard xterm "motion, no button" SGR report
   (`\x1b[<35;col;rowM`). No protocol changes needed — `terminal/mouse.py`'s
   `encode_mouse` already supported this shape.

Poll rate: 33ms (~30Hz), cheap due to the early-exits above. Loop starts in
`plugin_loaded()`, "stops" in `plugin_unloaded()` — though note
`sublime.set_timeout` always returns `None` in this ST version (confirmed
via the API stub), so the `_hover_poll_token`/`_clamp_token`/`_poll_token`
variables are vestigial in this codebase; cancellation is a no-op and the
loops just keep self-rescheduling regardless. `_hover_last_cell` is cleared
per-view in `_Terminal.kill()` alongside `_MOUSE_LAST_CLICK`/`_last_mouse_cell`.

Added `import ctypes` at module top. `BTN_RELEASE_X10` had to be added to
**both** `from .terminal.mouse import (...)` blocks — this file duplicates
its `terminal.*` imports twice (top-level + a nested ImportError-fallback
block for tests/scripts using `ai.*` instead of relative imports) — easy to
patch only one and get an `ImportError` under the other invocation path.

**Known limitation:** only forwards hover for `window.active_view()` of the
OS-foreground ST window. A terminal in a background pane/window while
another view has focus won't get hover motion. Acceptable for the
hover-highlight use case — a user can only be pointing at what's actually
focused/foreground.

**Result:** confirmed working live against pybackup's Textual TUI ("works
fairly well, is quick" — remaining visual quirks in the highlight styling
are pybackup's own rendering, not ai_terminal's).

### Menu wiring fixed in the same pass

The pre-existing "PyBackup Textual TUI" menu item (`Main.sublime-menu`) and
command-palette entry (`Default.sublime-commands`) were wired to a separate
launcher, `launchers/pb_tui_launcher.py`, that opened the TUI via **Terminus**
— a plugin that isn't installed, so the command silently no-op'd (both the
primary and fallback code paths called `terminus_open`). Repointed both
entries to `ai_terminal_open_here` with `args: {"profile": "Pybackup Textual
TUI"}` — that profile already existed in `ai_terminal.sublime-settings` with
`mouse_handling: true, force_main_screen: false`. Removed
`launchers/pb_tui_launcher.py` and its import in `PluginLoader.py`.

### Deploy caveat

Mirroring an edited `PluginLoader.py` into the live `Packages\User` tree
triggers a **full Sublime Text restart** — unlike `ai_terminal.py`, which
reloads surgically via `sublime_plugin.reload_plugin("User.ai.ai_terminal")`
with no restart. `PluginLoader.py` is the top-level loader ST rescans
wholesale on any change. Warn before deploying a `PluginLoader.py` edit live,
especially mid-session.
