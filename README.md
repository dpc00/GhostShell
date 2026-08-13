# GhostShell

A bare-bones, owned terminal for Sublime Text — no Terminus dependency. Pure
ctypes against the Windows ConPTY (Pseudoconsole) API, plus a cursor-aware
ANSI renderer (backed by libghostty-vt) tailored to the subset TUI agents
(Claude Code, OpenCode, Codex, etc.) emit.

## Supported CLIs

Each one gets its own tuned profile in `ai_terminal.sublime-settings` —
mouse tracking, alt-screen handling, and page-key routing are calibrated
per-agent, not guessed at generically:

- **Claude Code** (Anthropic) — plus a `--chrome` variant
- **Codex** (OpenAI)
- **OpenCode**
- **Grok Build** (xAI)
- **Antigravity** (Google)
- **Qwen Code** (Alibaba)
- **Kimi Code** (Moonshot AI)
- **Kiro** (AWS)
- **Junie** (JetBrains)
- **jcode**
- **Mimo** (Xiaomi)
- **Vibe** (Mistral)

Claude Code, Codex, OpenCode, and Qwen Code each also have an
`→⇢⇨ Ollama`-routed variant for running against Ollama's cloud model
catalog instead of the provider's own backend.

Plain shells (`Bash`, `PowerShell`, `WSL Bash`, `Dos Console`, `cmd.exe`)
are supported too, as themselves — no agent-specific tuning applied.

## Layout

```
ai/
  ai_terminal.py      -- Sublime adapter: ConPTY, view I/O, commands, color-scheme
  terminal/            -- pure core, unit-testable without Sublime
    screen.py, parser.py, colors.py, keys.py, render.py, caret.py, mouse.py
    ghostty_engine.py, ghostty_vt.py -- ctypes bindings to libghostty-vt
    launcher.py, profile_availability.py, pty_env.py -- profile/launch plumbing
    history_scan.py, usage_scan.py   -- scrollback/usage helpers
    bin/ghostty-vt.dll  -- libghostty-vt, built from https://github.com/ghostty-org/ghostty
PluginLoader.py        -- top-level plugin entry point (ST only auto-loads top-level .py)
Default.sublime-keymap -- key-forwarding bindings, gated by setting.ai_terminal_view
Main.sublime-menu       -- Tools > Ai Terminal submenu
Default.sublime-commands, Context.sublime-menu, Side Bar.sublime-menu,
Tab Context.sublime-menu -- command palette / context-menu entries
ai_terminal.sublime-settings     -- profiles (shells/agents), rendering knobs
ai_terminal.sublime-color-scheme -- color scheme with the ai.terminal.* scopes
tools/                  -- check_import.py (import sanity check), stale-scheme cleanup script
tests/                  -- unit tests for ai/terminal/*, no Sublime required
```

See [COMMANDS.md](COMMANDS.md) for every registered command: ST command name,
command palette entry, menu location(s), and keybinding.

## Installing

Symlink this repo into your Sublime Text `Packages/` directory:

```powershell
New-Item -ItemType SymbolicLink -Path "$env:APPDATA\Sublime Text\Packages\GhostShell" -Target "<path to this repo checkout>"
```

If the symlinked folder name is anything other than `GhostShell`, update the
`GhostShell.ai.ai_terminal` import prefix in `PluginLoader.py` to match.

### Getting libghostty-vt.dll

The DLL isn't tracked in git (it's a built binary artifact). Download it and
place it at `ai/terminal/bin/ghostty-vt.dll`:

https://drive.google.com/open?id=1d1GyMHTtVN71RVYKjnsEnRzfBqrJwA1h

To build it yourself instead: clone https://github.com/ghostty-org/ghostty,
run `zig build`, and copy `zig-out/bin/ghostty-vt.dll` to the path above.

## Testing

```
python -m unittest discover -s tests -v
```
