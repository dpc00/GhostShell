# Windows Terminal ConPTY provenance

Profiles with `tsp_enabled` need the newer ConPTY that Windows Terminal uses. Windows' built-in
ConPTY swallows the questions omp asks at start-up, so the native view never starts. GhostShell does
**not** ship these files in the repository; it downloads them once and keeps them in
`terminal/bin/conpty/` (ignored by Git).

- Source: NuGet package `Microsoft.Windows.Console.ConPTY` version `1.24.260512001`
  (`https://api.nuget.org/v3-flatcontainer/microsoft.windows.console.conpty/1.24.260512001/...nupkg`),
  built from <https://github.com/microsoft/terminal> by Microsoft. License: MIT.
- Package SHA-256: `F889A9272A8B257DC6D5BE7525626FDB0F7CA6B5CE7E13093FC4BC979D24F484` (1,732,017 bytes).
- Files used (x64 only): `runtimes/win-x64/native/conpty.dll`
  (`C46DCD04F52B97F6A8CF53E8F547C85A821660BED18DE2B3344AFCD4A8389AD6`) and
  `build/native/runtimes/x64/OpenConsole.exe`
  (`47828C3FE080212F69DFDB39AB3673170FCC7445924C76FE003CEFD18247DD5D`). Both carry Microsoft's
  Authenticode signature (checked 2026-10-01).
- How it is used: `tools/conpty_api.py` `ensure_default_files()` downloads the package, checks the
  package hash, then each file's hash, and only then moves the files into place. A wrong hash, no
  network, or a CPU type other than x64 prints one line and the tab uses Windows' built-in ConPTY.
  Both the detachable-session broker (`tools/agent_broker.py`) and the in-process terminal
  (`ai_terminal.py` `_Pty`) choose through the same function. `_with_conpty_folder()` in
  `ai_terminal.py` hands a profile with `tsp_enabled` the folder; a profile that names its own
  folder in `spawn_env` (`GHOSTSHELL_CONPTY_DIR`) keeps it.
- VERIFIED 2026-10-02: the x64 files in the package are byte for byte the files first tested on
  2026-10-01; through `ai_terminal.py` `_Pty` a program's device-attributes query is delivered to
  GhostShell with the new files and swallowed with the built-in ConPTY; a wrong package hash leaves
  no file behind; a corrupted file is detected and repaired by the next download.
- UNVERIFIED: x86 and ARM64 (the package has those files; not pinned or tested here).
- Updating: change `NUGET_URL`, `NUGET_SHA256`, `NUGET_MEMBERS` and `KNOWN_SHA256` in
  `tools/conpty_api.py` together with this file.
