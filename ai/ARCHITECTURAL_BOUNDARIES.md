# GhostShell architectural boundaries

## Why this document exists

GhostShell combines four responsibilities that comparable Sublime Text
packages usually keep separate:

1. launch and communicate with an interactive process;
2. emulate a terminal and paint it into a Sublime view;
3. keep the process alive while Sublime Text is closed;
4. reconstruct the old screen and scrollback after reconnecting.

The fourth responsibility is the dangerous one. A terminal byte stream is a
sequence of editing operations, not a transcript. Applications can erase,
move, overwrite, resize, and redraw earlier material. Exact reconstruction
requires the same initial state, geometry, complete relevant byte stream, and
terminal-emulator behavior.

## Evidence from the Sublime package ecosystem

The local June 2026 Package Control census covers roughly 4,878 packages. Its
most important general finding is that there is no standard package shape:
only 14% of resourced packages contain all of commands, menus, keymaps, and
settings. Successful packages reduce their scope rather than conforming to a
single architecture.

The closest comparisons are more instructive than the other thousands:

- **SublimeREPL** appends decoded subprocess output to a text view. Closing
  the view closes/kills the subprocess. Its persistent history is command
  history stored by external REPL identity; it does not preserve terminal
  display state or reconnect a surviving process. Its per-language menus are
  declarative adapters over a small process abstraction.
- **TerminalView** implemented a PTY, terminal emulator, scrollback, and
  Sublime buffer integration. Its documentation explicitly says project
  switching or restarting Sublime restarts terminal views, because there is
  no obvious way to restore the earlier sessions.
- **Terminus** implements a real cross-platform terminal and continuous
  in-session history. Its documentation acknowledges terminal-buffer memory
  costs and that some scrollback color information cannot be preserved across
  view-mode changes.
- **terminus-persistence** deliberately persists only whether a Terminus panel
  was visible and reopens it after startup. It stores a boolean and panel
  name, not a process, emulator state, or transcript.
- **Terminal** (the external-terminal launcher) avoids emulation entirely. It
  resolves the relevant working directory and delegates terminal ownership to
  a dedicated terminal application.

Sources:

- Local census: `C:\Users\donal\data\st_packages\FINDINGS.md`
- Local SublimeREPL:
  `C:\Users\donal\AppData\Roaming\Sublime Text\Packages\SublimeREPL`
- TerminalView: <https://github.com/Wramberg/TerminalView>
- Terminus: <https://github.com/randy3k/Terminus>
- terminus-persistence:
  <https://packagecontrol.io/packages/terminus-persistence>
- Terminal: <https://github.com/SublimeText/Terminal>

## GhostShell's authority hierarchy

These artifacts answer different questions and must not be conflated:

1. **Agent-native transcript or JSONL** is authoritative for conversation
   content when the agent provides it.
2. **The live terminal emulator** is authoritative for current interactive
   screen state.
3. **The tab text log** records exactly the latest Sublime tab paint. It is a
   screen snapshot, not a guaranteed conversation transcript.
4. **The asciicast** records terminal input/output operations for diagnosis.
   It is evidence, not canonical conversational data.

No conversion between these forms is guaranteed to be lossless.

## Supported restart promise

GhostShell should promise:

- the detachable child process can survive a Sublime restart;
- reconnecting permits continued interaction with that same process;
- a bounded raw-output replay makes a best effort to reconstruct useful recent
  screen state;
- failure to reconstruct old scrollback does not destroy the surviving
  process or its agent-native transcript.

GhostShell should **not** promise exact, unlimited scrollback reconstruction
for arbitrary terminal applications after Sublime exits.

The broker replay budget is therefore bounded and configurable. Its 2 MiB
default targets roughly 300 recent displayed lines for development recovery,
with margin around Codex redraw boundaries; it is not a session archive.
Enlarging it is not a general correctness proof: it trades memory and replay
work for a larger reconstruction window and exercises complex full-redraw
paths.

## Stop rule for restart recovery

Test one newly created detachable session across a real Sublime restart. If
the 2 MiB replay window reconstructs a usable screen without corruption,
retain the bounded design. If it still loses or corrupts substantial history:

1. preserve process reattachment;
2. preserve agent-native transcript discovery;
3. report that terminal scrollback restoration was partial;
4. do not add casts, JSONL synthesis, or more emulator merge heuristics to the
   automatic reattach path without a separate design decision and a failing
   isolated test.

This stop rule prevents a presentation convenience from becoming an
unbounded session-database and terminal-checkpointing project.

## Profile policy

Profiles remain declarative data. That is a sound choice and resembles
SublimeREPL's menu-driven adapters. A profile should describe launch command,
environment, terminal capabilities, and bounded behavior switches. Agent-
specific code belongs in a small adapter only when behavior cannot be stated
as data without conditionals spreading through the terminal core.

Adding another agent must not require a new persistence format or redefine
where transcripts live. Transcript discovery should be an optional adapter
capability, independent from PTY rendering and process reattachment.

## Practical review question

Before accepting a terminal feature, ask:

> Does this improve live interaction, or is it attempting to turn terminal
> operations into an authoritative conversation database?

If it is the latter, prefer the agent's native structured transcript and keep
GhostShell's terminal responsibility bounded.
