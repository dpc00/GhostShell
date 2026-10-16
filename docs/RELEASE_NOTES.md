# Release notes


## 0.2.10

**Fixed:** a tab did not close itself after its program exited (for example after `/exit` in Claude Code), although `close_tab_on_exit` was on. When the broker ended the session, the tab's read of its pipe failed with WinError 233, and Python 3.14's asyncio crashed handling that error (`'_ProactorReadPipeTransport' object has no attribute '_empty_waiter'`) without telling the tab the session had ended. The tab now gets the end of the session and closes itself 1.5 seconds later. The thread-free client that introduced this came with 0.2.7.

**Fixed:** Move to Windows Terminal no longer asks for approval; the move is harmless, the session keeps running. Recover Session now takes a session back from a Windows Terminal window: it asks the broker to disconnect that window, waits, then attaches. Before, it connected into the busy pipe, reported the live session as gone, and left an empty tab while the session stayed in Windows Terminal. The picker row says "busy in another window — picking it takes it over".

**Fixed:** reattaching to a session (Recover Session, a plugin reload, a restart) could draw a repeated banner and overwrite text when the tab was not the width the session had been. The broker's saved output mixes screen draws made at different widths, and the tab replayed them all at one width. The broker now records each resize in its saved output, and the tab resizes its screen at the same points during the replay. Sessions started before the update have no records and replay as before.

**Fixed:** Recover Session listed nothing for a tab that had died but still had its view open; it now lists such a tab. List Sessions no longer lists sessions that have already ended.

**Fixed:** after a plugin reload, a detached tab said "session kept alive, still running" even if the session had ended meanwhile. It now checks, and says the session had already ended.

**Changed:** closing a tab with Ctrl+W, the tab's close button or the Close File command is no longer blocked on GhostShell tabs. A plain close detaches the session (it keeps running, and Recover Session brings it back); it does not kill it. Kill Session, End Session and Close Tab (Keep Session Alive) work as before.

- Restart Sublime Text after updating: the changed broker is under `tools/` and a plugin reload does not load it. Sessions already running keep their old broker.
- Tested on Windows 11 with the installed Sublime Text (build 4215, Python 3.14): natural exit, an outside kill, Kill Session, End Session on a live and a dead tab, Close Tab (Keep Session Alive) with Recover Session, a plugin reload, Move to Windows Terminal and back, Detach All and Reattach All, and a full Sublime restart. Also on a portable build 4200 (Python 3.8): the same list, and on the final code natural exit, typing, detach with Recover Session, and a plugin reload.
- Known flaw, not fixed: in a very narrow tab (about 20 columns) on build 4200, text typed after a plugin reload was drawn into the middle of the prompt row. Tabs at the normal width (94 columns) did not show it, and it is not caused by the width records above (it also showed before them). Not investigated further.

## 0.2.9

**Changed:** each Sublime Text keeps the records of its own sessions in its own data folder, `<data folder>\Local\GhostShell\broker_sessions`, instead of one machine-wide folder (`%LOCALAPPDATA%\GhostShell\broker_sessions`) shared by the installed Sublime and every portable copy. With the shared folder, Recover Session and List Sessions in a portable Sublime listed the installed Sublime's sessions, and Reattach All From Windows Terminal could take over a session that belonged to another Sublime (it asks a busy session to disconnect, which would have detached the other Sublime's tab). Now a Sublime lists and reattaches only its own sessions.

- Sessions already running when you update keep their record in the old folder, and the installed Sublime still reads that folder until they end; no new record is written there. A portable Sublime does not read it.
- The `broker_registry_dir` setting still overrides the folder.
- `tools/recover_console.py` takes `--registry-dir` for its own session lookup; Move to Windows Terminal always names the session, so it is not affected.
- Tested with the installed Sublime (build 4215) and a portable build 4200 side by side: each saw only its own session, and Reattach All inside the portable left the other Sublime's tab attached.

## 0.2.8

**Fixed:** with 0.2.7, typing did nothing in a tab on Sublime Text builds that run plugins on a real Python 3.8 (build 4200 and earlier): the keystrokes never reached the session, and the console showed "got Future attached to a different loop". The write queue was created before the plugin's event loop existed, which Python 3.8 binds to the wrong loop. It is now created on the loop. Found by running 0.2.7 in a portable build 4200, which I should have done before releasing it.

Tested on Python 3.8.12 (build 4200): opening a tab, typing, detaching with Close Tab (Keep Session Alive), Recover Session with its replay, resize, a plugin reload with the tab open, Relaunch (a new broker replaced the old one), Move to Windows Terminal (the relay was given the tab's size, 73x29) and back with Recover Session, and End Session. Also still working on build 4215 (Python 3.14).

## 0.2.7

**Changed:** GhostShell no longer runs a thread of its own for the terminal. Each tab used to have a reader thread and a writer thread, the broker that keeps a session alive had five, and timers, session discovery and the Windows Terminal ConPTY download each started threads too. All of that now runs on one asyncio loop that Sublime Text's main thread steps, and the broker (`tools/agent_broker.py`) is one asyncio program. Typing, output, resize, Close Tab (Keep Session Alive), Recover Session, Kill Session, End Session and New Session work as before. The cast recorder and the session text log keep their own thread on purpose, so a slow disk never holds up the editor. A profile with `"detachable": false` still uses the old direct ConPTY and its threads.

- Output is parsed on the main thread now. A 30,000-line burst stalls the editor no more than it did with threads (measured against the old client on the same burst).
- Reloading the plugin detaches each tab and reattaches it, as after a restart; the session itself keeps running.
- Tested on Windows 11 with Sublime Text build 4215, where a 3.8 plugin runs on the 3.14 host. Older builds with a real 3.8 host were not tried.

**Fixed:** Move to Windows Terminal opens its window at the tab's size and keeps the session at that size. Before, the session was resized to the Windows Terminal window, so what the program drew there (boxes, rules) was laid out at another width when the tab came back, and the conversation showed smeared frames and over-long lines.

**Fixed:** the status tones (setting `status_tones`) no longer play note by note with gaps. They are rendered once to a small sound file and played by Windows, so they come out faster and smoother. A plugin reload no longer makes the tone player raise "release unlocked lock".

**Fixed:** `auto_approve_permissions`, `modern_conpty`, `scroll_manipulation_enabled` and `tsp_aside_column` were reported as unknown profile settings every time the plugin loaded, although they work. They are known now.

**Changed:** Recover Session, List Sessions, Reattach All From Windows Terminal and the Windows Terminal ConPTY download no longer make the editor wait while they look for sessions, ask a busy pipe to free up, or download the 1.7 MB.

Restart Sublime Text after updating instead of reloading the package: a plugin reload loads `ai_terminal.py` but not the files under `terminal/` and `tools/`.

**Added:** the setting `tsp_tab_status` (off by default, on in the omp profile) and its companion `tsp_tab_prefix` (omp: `π`). omp leaves its run state and session name out of the window title when it draws through the Tern Surface Protocol, so its tab showed only the profile name. With `tsp_tab_status` the tab is named the way omp names its own title in a plain terminal: the prefix, the run state, then the session name.

- The run state comes from omp's Program Status Protocol (OSC 7501) reports: a spinner frame while omp is working, `!` while it waits on you, a check mark when a turn is done, a cross on an error, `>` when idle. omp's own title shows done and error as `>`; the tab does not. The cross has not been seen in testing.
- The session name is shown exactly as omp sends it, with the icon if it has one. It comes from omp's card-form title in the console title (`<icon> CODE: title`) or from the `<name>: Complete` notice omp sends at the end of each turn, which also carries a hand-typed `/rename`. The profile name shows until omp has sent one.
- The titles that omp's child processes write to the same console (`npm exec …`, `cmd.exe`) are not used as the session name.
- A hand-typed rename shows at the end of the next turn, not at once.
- Takes effect for tabs started after the change. Tested on Windows with omp 18.11.0 only.

`terminal/tsp_view.py` gained an optional `on_state` callback that the native view calls after each draw. Restart Sublime Text after updating, because a plugin reload loads `ai_terminal.py` but not `terminal/tsp_view.py`, and the omp tab then falls back to plain text.


## 0.2.6

**Fixed:** the switches, steppers, drop-downs and Reset buttons on omp's `/settings` page did nothing when clicked. Every one of them now sends its change to omp. Reset puts a setting back to its default.

**Fixed:** coloured text from a program that draws through the Tern Surface Protocol (for example green "ok" marks) was drawn black on the dark panel. Programs that do not set their own colours now get the standard ANSI palette.

**Fixed:** the Claude tab no longer jumps past the end of its text after a large block of old output is trimmed.

**Changed:** omp's `/settings` page opens at its top, with its title, page list and search box in view, instead of at its end. Typing in its search box no longer pulls the view to the bottom. The page stays inside the omp tab: the window's groups and layout are not changed.

**Added:** the setting `tsp_aside_column` (off by default). When it is on, the Settings page opens in a column of its own beside the tab, as the Tern protocol describes, and closes again with the tab layout restored. It adds a group to the window, so it is off unless you ask for it. Takes effect for tabs started after the change.

**Added:** much fuller support for the Tern Surface Protocol, so programs other than omp can draw rich output in a tab: plain HTML elements and forms with checkboxes and radio buttons, images and command blocks, style sheets, output that stays in the scrollback after the program ends, scrolling and jumping to an item in a list, and announcing the terminal's colours and reduced-motion setting. A program that opens a surface with `listen:false` no longer gets any answers sent into its input.

Restart Sublime Text after updating instead of reloading the package. Once, after a detach and reattach that followed a code change, an omp tab showed raw protocol text until Sublime was restarted.

## 0.2.5

**Fixed:** in omp's native view, a view that the reader pushed below the last line while omp is working could still be thrown back to the ordinary bottom by a redraw. The view is now put back where the reader left it.

## 0.2.4

**Fixed:** in omp's native view, a reader who scrolls below the last line to put the input box in the middle of the screen keeps that place; the view no longer snaps back to the bottom on the next redraw. Typing no longer moves the view while the input box is completely on screen. When the input box is off screen, typing still returns the view to it.

## 0.2.3

**Changed:** the omp profile no longer sets `tsp_animation_ms` to 0, so omp's spinner and running-time counters animate again in the native view (four updates a second while omp is working). 0.2.2 shipped with the animation off; the scroll fixes in 0.2.2 were then tested with the animation on. Remaining rough edges: scrolling up while omp works can still jitter now and then, typing returns the view to the input box by design, and the view cannot be scrolled past the last line.

## 0.2.2

**Fixed:** in omp's native view, scrolling back no longer snaps to the bottom on every spinner tick or while `/usage` is open. A reader who has scrolled up keeps their place, and the scroll to the bottom that follows an overlay opening stops as soon as the reader scrolls.

**Fixed:** after Sublime restarts, an omp tab that was idle no longer shows the replayed raw output until a key is pressed. The tab now asks omp to send the whole conversation again straight away.

**Added:** moving an omp session to Windows Terminal now shows the conversation as text. Windows Terminal does not speak the Tern Surface Protocol, so the window used to stay blank or fill with escape codes. Each panel is written once to the window's scrollback, and only the newest panels, an open overlay and the input line are redrawn. Panels are plain text: no colours or images.

**Fixed:** the omp input box showed its placeholder as raw list text, and a completion appeared one column to the right of the caret in the Windows Terminal view.

**Added:** the omp tab registers its panels so editor tools that list a view's phantoms (sublime-mcp `get_view_phantoms`) can read the tab as text.

**Added:** optional view-jump tracing (`debug_view_trace`, off by default, switches itself off after `debug_view_trace_hours`), status tones when a program reports a status change (`status_tones`), a settings toolbar that lists every on/off setting, and an auto-approve that waits for a pause before it answers a permission prompt.

**Changed:** the omp profile sets `tsp_animation_ms` to 0, so omp's spinner and clock are not animated in the native view. Set it to 250 to bring the animation back.

## 0.2.1

**Added:** a Muse profile (Muse Code CLI) with entries in the Ai Terminal menu and the side bar menu. It is untuned: no mouse or page-key behaviour is claimed for it.

**Fixed:** when Claude Code redraws its whole conversation in one frame, a viewer who was at the end of the tab is no longer left a quarter of the way down it; the view follows the new end. A viewer who has scrolled further up keeps their place.

**Fixed:** a small drift of the view (a palm brushing the trackpad, a layout shrink, typing after pushing the tail up) no longer switches off following the end of the tab. Following stops only when the view is well above the end.

**Fixed:** Claude Code hides the terminal cursor and parks it in the bottom-right corner while it redraws the spinner or footer. The caret is no longer moved there, which had put it over the status line and scrolled the view to it.

## 0.2.0

> **Note (2026-09-21):** the owner deleted the whole `tests/` folder that day. References to tests in entries dated on or
> before then describe what was checked at the time; they are not ongoing protection. Changes are now proven live in Sublime
> (`AGENTS.md`, rule 7a).

**Added:** a newer ConPTY for profiles that set `tsp_enabled` (the omp native view). Windows' built-in ConPTY
swallows the questions omp asks at start-up, so that view never started. GhostShell now downloads Microsoft's own
`Microsoft.Windows.Console.ConPTY` NuGet package once (x64 only), checks the package hash and then each file's hash,
and keeps `conpty.dll` and `OpenConsole.exe` in `terminal/bin/conpty/`. A wrong hash, no network or a non-x64 CPU
prints one line and the tab uses the built-in ConPTY. Provenance: `terminal/CONPTY_PROVENANCE.md`.

**Added:** the omp native view through the Tern Surface Protocol. Experimental, opt-in per profile, not recommended.

**Added:** a `log_root` setting that sets where GhostShell's own logs go. With it unset nothing is logged and the code
fails loudly rather than guessing a location. The broker lifecycle log is opt-in via `GHOSTSHELL_BROKER_LOG`.

**Changed:** mouse routing follows Ghostty, and Text Edit Mode also takes the mouse. Font settings default to
Sublime's own font (the dead `terminal_font` setting is gone). The Ai Terminal menu is at the top level. Claude,
Auggie and Vibe profile tuning is now shipped instead of living only in the owner's User override. The Gemini profile
was removed because it does not work.

**Fixed:** the typing jiggle and status-update jiggle (only changed lines are redrawn, steady tab height), hover or
focus snapping a scrolled tab to the top, Nuke never clearing the terminal engine, the GhosttyError crash while
building its own message, a rewrap taking about 7 s after hiding the minimap, and leftover files from brokers that
died without cleaning up.

**Removed:** unit tests, notes, plans and scratch files from the shipped package.

**Removed:** all usage and quota features. The scanner read other CLIs' saved OAuth credentials, called the
providers' usage endpoints every 20 minutes and, for Claude Code, rewrote `.credentials.json` when refreshing a
token. GhostShell is not an authorised app with those providers, so it no longer does any of this. The
`usage_scan_enabled` and `usage_refresh_minutes` settings and the "Refresh Usage & Quota" command are gone.
The Launch Agent list, the menu captions and the session-info view no longer show percent remaining, reset times,
"quota exhausted" or "no usage data". An agent row shows only whether its program is installed. Use `omp usage`
for real quota figures. A test (`tests/test_no_usage_in_code.py`) fails if usage code is ever added back.

**Changed:** an agent whose program is not installed is no longer listed on the Launch Agent list, and its menu
entries are hidden. Previously it was listed and marked "Not installed". The Cody profile was removed.

**Changed:** agents are launched from the **Ai Terminal > Agents** and **Ai Terminal > Shells** submenus, sorted A-Z,
instead of a picker. Agents that are not installed are hidden. The "Launch Agent…" picker, its palette entry and its
`Ctrl+Alt+N` shortcut are gone, and so are the picker code, the folder picker that went with it, the "Default Profile" item and the sidebar's
"Open Ai Terminal here…". The same two submenus are in the sidebar's right-click menu. The submenus are
built ahead of time by `tools/regen_agent_menu.py` (Sublime has no API for adding menu items while it runs), and a test
fails if the menu and the agent list ever differ.


## 0.1.7

**Bug fix:** GhostShell still sent a 1-row `SIGWINCH` when Sublime's
viewport height wobbled by ~15px (H-scrollbar or `int(h/lh)-1`).
0.1.6 removed the extra emoji cell; the row count could still flip
47↔48, and omp still dumped the conversation.

`accepted_rows` now ignores a 1-row change in both directions, matching
the live pin that stopped the loop. A real window drag (≥2 rows) still
resizes the PTY.

An inline OpenUri phantom after a full-width URL can still steal one em
and pop the H-bar. That is OpenUri's `"show_open_button": "always"`,
not this package. `"hover"` keeps the button without parking an icon
on the line. Do not enable OpenUri `"draw_uri_regions"` on a terminal
tab: GhostShell `view.replace`s the buffer every frame, so those
regions land on stale offsets (random blue underlines during output).
Hover still finds a URI under the caret. Underlines cannot show on the
live command line (typing + per-cell `ai.fb.*` fills).



## 0.1.6

**Bug fix:** a wide character (emoji) in a full-width TUI box made
GhostShell send a 1-row `SIGWINCH` in a loop. The app then replayed the
whole conversation until the prompt came back.

libghostty-vt already knew the glyph was 2 cells
(`ghostty_unicode_grapheme_width`). The Sublime line also kept Ghostty's
spacer space, so the box was one em too wide, the H-scrollbar stole
viewport height, and measured rows flipped 47↔48. The spacer is dropped
now. A sub-cell jog can remain if the font's emoji advance is not
exactly `2 × em_width`; that is not enough to pop the scrollbar.

Also: `scrollback_history_size` is still lines; it is converted to bytes
at `terminal_new` because libghostty-vt's `max_scrollback` is a byte
budget ([ghostty#12769](https://github.com/ghostty-org/ghostty/discussions/12769)).
The session text log is the painted tab without the 300-line cap.


## 0.1.5

**Bug fix:** the session text log dropped anything that left the tab.

With `log_tab_text` on, the log rewrote the whole file as a snapshot of
the current paint. Capped scrollback, in-place redraws, and replaced
lines (thinking then answer) vanished. The file now only grows: lines
are appended as they leave the live last row, and the last row is
written when the session closes.

Opt-in is unchanged (`log_tab_text`, default false). Anyone already
logging was losing history; this release fixes that.

## 0.1.0 (initial release)

GhostShell is a terminal for Sublime Text with profiles for shells and AI
coding CLIs, native editor scrollback, and detachable sessions. It uses
Windows ConPTY and [libghostty-vt](https://github.com/ghostty-org/ghostty)
rather than depending on another terminal package.

- **Profiles for shells and AI coding CLIs** — launch cmd/PowerShell/WSL or
  any installed AI CLI (Claude Code, Codex, Pi, Cline, OpenCode, and others)
  in a real terminal tab, with per-profile mouse handling, alt-screen, and
  page-key routing tuned against each tool's actual behavior.
- **Detachable sessions** — a session's broker process can survive a
  Sublime Text restart and be recovered afterward, backed by a standalone
  Python interpreter and Windows Task Scheduler.
- **Native editor scrollback** — real Sublime scrollback and folding over
  terminal output, not a fixed-size alt-screen matrix.
- **Privacy-conscious defaults** — session recording (`log_tab_text`,
  `record_asciicast`) is off by default; it is an explicit opt-in.

### Verification for this release

- Full test suite passing (624 tests, 1 intentional skip).
- Real isolated-Sublime-install smoke testing (not just unit tests) covering:
  privacy defaults taking effect at runtime, the DLL download/checksum path
  (including a real fix for a Windows ACL-related install hang), detach/
  reconnect across a simulated restart, package update, and uninstall
  behavior. See [docs/PACKAGE_CONTROL.md](PACKAGE_CONTROL.md) for the full
  dated findings.
- Not yet covered: interactive resize/selection in a real Sublime UI
  (needs a human, or a UI-driving tool this project's test harness doesn't
  have).

### Known limitations

- Windows x64 only.
- First terminal launch needs internet access to download the pinned
  native library (see [Native library](../README.md#native-library) for
  offline use).
- Not yet listed in Package Control; see
  [docs/PACKAGE_CONTROL.md](PACKAGE_CONTROL.md) for submission status.
