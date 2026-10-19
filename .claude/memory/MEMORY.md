# Memory index (GhostShell notes)

Notes from long sessions working on GhostShell. Each is a dated record, so check that it still matches.

- [tern-sdk: no daily update](feedback_tern_sdk_no_daily_update_2026_10_18.md) - 2026-10-18 he wants no schedule; check on request, act only on monumental changes; clone is 1 small commit behind
- [GhostShell secrets hardening](project_ghostshell_secrets_hardening.md) - user wants LLM package's secret-handling design ported into.
- [GhostShell session_text_log freeze](project_ghostshell_session_text_log_bug.md) - SessionTextLog.observe() does synchronous full-buffer disk rewrite.
- [GhostShell usage-scan omp port](project_ghostshell_usage_scan_omp_port.md) - Copilot fetcher shipped; Devin needs no fetcher (text tier already.
- [GhostShell follow patch](project_ghostshell_follow_patch_2026_10_03.md) - jump fix live (in backup commit 4a3b028); jump still reported.
- [Stale Claude tab on phone](project_ghostshell_stale_tab_phone_2026_10_07.md) - 2026-10-07 tab showed an old permission prompt; change_count frozen while active; 10 PTY resizes in a row; cause not established
- [ai_terminal ANSI rendering glitch](project_ai_terminal_ansi_rendering_glitch.md) - Claude tab garbled after colorized node output; resize did NOT fix.
- [ColorPicker modal freeze](project_colorpicker_modal_freeze.md) - ColorPicker's native dialog blocks main thread, freezes ALL.
- [ST trackpad zoom freeze](project_st_trackpad_zoom_freeze.md) - ST "freeze" during heavy terminal output can be a pinch-zoom.
- [Buildview ? GhostShell study](reference_buildview_ghostshell_study.md) - study of Buildview's view-to-view PIPING (read-head delta copy.
- [ai_terminal send_string unreliable](feedback_ai_terminal_send_string_unreliable.md) - don't trust programmatic PTY injection to steer a live omp/agent CLI.
- [Run commands in an ai_terminal via sublime-mcp](feedback_use_sublime_mcp_ai_terminal_for_commands.md) - open_here with profile Bash/PowerShell, send_string to that view id.
- [Study the named mechanism](feedback_study_the_named_mechanism.md) - 'study X for GhostShell' = the one mechanism he names (Buildview's.

- [close-test-tabs-with-kill-session-2026-10-10](feedback_close_test_tabs_with_kill_session_2026_10_10.md) - never say a tab can't be closed: ai_terminal_kill_session then view.close() by exact id; no focus_view on his Claude tab; no sleep in eval_python
- [ghostshell-osc7501-step-one-2026-10-10](project_ghostshell_osc7501_step_one_2026_10_10.md) - SOLVED and in daily use 2026-10-10/11: Claude Code now sends OSC 7501 reports (root cause: two ConPTY effects; fix: Windows Terminal ConPTY for the Claude profile + 7501 reply as key events; tones on state change); Donald 2026-10-11: it IS working and significantly speeds operation; issue anthropics/claude-code#101114
- [input-panel-for-claude-tab-2026-10-12](project_input_panel_idea_2026_10_12.md) - his idea: type in a Sublime input panel, Enter sends to the tab, so prompts never collide with typing; not built
- [scroll-jump-typing-scroll-duplicates-2026-10-13](project_ghostshell_scroll_jump_typing_scroll_duplicates_2026_10_13.md) - his two bug reports (view jumps to 1/3 of scrollback; typing pushes the command line above the view) plus repeated screen blocks found in the Claude tab, hypothesis, next steps
- [ghostshell-tsp-session-2026-10-15](project_ghostshell_tsp_session_2026_10_15.md) - TSP session 2026-10-14/15: aside is opt-in (tsp_aside_column), Settings stays in the omp tab, prefs change events and ANSI colour fixes, released as v0.2.6; portable test method; mouse caret in the composer is NOT a priority (his word); restart Sublime after changes touching ai_terminal.py and terminal/*.py
- [tern-sdk-ghostshell-2026-10-14](project_tern_sdk_ghostshell_2026_10_14.md) - tern-sdk = program side of TSP, used as a test client; el/media/block, forms, listen:false, styles, flow, palette, reduce-motion; his decisions and the plan; how to run an SDK example in a tab
- [ghostshell-phantom-selection-console-read-2026-10-08](project_ghostshell_phantom_selection_console_read_2026_10_08.md) — Claude tab froze from a stray line selection; Donald: happens when I read the console; visible-capture click fix in sublime_mcp.py, not proven by trace
- [ghostshell-tab-stale-after-phone-approval-2026-10-08](project_ghostshell_tab_stale_after_phone_approval_2026_10_08.md) — approving a prompt from the phone leaves the Claude tab showing the old prompt (no repaint); not yet investigated
- [briefing-omp-native-view-unreadable-2026-10-14](project_briefing_omp_native_view_unreadable_2026_10_14.md) - briefing for omp: native TSP view text is blank phantoms, so agents cannot read tab 2 except by screenshot; asks for one new sublime-mcp tool that returns the panels as text
- [one-thread-conversion-map-2026-10-15](project_one_thread_conversion_map_2026_10_15.md) - where each of the 33 thread/timer sites goes in a one-thread async design, basis (2 experiments), order of work, risks; no code changed
- [ghostshell-notes-from-sublime-mcp-baton-2026-10-14](project_ghostshell_notes_from_sublime_mcp_baton_2026_10_14.md) - view tracer never wrote (import fix), auto-approve, omp scroll snap (tsp_animation_ms), omp panel registry for get_view_phantoms, view-jump root cause (trim pause) and fix bf92c6a; carved out of the sublime-mcp baton
- [recorders-loggers-keep-own-thread-2026-10-15](feedback_recorders_loggers_keep_own_thread_2026_10_15.md) - exception to his one-thread rule: cast recorder and text log keep their own writer thread; I removed them unasked, restored
- [test-oldest-supported-build-before-release-2026-10-15](feedback_test_oldest_supported_build_before_release_2026_10_15.md) - v0.2.7 shipped broken on Python 3.8 hosts (4200 and earlier); run the real flow on the 3.8 portable before any release; fixed in 0.2.8
- [exit-close-detach-test-matrix-2026-10-15](project_exit_close_detach_test_matrix_2026_10_15.md) - live test of every exit/kill/close/detach path (9 cases, all pass), the five bugs fixed (0a325bd auto-close = Py3.14 asyncio _empty_waiter crash, 30977dd, 9464b24, 7076fcc, b4cc9f0), also all pass on the 3.8 portable (4200)
- [readme-out-of-date-2026-10-16](project_readme_out_of_date_2026_10_16.md) - Donald: GhostShell README is out of date (last committed 10-15 11:23, 0.2.11 and 0.2.12 came after); "what else?" still open
