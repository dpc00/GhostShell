---
name: ghostshell-session-text-log-bug
description: "FIXED (2026-09-14 debounce, 2026-09-18 append design) -- GhostShell's SessionTextLog.observe() does a synchronous full-buffer disk rewrite on every changed paint, causing ST main-thread freezes under high-throughput terminal sessions (e.g. omp)"
metadata: 
  node_type: memory
  type: project
  originSessionId: bd237c82-c328-4cea-8ba5-30e19d5fa848
  modified: 2026-09-14T20:59:52.774Z
---

Confirmed real bug in GhostShell (`terminal/session_text_log.py`, `SessionTextLog.observe()`, ~line 64): every time the painted tab content changes, it does a **full synchronous rewrite** of the entire painted buffer — write to temp file, `os.replace()`, close, reopen — all on Sublime Text's main thread, gated only by "did content differ from last observe." The module's own comment (lines 17-23) already flags this as "real disk I/O on ST's main thread."

**Why: this caused a real incident (2026-09-14).** A long-running, high-output omp session (41-package live bug-hunt test) in Tab 2 produced continuous colored terminal output. `ai_terminal.py`'s render loop caps at `_RENDER_MS = 30` (~33fps, hardcoded, not user-configurable) via `_schedule_render`/`_RENDER_MIN_INTERVAL_MS`, but every one of those render ticks that changes content also triggers `observe()`'s full-buffer rewrite. With continuous output, that's up to ~33 full synchronous disk rewrites/sec of the entire painted buffer (scrollback + live screen), each blocking ST's main thread — froze ST's UI (no focus, no click response, unresponsive Claude Code permission prompts) for extended periods. Root cause was misdiagnosed at first as a `log_tab_text` setting issue — it wasn't; both sessions were accurate observations of the setting at different times (session had a 27-min-old log handle opened while `log_tab_text` was still true, before it got flipped to false ~11 min in). The actual mechanism is the synchronous full-rewrite architecture itself, independent of the setting value.

**Immediate mitigation used:** detached the runaway session to its own Windows Terminal window via the `ai_terminal_open_in_windows_terminal` command (found via `eval_python` on ST's main thread, targeting the view by its `ai_terminal_broker_profile` setting == "omp"), which is a non-destructive broker handoff (session keeps running, tab just stops rendering it in ST). This immediately relieved the render/log-write load. Reattaching it (`ai_terminal_reattach_all_from_windows_terminal`) reintroduces the same load, and the freeze recurred on reattach.

**How to apply / what to actually fix:** Donald does not want to reduce scrollback further (already lowered once to fix a separate mouse-scroll-granularity problem — changing it is off the table as a mitigation). The correct fix is architectural, in `session_text_log.py`'s `observe()`:
- Move the write off the main thread (background thread/queue), or
- Debounce/coalesce writes (e.g. max 1 rewrite per N seconds) instead of one per changed paint, or
- Switch from full-snapshot-rewrite to an incremental/append-friendly representation for high-churn sessions specifically.

Related: [[project_ghostshell_secrets_hardening]] (other GhostShell/ai_terminal.py work), [[project_ai_terminal_ansi_rendering_glitch]], [[project_colorpicker_modal_freeze]] — this is the third distinct ST-freeze-class bug found in ai_terminal.py/GhostShell, worth treating as a pattern (main-thread-blocking operations in the render/paint path) rather than one-off fixes.


**STATUS 2026-09-29 (checked in the code and live):** this bug is FIXED. `terminal/session_text_log.py` now debounces disk writes on a 0.5 s timer thread (2026-09-14) and keeps evicted lines in `_kept` with row-alignment (`_scrolled_off_count`, 2026-09-18, 'Append tab text to the session log instead of rewriting a snapshot'). Live `session_text_log_stats()` from the running Sublime: 49,814 observe calls, 15,505 writes, 10.95 s total (~0.7 ms each), worst single write 30 ms. Remaining theoretical cost: `_write_tab_locked` still rewrites kept+tab under the lock `observe()` takes on the main thread (O(session)); see the Buildview study in the GhostShell repo (GhostShell `ai/BUILDVIEW_STUDY.md`, section 3.2). Not a current problem.
