---
name: project-libghostty-vt-1-1-0-2026-11-07
description: libghostty-vt 1.1.0-dev shipped in GhostShell 0.2.18 (DLL release, ABI mapping, what was verified live, rollback), written 2026-11-07
metadata:
  type: project
---

# libghostty-vt 1.1.0-dev in GhostShell 0.2.18 (2026-11-07)

Released: GhostShell **0.2.18** (tag `v0.2.18`, notes in `docs/RELEASE_NOTES.md` §0.2.18). The native
library moved from `0.1.0-dev` (ghostty `634957c8`) to **`1.1.0-dev+8265e74`**, built from that commit
with Zig 0.17.1 and published as its own release, **`ghostty-vt-8265e74`** (2,252,288 bytes, sha256
`C511C906…`); `ensure_dll`/`RELEASE_DLL_URL` point at it. The previous binary is kept as
`terminal/bin/ghostty-vt-0.1.0-dev.dll` — swap the two filenames and restart Sublime to roll back.

## The ABI is not a rebuild

`terminal_new` takes cols/rows instead of an options struct. The scrollback cap is a **line count set
afterwards**, and its byte limit defaults to 10 000 bytes and must be lifted with `SIZE_MAX`; the line
limit trims whole pages and overshoots, so native keeps a `max(cap*4, 1200)` backlog while
`Screen.history` still enforces exactly `history_cap`. `GhosttyStyle` is a 16-byte flags bitmask with
a one-byte-tag colour. `GhosttyGridRef` dropped its leading size. `terminal_mode_get` is gone. The
key/mouse encoders renamed their setters and shifted every option id by one. `key_event_set_utf8`
takes its text as a `GhosttyString` by value.

## Rule that cost the owner an editor twice

**Never port the binding into the live package while the shipped DLL is the old ABI.** Doing that on
2026-11-07 crashed his Sublime twice. Port and DLL ship together, or the binding stays on the old ABI.

## Measured on the new library

A 17 400-line burst went from 13.9 s to 0.82 s (a burst no longer materialises every frame); a
full-screen materialisation is 2.4 µs/cell instead of 10.6; scrollback capture waits for the stream to
settle and rebuilds only the rows that survive the cap; the post-replay rebuild reads the newest
`history_cap` rows instead of every native row then trimming. A typed character went from 151–183 ms to
42–80 ms (`send_string` → painted frame), and an omp frame from 63–114 ms to 19–31 ms; the echo
fast-path (`_RENDER_MS_ECHO`, `_RENDER_ECHO_WINDOW_S`, `_RENDER_ECHO_MAX_BYTES`) paints 16 ms after
input the person actually typed.

## Verified live before the tag, and what was not

Verified on the shipped build: spawn a Dos Console tab, type into it and read the echo back, resize the
console with `mode con`, End Session (kill + close, no dialog, no orphan), and two full restarts that
reattached the agent tab. **Not verified**: Close Tab (Keep Session Alive) and Recover Session —
broker-level paths that do not touch the library — and the oldest-supported-build portable
(`D:\st_portable_4200`) no longer exists on this machine, so that check is absent rather than skipped.

`tools/verify_libghostty_vt.py` is the ABI harness: 10 checks, 0 failed on the new DLL, including the
exact bytes for a, enter, backspace, up, tab and ctrl+c, a mouse press as `\x1b[<0;4;5M`, a full-screen
materialisation at 2.4 µs/cell, a 1200-line burst in 0.72 s, and history ending on exactly 300 rows
that abut the visible screen. Run it before any future DLL swap.
