# GhostShell

A bare-bones, owned terminal for Sublime Text — no Terminus dependency. Pure
ctypes against the Windows ConPTY (Pseudoconsole) API, plus a cursor-aware
ANSI renderer (backed by libghostty-vt) tailored to the subset TUI agents
(Claude Code, OpenCode, Codex, etc.) emit.

Extracted from the [SText](../SText) user-config repo so it can live as a
standalone Sublime Text package.

## Layout

```
ai/
  ai_terminal.py     -- Sublime adapter: ConPTY, view I/O, commands, color-scheme
  terminal/           -- pure core (Screen, Parser, colours, keys, render); unit-testable
    bin/ghostty-vt.dll -- libghostty-vt, built from https://github.com/ghostty-org/ghostty
PluginLoader.py        -- top-level plugin entry point (ST only auto-loads top-level .py)
Default.sublime-keymap -- key-forwarding bindings, gated by setting.ai_terminal_view
Main.sublime-menu       -- Tools > Ai Terminal submenu
Default.sublime-commands, Context.sublime-menu, Side Bar.sublime-menu,
Tab Context.sublime-menu -- command palette / context-menu entries
ai_terminal.sublime-settings     -- profiles (shells/agents), rendering knobs
ai_terminal.sublime-color-scheme -- color scheme with the ai.terminal.* scopes
tests/test_terminal_core.py      -- unit tests for ai/terminal/*, no Sublime required
```

## Installing

Symlink this repo into your Sublime Text `Packages/` directory:

```powershell
New-Item -ItemType SymbolicLink -Path "$env:APPDATA\Sublime Text\Packages\GhostShell" -Target "C:\Users\donal\projects\GhostShell"
```

If the symlinked folder name is anything other than `GhostShell`, update the
`GhostShell.ai.ai_terminal` import prefix in `PluginLoader.py` to match.

## Testing

```
python -m unittest tests.test_terminal_core -v
```
