# Release notes


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
  `record_asciicast`) and background usage/quota scanning
  (`usage_scan_enabled`) are off by default; each is an explicit opt-in.

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
