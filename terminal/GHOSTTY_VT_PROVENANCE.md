# libghostty-vt provenance

## 1.1.0-dev build (branch `libghostty-vt-1.1.0-port`, 2026-11-07)

- libghostty-vt version: `1.1.0-dev+8265e74`
- SHA-256: `C511C906FE57E36B36E2FD1248556C5127C1D889B64A20B47272AF7989C4BD88`
- Size: `2,252,288` bytes
- Built locally, not published: `zig build -Demit-lib-vt=true -Doptimize=ReleaseFast`
  with Zig `0.17.1` from a shallow clone of `https://github.com/ghostty-org/ghostty`
  at commit `8265e74` (2026-11-07), artifact `zig-out-rel/bin/ghostty-vt.dll`.
- The Debug build of the same source is 9,346,560 bytes plus a 12 MB `.pdb`; the
  shipped artifact must be the optimised one.
- `RELEASE_DLL_URL` still points at the 0.1.0-dev release so `ensure_dll()` fails
  closed on the hash rather than replacing this DLL. Publish a release for this
  binary before merging the branch.

### What the upgrade changed in this repo's binding

`tests: tools/verify_libghostty_vt.py` gates it (8 checks, 0 failed on this build):

- `ghostty_terminal_new(allocator, &term, cols, rows)` -- no options struct.
- Scrollback is `TERMINAL_OPT_SCROLLBACK_MAX_LINES` (`size_t*`) plus
  `TERMINAL_OPT_SCROLLBACK_MAX_BYTES`. The byte limit defaults to only 10,000 and
  wins when reached first, so it is lifted with `SIZE_MAX` (`NULL` removes only a
  limit you set). The line limit trims in whole pages and overshoots by about a
  page -- 300 lines kept 52 rows, 1000 kept 818 -- so native gets a 4x backlog
  (`max(cap*4, 1200)`) and `Screen.history` still enforces exactly `cap`.
- `GhosttyStyle` is 16 bytes with no size field and a 16-bit `flags` bitmask;
  `GhosttyStyleColor` is a one-byte tag plus a three-byte value; `GhosttyGridRef`
  drops its leading `size_t`.
- `ghostty_terminal_mode_get` is gone: modes read through
  `GHOSTTY_TERMINAL_DATA_MODE` (37) with a `GhosttyTerminalModeConfig`.
- `ghostty_{key,mouse}_encoder_setopt*` are now `..._set*`, and every
  `*_ENCODER_OPT_*` id is one higher (INVALID took 0).
- Terminal data ids, `GhosttyBuffer`/`GhosttyString`, `row_cells_get_multi` and
  `HAS_STYLING` are unchanged, so the 2026-11-07 replay work carries over.

Not covered by the harness and therefore unproven on this build: key encoding,
mouse encoding (both renamed and renumbered), and anything that depends on
`TERMINAL_OPT_ENQUIRY` / `TERMINAL_OPT_WRITE_PTY` behaviour in a live tab.

## 0.1.0-dev build (audited 2026-08-30)

The runtime binary `terminal/bin/ghostty-vt.dll` is intentionally ignored by
Git. The binary audited on 2026-08-30 has:

- libghostty-vt version: `0.1.0-dev`
- SHA-256: `BAB4D9ACA5C96B0BDB97FDC30A7C04630166D4F9585FBA4501A4E5DAE1243C20`
- Size: `1,693,184` bytes
- Distribution: [GitHub Release `ghostty-vt-634957c8`](https://github.com/dpc00/GhostShell/releases/tag/ghostty-vt-634957c8)
- Source repository: `https://github.com/ghostty-org/ghostty`
- Source commit: `634957c8e67cad5040f54cef57de5502450d1f5f`
- License: MIT, reproduced in [GHOSTTY_LICENSE](GHOSTTY_LICENSE) from the
  [pinned source revision](https://github.com/ghostty-org/ghostty/blob/634957c8e67cad5040f54cef57de5502450d1f5f/LICENSE).
- Supported distribution target: Windows x64. The downloaded artifact is a
  Windows DLL, not a macOS or Linux shared library.

At the time of the audit, the shipped DLL was byte-for-byte identical to
`zig-out/bin/ghostty-vt.dll` in the source checkout at that commit. The source
checkout also contained an untracked `zig-pkg/` directory; it did not change
the recorded Git commit.

`terminal.ghostty_vt.load_library()` queries `ghostty_build_info` and logs the
libghostty-vt version alongside the loaded path, SHA-256, and size. On first
use it downloads the DLL from the Release above via `ensure_dll()`, verifying
it against `EXPECTED_SHA256` before use -- a file already at
`terminal/bin/ghostty-vt.dll` that already matches is reused, no network
touched. When replacing the DLL: publish the new build as a new GitHub
Release, then update `RELEASE_DLL_URL` and `EXPECTED_SHA256` in
`terminal/ghostty_vt.py` together with this record.
