# Release notes


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
