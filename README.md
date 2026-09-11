# GhostShell

A terminal for Sublime Text, with profiles for shells and AI coding CLIs,
native editor scrollback, and detachable sessions. GhostShell uses Windows
ConPTY and [libghostty-vt](https://github.com/ghostty-org/ghostty) rather than
depending on another terminal package.

![12 AI coding CLIs running in GhostShell terminal tabs](docs/screenshots/supported-clis.png)

## Requirements

- **Sublime Text 4, build 4107 or newer, on Windows x64.** The plugin selects
  Sublime's bundled Python 3.8 host via `.python-version`.
- **Windows 10 version 1809 or newer, or Windows 11**, for ConPTY.
- Internet access to GitHub on the first terminal launch, to download the
  pinned native library. See [Native library](#native-library) for offline use.
- Install any shells or AI CLIs you want to use separately and make their
  commands available on `PATH`. GhostShell does not install them or provide
  their accounts, subscriptions, or API access.
- **Detachable sessions**, enabled in the current defaults, additionally need
  a standalone Windows Python installation (`python.exe` and `pythonw.exe`),
  PowerShell, and permission to run a temporary task in Windows Task Scheduler.
  Python 3.12 is used for development. Set `broker_python` to the full path to
  `python.exe` if detection picks the wrong interpreter. A Windows Store
  execution alias is not a substitute for an installed interpreter.
- Windows Terminal is optional, only needed for the Windows Terminal handoff
  commands. A monospace font with box-drawing coverage is recommended. Install
  Cascadia Code or override `font_face` if it is not available on your machine.

The distributed native library is Windows x64 only. POSIX-related code in the
repository does **not** mean that macOS or Linux installations are supported.

## Installation

**Not yet listed in Package Control.** Until approved, use a development
checkout. In Sublime Text, choose **Preferences > Browse Packages**, then clone
this repository into a folder named **GhostShell** inside that directory:

```console
git clone https://github.com/dpc00/GhostShell.git GhostShell
```

Alternatively, link an existing checkout from PowerShell (creating a symbolic
link may require Developer Mode or administrator permission):

```powershell
New-Item -ItemType SymbolicLink -Path "<Packages directory>\GhostShell" -Target "<checkout directory>"
```

Use the actual Packages directory opened by Sublime, especially for portable
installations. Keep the folder name `GhostShell`: generated resources and the
settings menu use that package name. Restart Sublime after installation.

After the package is accepted, use **Package Control: Install Package**, then
select **GhostShell**. Do not keep a manual checkout installed alongside the
Package Control installation. The `.no-sublime-package` marker ensures that
the native library and broker scripts have real filesystem paths.

## Quick start and settings

1. Open **GhostShell: Settings** from the command palette, or
   **Preferences > Package Settings > GhostShell > Settings**.
2. Put overrides in the **right-hand User file**, not the left-hand defaults.
   Package updates replace the defaults. The settings filename remains
   `ai_terminal.sublime-settings` for compatibility.
3. Run **Ai Terminal: Launch Agent…** (`Ctrl+Alt+N`) and select an installed
   shell or CLI. Most commands currently use the **Ai Terminal** prefix.
   **Ai Terminal: Open Here** launches the configured default profile.

The current defaults still include development-machine profiles. To start
with a plain Windows command prompt, without a detachable broker or full
session recording, use these User settings:

```json
{
    "default_profile": "Command Prompt",
    "log_tab_text": false,
    "record_asciicast": false,
    "usage_scan_enabled": false,
    "color_scheme_log_path": null,
    "shared_spawn_env": {},
    "profiles": {
        "Command Prompt": {
            "launch_command": ["cmd.exe"],
            "detachable": false,
            "spawn_env": {}
        }
    }
}
```

Add other profiles using their CLI command, for example `"launch_command":
["claude"]` or `["codex"]`. Restart Sublime after changing its inherited
`PATH`. Mouse handling, alternate-screen behavior, and page-key routing are
per-profile options, not a guarantee of compatibility with every TUI version.

**Ai Terminal: Sync Detected Agent Profiles** refreshes the generated profile
list after installing a CLI. It writes only
`User/ai_terminal_agents.sublime-settings`. Hand-written profiles take precedence
over generated profiles of the same name.

See [COMMANDS.md](COMMANDS.md) for command names and bindings, and
[detachable sessions](docs/DETACHABLE_SESSIONS.md) for recovery and lifecycle
details. Use **End Session (Kill + Close)** when you want to stop a session,
rather than merely disconnect from a persistent broker.

## Privacy and background activity

Review the defaults before running commands with sensitive output:

- **Session transcripts and asciicast recording are disabled by default**
  (`log_tab_text: false`, `record_asciicast: false`). Enabling either writes
  under `~/data/logs/ai_terminal_session_text_logs` and
  `~/data/logs/ai_terminal_asciinema_casts_for_troubleshooting_rendering`.
  Recordings can contain terminal output and input, including credentials and
  source code. Profiles can override these settings. Disabling recording does
  not remove existing files or disable all diagnostic logs.
- Detachable brokers retain a bounded output replay buffer and local registry
  records. Their temporary launch files include the child environment. A
  detachable session's broker process persists in the background across
  Sublime Text restarts (that's the point — it's what makes reconnect work),
  not just while Sublime is open; use **End Session (Kill + Close)** to
  actually terminate one instead of just closing the tab. Review
  [the broker architecture](docs/DETACHABLE_SESSIONS.md) before enabling them
  in a restricted environment. **Removing the package does not stop a
  running broker either** — confirmed directly: deleting the package
  directory while a detachable session is alive leaves its broker and
  child process running, with no package-provided way left to reconnect or
  stop them (`agent_broker.py`/`recover_console.py` are gone too). The same
  is almost certainly true of a Package Control *update* while a session is
  live, for the same reason (the running broker process already has its
  code loaded and never checks the package directory again), though that
  exact combination has not been separately tested. Run **End Session** on
  every open detachable tab (or check for orphaned `python.exe`/
  `pythonw.exe` processes running `agent_broker.py` in Task Manager) before
  uninstalling or updating if you don't want any left running.
- Usage/quota discovery reads supported CLIs' local session and credential
  files and calls their providers' usage endpoints in the background. Some
  providers may refresh saved OAuth tokens. This happens at plugin load and,
  by default, every 20 minutes. **`usage_refresh_minutes: 0` disables the
  periodic refresh only, not the startup scan or a manual refresh.** Set
  **`usage_scan_enabled: false`** to disable all three, including credential
  reads and OAuth refreshes by the usage scanner. This also clears cached
  provider results. A provider fetch already in progress is allowed to finish
  and persist any rotated tokens, but subsequent fetches are cancelled. Usage
  learned from the terminal's own output still works. Set this before first
  load in a restricted environment. It does not control the DLL download or
  network access by commands you launch.
- The native library is downloaded from the pinned GitHub Release below.
  Installing GhostShell does not install or authenticate AI CLIs. Commands
  run inside a terminal have their own privacy policies and side effects.

Do not attach recordings or credential settings to public bug reports without
reviewing and redacting them. POSIX-style `0600` file modes do not enforce
private Windows ACLs. Protect the log directories using Windows permissions.

## Native library

`terminal/bin/ghostty-vt.dll` is not tracked in Git. When the first terminal
creates its parser, `terminal/ghostty_vt.py` downloads the pinned binary from a
GitHub Release, verifies its SHA-256, and installs it atomically at that path.
An existing matching file is reused without a download. An offline first
launch fails unless the library is already installed.

For offline installation, download the artifact linked in
[GHOSTTY_VT_PROVENANCE.md](terminal/GHOSTTY_VT_PROVENANCE.md) on a connected
machine, verify its recorded SHA-256, and copy it to
`GhostShell/terminal/bin/ghostty-vt.dll` before launching a terminal.

Developers may set `GHOSTTY_VT_DLL` to an alternative compatible build. This
override bypasses the pinned download **and its checksum enforcement**, so use
only a trusted build. The pinned source revision, fingerprint, and upstream
[MIT license](terminal/GHOSTTY_LICENSE) are included in `terminal/`.

## Development and testing

```console
python -m pip install "pytest>=7,<8.4"
python -m pytest tests/ -q
```

The test dependency range also supports Python 3.8. Sublime itself does not
need pytest. Run the suite outside Sublime: tests use API stubs, and native
stress tests belong in an isolated Python process. Native tests skip if the
DLL is absent. The Task Scheduler integration test is separately opt-in, as
documented in [detachable sessions](docs/DETACHABLE_SESSIONS.md).

`python -m unittest discover -s tests -v` misses pytest-only tests. Use pytest
for the full suite. Unit tests are not a substitute for a clean Sublime
installation smoke test.

Repository layout:

- `ai_terminal.py`: Sublime adapter, ConPTY, views, commands, and settings.
- `terminal/`: renderer, VT bindings, profiles, history, usage, and logging.
- `tools/`: runtime broker/relay scripts and development utilities. The broker
  scripts must remain in release archives.
- `tests/`: unit and opt-in integration tests, retained in Git checkouts.
- `docs/`: user documentation and screenshots.

Maintainers: see the
[Package Control submission checklist](https://github.com/dpc00/GhostShell/blob/main/docs/PACKAGE_CONTROL.md)
in the source repository before tagging a release.

## License and support

GhostShell is [MIT licensed](LICENSE). libghostty-vt has its own
[MIT license](terminal/GHOSTTY_LICENSE).

Report problems at [GitHub Issues](https://github.com/dpc00/GhostShell/issues),
including your Windows and Sublime builds, CLI version, and a minimal
reproduction without secrets.
