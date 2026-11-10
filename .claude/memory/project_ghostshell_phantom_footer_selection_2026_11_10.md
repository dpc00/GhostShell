---
name: ghostshell-phantom-footer-selection-2026-11-10
description: SOLVED 2026-11-10 — the "status line selects itself, output freezes, keys dead until a click" bug: view.replace() on the line holding the caret leaves the replacement SELECTED; GhostShell's line-diff render did that to the status row every frame
metadata:
  type: project
---

# Phantom selection on the omp status line — ROOT CAUSE FOUND (2026-11-10)

## The cause (proven, not inferred)

`view.replace(edit, Region(line_start, line_end), new_text)` **on the line that holds the
caret** leaves the replacement *selected*. Reproduced on build 4215 with a standalone probe,
three cases, same view size:

| case | before | after |
|---|---|---|
| whole-buffer replace, caret at end | `regs=15-15 empty=True` | `regs=15-15 empty=True` |
| **replace the caret's line**, caret at its end | `regs=15-15 empty=True` | **`regs=15-20 empty=False`** |
| whole-buffer replace, caret at offset 0 | `regs=0-0 empty=True` | `regs=0-0 empty=True` |

GhostShell's render has exactly one path that replaces individual lines — the line-diff loop in
`AiTerminalRenderCommand` (`ai_terminal.py`, `view.replace(edit, Region(start, start+len(old_line)), new_line)`).
The omp status row is the **last** line, the caret sits at the end of it, and that row changes
every second (spinner + timer). So every frame that rewrote it manufactured a selection in that
row. Nothing else in the codebase ever adds a non-empty range — every `sel().add(...)` in
`ai_terminal.py` is `Region(pos, pos)`.

## Why it looked like a mysterious external writer

- The selection is always the status line, from its first character to its end, with the caret
  at the end — exactly the shape of "replace this line with this text".
- It appears with `idle=` tens of seconds (no human input) because it is made by the render.
- No `drag_select` or any text command precedes it, so every investigation looked for a plugin
  calling `view.sel()` and found none.
- The 2026-10-08 note's conclusion ("GhostShell never creates a non-empty selection") was true
  of explicit calls, and wrong about edits: the selection is a side effect of `view.replace`.

## Measured occurrences (recorder in `Packages\User\gs_sel_trace.py`)

Log: `C:\Users\donal\data\logs\ghostshell\sel_trace.log`, every line carrying `idle=` (ms since
the last physical input, `GetLastInputInfo`) — the discriminator no earlier investigation had.

```
02:21:08 idle=96109  ' ⠏ 2m · ◒ DeepSeek V4.1 Flash 👁 · 📁 sublime-commander · ⑂ ma'
02:23:15 idle=34375  ' ⠧ 34s · ◒ DeepSeek V4.1 Flash …'
02:29:25 idle=17281  ' ⠇ 17s · ◒ DeepSeek V4.1 Flash …'
03:02:18 idle=48937  ' ⠹ 48s · ◒ DeepSeek V4.1 Flash …'
```

The last one carries `SEL-STACK`, whose outermost frame is `ai_terminal.py:6140` — i.e.
`view.run_command("ai_terminal_render", …)`, GhostShell's own render. Sublime queues the
selection notification until after the command, so the stack names the command, not the line
inside it; the probe above supplies the missing half.

## Effect inside GhostShell (what turned it into a freeze)

1. `_selection_paint_blocked()` (`ai_terminal.py:5683`) blocks every paint while any range is
   non-empty, one frame every `_SELECTION_PAINT_BLOCK_MAX_S = 2.5` s. Log shows `block_age_ms`
   climbing and `size` frozen — "output stops".
2. `on_selection_modified()` (`ai_terminal.py:6500`) latches `term._user_owns_caret = True` on a
   non-empty range ("unambiguously user-owned") — the keyboard stops reaching the PTY until a
   click lands in the command-line box.

## Fix applied 2026-11-10

At the line-diff patch site, after the loop, restore the caret the frame intended, and **only
when the caret is GhostShell's to place** (`not term._user_owns_caret`) — so a person's real
selection is never touched:

```python
if term is not None and not getattr(term, "_user_owns_caret", False):
    sel_now = view.sel()
    if len(sel_now) == 1 and not sel_now[0].empty():
        _place_auto_caret(view, term, sel_now[0].b)
```

A previous attempt on 2026-11-10 was reverted at Donald's instruction: it cleared *any*
non-empty range in `on_selection_modified`, which hid the symptom and destroyed the evidence.
This one repairs the plugin's own edit artefact instead.

## Second, separate symptom (not the same bug)

"Position ends up in the scrollback": after a click, the view sat 228–737 characters short of
the bottom for 20+ seconds while output streamed (`at_bottom=False gap=228`, `follow=True`).
The `omp` profile ships `scroll_manipulation_enabled: false`, so `_set_viewport()` returns early
and GhostShell may not scroll the tab at all. That flag is a candidate fix and was left at its
shipped value.

## Removing the instrumentation

Delete `Packages\User\gs_sel_trace.py`; the log can then be deleted too. `debug_view_trace` is
currently `true` in `ai_terminal.sublime-settings` (self-disables after 6 h) and writes
`~/data/logs/ghostshell/view_trace.jsonl`.
