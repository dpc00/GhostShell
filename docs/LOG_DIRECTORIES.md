# What is in ~/data/logs, and what to do with it

Written 2026-09-21 from a read-only inventory (223 MB total). The owner decides; nothing here was deleted.
"Writer" means the code that creates or appends to it (found by searching the projects and STLogs).

## Delete: no code writes it any more, or it was retired (nothing lost)

| Path | Size | Last written | Why it is safe |
|---|---|---|---|
| `ai_terminal/settings_debug.log` | 80.4 MB | 2026-08-11 | Debug trace of settings changes from one day. Only recreated if `AI_TERMINAL_DEBUG` is set. |
| `ai_terminal/vp_diag_id19.jsonl` | 48 KB | 2026-08-23 | One-off viewport diagnostic. No code in any project writes it. |
| `ai_terminal/mock_agent_cli.log` | 37 KB | 2026-09-18 | Test artifact from `tests/mock_agent_cli.py`. |
| `ai_terminal/color_scheme.log` | 585 B | 2026-08-18 | Retired diagnostic. Summary kept in `docs/DEVIATIONS_FROM_TERMINUS.md`. |
| `developer_diagnostics_and_runtime_server_error_logs/color_scheme.log` | 66 KB | 2026-07-20 | Same. |
| `ai_terminal_claude_session_ids/` (7 files) | tiny | 2026-08-26 | No code references it. Left over from the retired Claude-only resume launcher. |
| `developer_diagnostics_and_runtime_server_error_logs/server_relaunch.log` | 0 B | 2026-07-16 | Empty, no writer. |

## Keep: a program is writing it now, or it is bookkeeping

| Path | Size | Writer | Note |
|---|---|---|---|
| `ai_terminal/agent_broker.log` | 424 KB | GhostShell broker | Live. Written every session. |
| `ai_terminal/scheme_backups/` | empty | GhostShell (`_durable_scheme_backup`) | Backups of the color scheme. |
| `ai_terminal_session_text_logs/` | 1.1 MB | GhostShell (`log_tab_text`) | Small. |
| `jsonl_tail_transcripts/` | 17 MB | STLogs | The agent transcript record. |
| `.dsh_tail/`, `.jcode_tail/`, `.hook_spool/` | 0 MB | STLogs | Bookkeeping so STLogs does not re-read old transcripts. Do not delete. |
| `pybackup/` | 0.1 MB | pybackup | Its own logs. |
| `2026-09-*.md` | small | STLogs | Daily notes. |

## Your decision

| Path | Size | Writer | Choice |
|---|---|---|---|
| `ai_terminal_asciinema_casts_for_troubleshooting_rendering/` | 118.6 MB, 58 files | GhostShell (`record_asciicast`) | The biggest item. They are how the resize storms were diagnosed. Keep the newest 7 days, or switch `record_asciicast` off. |
| `developer_diagnostics_and_runtime_server_error_logs/` (`server_error.log` 2.3 MB, `post_error.log`, `server_runtime.log`) | 4.8 MB | STLogs | Old errors (Jul 28 to Aug 13). Safe to delete. It is STLogs' directory, not GhostShell's. |
| `developer_diagnostics_and_runtime_server_error_logs/ai_diagnostics.log` | 2.5 MB | STLogs | Written today. Keep while STLogs is in use. |
| `periodic_automatic_editor_screenshots_for_additional_context/` | empty | STLogs | A feature that produced nothing. |

## Rule for every agent (also in `AGENTS.md`)

No agent may create a log file, log directory or output location, or move an existing one, without the owner's
approval. Anything approved is added to this file first, with its writer, purpose, size cap and retention, and is off
by default.
