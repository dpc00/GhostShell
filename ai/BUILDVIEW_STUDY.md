# Buildview's piping: how it mirrors one Sublime view into another

Written 2026-09-29 at the owner's request. The subject is only Buildview's view-to-view piping (`pipe_views.py`, class `PipeViews`), not anything else in Buildview and not GhostShell's own internals. Working note (AGENTS.md rule 16): kept in `ai/` because the owner asked for it here. Every finding is marked VERIFIED (read in the source, or tested live) or UNVERIFIED, per rule 12.

**Method.** Buildview 1.2.3 (rctay/sublime-text-buildview) was installed on a portable Sublime Text 4215. Its `PipeViews` class was read in full (about 110 lines) and driven directly with two ordinary scratch views through nine scenarios, plus a cost benchmark. The test script is not in this repo (rule 16); it can be re-created from the scenario list below. Nothing in GhostShell was changed or run.

## 1. The mechanism (VERIFIED, read in `pipe_views.py`)

Buildview copies the text of Sublime's transient `exec` output panel (the source) into a persistent scratch tab named "Build output" (the destination).

1. **Trigger.** `on_modified` of the source view calls `pipe_text(view)`. There is no timer and no polling.
2. **Read-head.** `source_last_pos` is how much of the source has already been copied. Each call copies only `Region(source_last_pos, view.size())` and then advances the read-head.
3. **Same-offset write.** The delta is applied to the destination with `content_replace(start, end, text)`, i.e. `view.replace(edit, Region(start, end), text)` at the **same offsets** the text has in the source. The destination is therefore a position-for-position mirror.
4. **Tiny write commands on the destination view.** `ContentClear`, `ContentReplace` and `ContentPrepend` are `TextCommand`s run with `dest_view.run_command(...)`. That is how a view is edited from an event handler (which has no `edit` token) while the edit token belongs to the right view.
5. **Re-entrancy guard.** `is_running` makes a nested `pipe_text` return immediately.
6. **Lazy destination.** The destination is created on demand. `prepare_copy()` (called at the start of each new run) resets the read-head to 0, clears the destination if it exists, and otherwise arms `prepare_create` and creates the view from a `set_timeout(..., 100)` callback. The source comment says creating the view inside the callback "breaks modify listening". Text that arrives during that 100 ms gap is accumulated in `self.buffer` and prepended when the view appears.
7. **Rebuild from the source.** If the destination is gone (`dest_view is None`, which the listener sets in `on_close`), the next call recreates it and first copies `Region(0, read-head)` from the source with `content_prepend`, so the destination is always reconstructible from the source.

## 2. What was tested, and what happened (all VERIFIED live, ST 4215)

| # | Scenario | Result |
|---|---|---|
| S1 | Source grows by appends, `pipe_text` after each | Destination identical to source |
| S2 | Text arrives before the destination view exists (buffer path), then more text | Buffer held `one\ntwo\n`; destination identical after creation and a later delta |
| S3a | User closes the destination; `pipe_text` still holds the stale view object | No error, **the new text is silently lost** (read-head still advances to the end) |
| S3b | Same, but `dest_view` reset to `None` (what `BuildListener.on_close` does) | Destination recreated with the full history, identical to source |
| S4 | Source cleared and regrown smaller, **without** `prepare_copy` | Destination garbled (`'hel'` vs source `'hi\n'`) |
| S5 | An earlier part of the source is edited in place, then appended to | The edit never reaches the destination (destination stays `aaaa`, source has `XXXX`) |
| S6 | The user types into the destination, then the source grows | Destination **corrupted and never recovers**: the delta overwrote the wrong offsets (`'line1\nline2\nYPED '`) |
| S7 | `pipe_text` called while the guard is set, then called again | Nothing lost: the skipped delta is picked up by the next call |
| S8 | Non-ASCII and non-BMP text (`é`, 😀, CJK) | Identical; offsets are in characters (source `size()` 20 = `len(str)` 20) |
| S9 | Cost per step as the source grows 4,000 characters per step to 400,000 | **Delta copy: 0.45 ms (first 10 steps), 0.61 ms (last 10). Copy-everything each time: 0.94 ms, then 10.66 ms.** The delta cost stays flat; re-copying grows with the total |

## 3. What the results say about the design (VERIFIED for the properties, UNVERIFIED for the wording of the conclusions)

- **It is correct only under three assumptions:** the source is **append-only between resets** (S4, S5), the destination is **written only by the pipe** (S6), and someone tells the pipe when the destination went away (S3a). Break any one and it either loses text silently or corrupts the destination permanently. There is no checksum, size check or resync.
- **The read-head makes it idempotent and coalescing.** A skipped or missed event costs nothing, because the next call copies everything since the read-head (S7). That is why an event-driven design with no timer is safe.
- **The destination is derived state.** Because the source keeps everything, the destination can be thrown away and rebuilt at any moment (S3b). This is the strongest property of the design.
- **Constant-cost updates** (S9) are the reason for the read-head. Anything that re-copies the whole buffer per event grows linearly with output size.
- **Cheap hardening** that Buildview does not have (UNVERIFIED, not tested): before applying a delta, check `dest.size() == source_last_pos` and on a mismatch rebuild the destination from the source instead of writing at stale offsets. That single invariant would turn S4 and S6 from silent corruption into a self-heal. Inserting at the end of the destination instead of replacing `Region(prev, new)` would also avoid overwriting user text, though the offsets would still be wrong.

## 4. How the mechanism could carry over (UNVERIFIED, no GhostShell code was read for this section)

The pipe applies wherever GhostShell wants a **second view that follows text produced elsewhere** and that text is append-only (or can be treated as such):

- A follower/transcript view that trails a source view or output panel without re-copying it on every change.
- Tapping a Sublime output panel (for example `window.get_output_panel("exec")`, as Buildview does) and feeding it somewhere else.
- The three ideas that transfer even when the source is not append-only: a read-head for the immutable part, a size/offset invariant with rebuild-from-source as the recovery path, and tiny `TextCommand`s on the target view as the only write path.

Limits to keep in mind: a live terminal screen is redrawn in place, so it is **not** append-only; a read-head would only fit an immutable history region, not the live screen. If the goal is only a second view of the same text, Sublime's own `clone_file` gives two views of one buffer with no piping at all; Buildview needs piping because its source is an output panel, which cannot be cloned, and its destination must be a persistent tab.
