# libghostty-vt provenance

The runtime binary `terminal/bin/ghostty-vt.dll` is intentionally ignored by
Git. The binary audited on 2026-08-30 has:

- SHA-256: `BAB4D9ACA5C96B0BDB97FDC30A7C04630166D4F9585FBA4501A4E5DAE1243C20`
- Size: `1,693,184` bytes
- Distribution: [shared Google Drive link](https://drive.google.com/open?id=1d1GyMHTtVN71RVYKjnsEnRzfBqrJwA1h)
- Source repository: `https://github.com/ghostty-org/ghostty`
- Source commit: `634957c8e67cad5040f54cef57de5502450d1f5f`

At the time of the audit, the shipped DLL was byte-for-byte identical to
`zig-out/bin/ghostty-vt.dll` in the source checkout at that commit. The source
checkout also contained an untracked `zig-pkg/` directory; it did not change
the recorded Git commit.

`terminal.ghostty_vt.load_library()` logs the loaded path, SHA-256, size, and
mtime at runtime. When replacing the DLL, update this record after comparing
the installed artifact to the intended build output.
