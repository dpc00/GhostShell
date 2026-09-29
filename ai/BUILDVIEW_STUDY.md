# Buildview study: ideas that could apply to GhostShell

Written 2026-09-29 at the owner's request. Working note (AGENTS.md rule 16): kept in `ai/` because the owner asked for it here. Every finding is marked VERIFIED (with file:line or a live check) or UNVERIFIED, per rule 12.

**Method.** Buildview (rctay/sublime-text-buildview, about 400 lines: `commands.py`, `pipe_views.py`, `settings.py`) was read in full and its behavior tested live on a portable Sublime Text 4215. GhostShell was read only: it was not run and not changed, apart from one read-only call to the live Sublime for `session_text_log_stats()`. GhostShell line numbers are from `984474f`.

## 1. What Buildview does

It turns Sublime's transient `exec` output panel into a persistent "Build output" tab. VERIFIED (read and live test).

- It hooks the build keys through a fake key context, `build_fake`. `on_query_context` returns `None`, so the real build still runs.
- It mirrors the panel on every `on_modified` with a **read-head**: `source_last_pos` remembers how much was already copied, only `Region(prev_pos, new_pos)` is copied, and a small `ContentReplace(start, end, text)` command applies it to the destination view.
- Around that: a re-entrancy guard (`is_running`); the destination view is created outside the modified callback (`set_timeout`) while text is buffered (`prepare_create`, `buffer`); a placement policy that remembers the last group and index at close and otherwise picks another group or the side, then focuses the source view again; three scroll modes (`bottom`, `top`, `last`; `last` restores the viewport after a 500 ms timer); per-view settings and palette toggles.

## 2. Where GhostShell already matches or beats it (VERIFIED by reading)

- **Line-diff rendering.** `AiTerminalRenderCommand._run` (ai_terminal.py:8328) replaces only changed lines (`line_diff_render_enabled`) and has a 0 to 4 character `fast_caret` patch path.
- **Re-entrancy and coalescing.** `_schedule_render` (4772) and `_do_render` (5034) use `_render_pending`, `_render_coalesce` and `_in_render`, with a 30 ms minimum interval and a 100 ms debounce (`_RENDER_MS`, `_RENDER_MIN_INTERVAL_MS`, 4689-4690).
- **Settings.** `_setting_bool`, `_setting_string` and `_setting_number` (2481-2530) resolve profile override, then global, then an in-code default, and catch cast failures. This is what Buildview lacked (section 4).
- **Tab and panel hosting, safe close.** `_terminal_view` (4516) and `_terminal_panel_view` (4524); `_dont_close_window_when_empty` (4533); `on_close` (5548) detaches without killing the session; `AiTerminalTabCloseInterceptor` (4072) blocks native close commands. Buildview's "hook a builtin command without reimplementing it" trick is what this already does.
- **Session text log.** The `SessionTextLog` main-thread freeze is fixed: a 0.5 s debounce timer thread (2026-09-14) and an append design with `_kept` and `_scrolled_off_count` (2026-09-18); `terminal/session_text_log.py`. VERIFIED live: `session_text_log_stats()` on the running Sublime returned 49,814 `observe()` calls, 15,505 writes, 10.95 s total (about 0.7 ms each) and a worst single write of 30 ms.

## 3. Ideas, ranked

The current behavior described in each item is VERIFIED by reading. The expected benefit of every idea is UNVERIFIED: nothing here was measured or prototyped.

### 3.1 Patch-based render: compute the diff in Python, send only patches

- **Today** (VERIFIED, ai_terminal.py): each paint builds the whole frame `text` and passes it as a `run_command` argument (5183-5192; Sublime JSON-serializes command arguments). The command then reads the whole view, `view.substr(Region(0, view.size()))` (8403, 8427), and splits it with `split("\n")` (8428) to find changed lines.
- **Idea** (UNVERIFIED): GhostShell already keeps the previous frame in `term._last_render_text` (5194). Compute the line diff in the caller against that string and send only `[(start, end, replacement), ...]`, like Buildview's `ContentReplace`.
- **Safety** (UNVERIFIED): Buildview's read-head works only because nothing else edits the destination. Before patching, check `view.size() == len(term._last_render_text)` and fall back to today's full path on a mismatch. Selection-blocked and copy-mode paints must invalidate the cached text.
- **Measure first.** Per-frame time and argument size were not measured. Instrument them with about 300 lines of scrollback under a heavy stream (the omp case) before building anything.

### 3.2 Immutable prefix and mutable suffix for the session log

- **Today** (VERIFIED, session_text_log.py:140): `_write_tab_locked` rewrites `kept + tab` in full on each flush, under the same lock `observe()` takes on the main thread.
- **Idea** (UNVERIFIED): the file is `kept_text` (only grows) plus `tab_text` (changes every paint). Remember `kept_bytes`; on flush `seek(kept_bytes)`, append newly evicted lines, write the tab suffix, truncate. That is O(tab) instead of O(session), and the copy could be taken under the lock and written outside it.
- **Priority: low.** The live numbers in section 2 (worst write 30 ms) show no present problem. It only matters for very long sessions or slow disks.

### 3.3 Remember where the tab was, and where new ones go

- **Today** (VERIFIED): `set_view_index` is never called in ai_terminal.py (0 uses). New terminals open in the active group.
- **Idea** (UNVERIFIED): Buildview remembers the last group and index at close and reopens there; otherwise it prefers a group that does not hold the source view, then the side, then `focus_view(source)`. Candidates: recover-session and new-terminal placement, and an "open in the other group" option.
- **Trap**, from Buildview's own comments: on ST3 `get_view_index()` inside `on_close` returns `(-1, -1)`. On ST4 capture it in `on_pre_close` instead.

### 3.4 A user-selectable scroll-follow mode and per-tab toggles

- **Today** (VERIFIED): scroll pinning is internal logic (`_scroll_to_bottom` at 7534, the near-bottom re-pin in the render command). There is no user-visible follow mode. The trackpad-freeze note lists "no auto-scroll to bottom on new output" as an open gap.
- **Idea** (UNVERIFIED): a `follow_output: near_bottom | always | never` setting, with palette toggles that write view-level settings so they persist per tab, as Buildview's `ToggleScroll*` commands do. The same mechanism would let the bisection gates (`fast_caret_patch_enabled`, `line_diff_render_enabled`, `caret_footer_pinning_enabled`) be flipped per tab instead of by editing settings.

### 3.5 Feature idea: mirror any output panel to the agent

- **Idea** (UNVERIFIED): Buildview shows a reliable way to tap the `exec` panel (`window.get_output_panel("exec")`, `on_modified`, read-head). The same tap could power "send the last build output to the agent" or auto-forward a failing build into a GhostShell session.
- **Constraint:** anything forwarded needs the same redaction rules as the planned secrets hardening. Nothing was prototyped.

### 3.6 Startup buffering (check only)

Buildview buffers output that arrives before the destination view exists and prepends it later (`prepare_create`, `buffer`). Whether GhostShell can drop PTY output produced between broker attach and tab creation in the recover-session path is UNVERIFIED. It was not checked.

## 4. How Buildview fails (keep GhostShell clear of these)

1. **Defaults living only in a package's own `Preferences.sublime-settings`.** VERIFIED live: they were not visible until Sublime restarted. The plugin read `None`, was silently inert, and in a half-configured state raised `TypeError: Bool required` from `set_scratch(None)`. GhostShell uses its own named settings file and the `_setting_*` helpers with in-code defaults, so this does not apply. **Audit item (UNVERIFIED, not done):** any direct `settings.get("x")` passed straight into an API that needs a bool or int (`set_scratch`, `set_read_only`, `set_viewport_position`) without a default.
2. **Using one command's `edit` token on another view.** VERIFIED: GhostShell has 4 `insert/replace/erase(edit, ...)` calls and all target `self.view` (grep). Buildview avoids the mistake with dedicated content commands run on the destination view.
3. **`hide_panel` cannot be called directly in the key-context callback.** Buildview defers it with `set_timeout(..., 1)`. This is the same class of re-entrancy GhostShell handles with `_in_render`.
4. **Known upstream pain** (open Buildview issues): tab not reused after restart (#23), steals focus or switches window (#27, #20), F7 fails when the console is focused (#35). A placement feature (3.3) should decide these up front.

## 5. Suggested order (UNVERIFIED, a recommendation only)

1. Measure frame cost (3.1 needs a number). 2. Placement memory (3.3): small and self-contained. 3. Scroll-follow setting and per-tab gate toggles (3.4). 4. Patch render (3.1) only if the measurement justifies it. 5. Log suffix write (3.2) and the build-output feature (3.5) later or never.
