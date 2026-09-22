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
| `ai_terminal/agent_broker.log` | 424 KB, 4,780 lines since 2026-08-30 | GhostShell broker (`ai_terminal.py:866`, added in `e248602`) | The broker's own output. The broker is a windowless process, so this is where its start, attach, detach and exit messages go. Each spawn line records the agent's full command line and working directory. A debugging aid, with no recorded decision to keep it. Live. Written every session. |
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

## What a Package Control user would get today (VERIFIED in code, 2026-09-21)

The defaults in `ai_terminal.sublime-settings` turn `log_tab_text` and `record_asciicast` off. These still happen on
every machine, with no setting to stop them:

1. **The broker log is always written to `~/data/logs/ai_terminal/agent_broker.log`** (`ai_terminal.py:866-870`,
   path built from the user's home directory). Detachable sessions are on by default for every profile, so any
   session creates a `data` folder in the user's home directory.
2. **A scheme backup folder is created at `~/data/logs/ai_terminal/scheme_backups`** (`ai_terminal.py:1944-1950`),
   with the path written in the code.
3. **`LOG_ROOT` is `~/data/logs`** (`terminal/log_paths.py:14`), the owner's personal layout, not a Sublime location.
4. **The colour scheme file is rewritten inside the package folder while it runs**
   (`Packages/GhostShell/ai_terminal.sublime-color-scheme`, `_scheme_disk_paths`, `ai_terminal.py:1896`). The
   code's own comment describes a developer layout ("junction-linked repo"). A package installed by Package
   Control may be a zipped `.sublime-package` with no writable folder, and an upgrade replaces the package folder, which
   would discard the scheme's accumulated rules.
5. The broker registry location (`_broker_registry_file`) was not checked in this pass.

None of these is documented as a deliberate choice. Under `AGENTS.md` rule 13 they need the owner's decision.
Standard Sublime locations would be `sublime.cache_path()` (throwaway files) and `Packages/User` (settings the
user owns).

## Owner's rule for shipping (2026-09-21)

Logging is for debugging before shipment, to find bugs, and it is fine on the owner's own installation. No user of the
released package may be subjected to it, and no inspected Package Control package logs. In the release, logging is off
by default and creates no folder or file until the owner's dev switch is on. Where practical the logging code is a
dev-only module the release leaves out. The release writes only to Sublime's standard locations.

History: on 2026-08-15 the loggers were deliberately handed to STLogs (`0a0490a` 01:15, `a5b8aac` 01:31, "so GhostShell
is not a second logging package"). Local copies were back in `terminal/` by 2026-08-28 (`820dd09`, an unlabeled
auto-backup), with no recorded decision. The four items in the section above (which happen even with logging off) are the ones that most clearly break this rule:
they must stop, or sit behind the owner's dev switch, before release. The recorder modules themselves must also default to
silent. Not done yet.

## Size cap (owner's rule, 2026-09-21)

A file the owner has just been told about should not be larger than 32 KB, and no log may grow without a limit.
Files that break this today (VERIFIED sizes):

| File | Size | Cap in code? |
|---|---|---|
| `ai_terminal/agent_broker.log` | 424 KB | None. Opened in append mode with no limit (`tools/agent_broker.py:152-160`). |
| `ai_terminal/settings_debug.log` | 80.4 MB | None found. Already on the delete list. |
| Recordings folder (`*.cast`) | 118.6 MB, 58 files | None found. Owner's decision listed above. |
| `developer_diagnostics.../ai_diagnostics.log` | 2.5 MB | STLogs, not checked. |

## STLogs removed (owner, 2026-09-21)

The owner removed STLogs from Sublime's `Packages`. Checked: GhostShell imports nothing from STLogs (searched all
`.py`, settings and JSON files), and no agent config references its hook forwarder (only Codex's `config.toml` lists the folder as
a trusted project). Consequences: `jsonl_tail_transcripts/` and the daily `2026-09-*.md` notes are no longer written,
and `.dsh_tail/`, `.jcode_tail/` and `.hook_spool/` have no writer and are safe to delete. The STLogs tailer that
was rewriting `.jcode_tail` every few seconds was added in an unlabeled commit on 2026-08-16 with no off switch.

## Broker log switched off by default (2026-09-21, owner's go)

`tools/agent_broker.py` no longer opens `~/data/logs/ai_terminal/agent_broker.log` unless the environment variable
`GHOSTSHELL_BROKER_LOG` is set to `1` in the broker's environment (the owner's dev switch). Off by default: no folder is
created, no file is opened, nothing holds the folder open. Tests: `tests/test_broker_lifecycle_log.py` (off by default, off
for any value other than `1`, on with the switch, no-op without a path). Brokers already running keep their old log open
until their session ends; only brokers started after this change are affected. Not yet changed: `ai_terminal.py` still
passes `--log-file` and still creates `scheme_backups/` (`ai_terminal.py:1944-1950`).
