# Open defects in the terminal render path

Measured 2026-11-05 on the main Sublime (4215-class host, Python 3.14), omp
profile, TSP off. Every number below came from the live editor, not from a
model of it.

## 1. `sublime_text.exe` retains memory per frame while a terminal tab lives

**Symptom.** The editor process grows while an agent session streams and never
gives the memory back until the tab is closed. On 2026-11-05 the Windows
Resource-Exhaustion-Detector logged five "low virtual memory" events between
18:25 and 18:45 naming `sublime_text.exe` at 37.2 -> 37.8 GB of commit against
a 23.2 GB limit; the machine died and was rebooted. No Sublime crash dump was
written — the OS went first — so nothing was recorded except those events.

**What it is not.** Not `plugin_host` (flat at 119-132 MB throughout), not the
broker (`pythonw`, flat 25 MB), not `bun`/node MCP servers (flat), not handles
or threads (flat at 641 / 16), not OpenUri (A/B with its renderer at 600 s vs
500 ms changed nothing, and growth continued with a plain file tab in front).

**What it is.** Sublime keeps a per-view record of each edit and each
`add_regions` call (it is what undo restores). The paint did two things per
frame that fed that record without changing the screen:

- re-issued `add_regions` for every colour run (~2.4 K scopes) every frame;
- passed the whole frame text through `ai_terminal_render`'s command args,
  which Sublime serializes and keeps per command.

**Fixed** in `1bed16a`: `_same_regions()` reads the live set with
`view.get_regions(key)` and skips the write when it matches; the renderer hands
the text over on `term._frame_text` (`text_from_term=True`) the same way it
already handed over `regions`.

**Measured, same 10 MB burst into the same tab:** +94 MB retained before,
+5 and +7 MB after. Screen unchanged.

**Still open.** A residual ~14 KB retained per rendered line of output survives
the fix (3000 lines of output cost ~42 MB). It scales with what is written into
the view, so it is bounded by the window size — which is the next section.

### Repro harness

```
# 10 MB of text through a terminal tab, measure commit before/after
$line = "0123456789abcdef" * 64 + "`r`n"; $sb = New-Object Text.StringBuilder
while ($sb.Length -lt 10MB) { [void]$sb.Append($line) }
[IO.File]::WriteAllText("$env:TEMP\burst10.txt", $sb.ToString())
# then in the tab:  Get-Content -Raw $env:TEMP\burst10.txt
Get-Process sublime_text | % { [int]($_.PrivateMemorySize64/1MB) }
```

## 2. Reattach/reload allocates ~135 MB against a 2 MB broker replay

A plugin reload (any save in the live package) followed by the tab reattaching
itself raised `sublime_text.exe` from 508 MB to 643 MB in one step, with a
2 MiB broker ring as the only input — 67x amplification. Not yet reproduced in
isolation, not yet attributed to `_reattach_broker_view` /
`finish_bootstrap` / `_sync_scrollback`. `ai/DURABLE_BROKER_SCROLLBACK.md`
already plans to stop `_seed_restored_history` on the replay path and import
native scrollback in `finish_bootstrap`; whether those changes bound this is
unmeasured.

## 3. TSP oscillation on resume

Phantom (`tsp`) rendering oscillates the viewport after a session is resumed,
to the point that the command line cannot be reached; reported again
2026-11-05 ("fine until I did a resume, then it oscillated again"). Not
reproduced without TSP, so it is TSP-only, and the omp profile runs with
`tsp_enabled: false` because of it. Not touched by the render-path fixes
above.

# Design invariant: scrollback depth is 300 lines

`scrollback_history_size: 300` is **not** a memory workaround and must not be
raised to "fix" long sessions.

- The rendered document is meant to be about one screen tall so the minimap's
  position *is* "where am I". At 5,000 lines the minimap shows a fraction of
  the document and its position no longer correlates with the reader's place —
  it is deceptive unless you read line numbers, which nobody does.
- Every proportional control degrades with depth, unavoidably: measured live at
  5,000 lines, one pixel of minimap drag moves 8 lines. Higher numbers make it
  worse (a PyCharm scrollback of 20,000 lines cost Donald the ability to scroll
  back to any response after two hours).
- The 300 value also keeps the layout short enough that wheel and page keys
  stay usable, and it bounds the residual retention in defect 1.

**Deep history, if it is ever wanted, is a windowed pager**: keep the document
at ~one screen, step through the stored history by an offset, and report the
position in the status line (for example "lines 1200-1330 of 4870") instead of
inferring it from the minimap. Anything that lengthens the document breaks the
minimap contract, the proportional scrubbers, and the memory bound together.

Reading an old agent session today does not need any of that: GhostShell writes
it to `<log_root>/session_text_logs/ai_<timestamp>.log`, which is a normal file
where search, Go To Line and scrolling are the right tools.

# Fixed along the way: PageUp/PageDown did nothing on this profile

`fda4830`. The omp profile sets `scroll_manipulation_enabled: false`, and
`_page_scroll()` moved the viewport through `_set_viewport()`, which is a
deliberate no-op in exactly that state — so both page-key branches swallowed
the key regardless of `page_keys_to_pty`. `_page_scroll()` now reports whether
it acted and the caller falls back to Sublime's own `move by pages`.
