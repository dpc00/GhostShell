# libghostty-vt provenance

The runtime binary `terminal/bin/ghostty-vt.dll` is intentionally ignored by
Git. The binary audited on 2026-08-30 has:

- libghostty-vt version: `0.1.0-dev`
- SHA-256: `BAB4D9ACA5C96B0BDB97FDC30A7C04630166D4F9585FBA4501A4E5DAE1243C20`
- Size: `1,693,184` bytes
- Distribution: [GitHub Release `ghostty-vt-634957c8`](https://github.com/dpc00/GhostShell/releases/tag/ghostty-vt-634957c8)
- Source repository: `https://github.com/ghostty-org/ghostty`
- Source commit: `634957c8e67cad5040f54cef57de5502450d1f5f`

At the time of the audit, the shipped DLL was byte-for-byte identical to
`zig-out/bin/ghostty-vt.dll` in the source checkout at that commit. The source
checkout also contained an untracked `zig-pkg/` directory; it did not change
the recorded Git commit.

`terminal.ghostty_vt.load_library()` queries `ghostty_build_info` and logs the
libghostty-vt version alongside the loaded path, SHA-256, and size. When
replacing the DLL, update this record after comparing the installed artifact
to the intended build output.
