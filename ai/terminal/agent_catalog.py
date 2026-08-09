"""Known-CLI database: everything ai_terminal has learned about specific
coding-agent TUIs, keyed by the command used to launch them.

This is the accumulated cost of running each agent through ai_terminal --
prompt-detection quirks (caret.py), mouse/scroll quirks (ai_terminal.py's
_mouse_handling_enabled kill switch), and the spawn_env each one needs. It
exists so that knowledge survives a fresh settings file instead of being
re-discovered: a detection script matches an installed executable's command
name against CATALOG and looks up the profile to offer, rather than guessing.

Entries here are generic (no personal paths, no secrets) -- they're the
starting point for a profile, not a ready-to-paste one. A machine-specific
shim path (e.g. Junie's ~/.local/bin/junie.bat) still has to be supplied by
whoever wires up that profile locally.
"""

CATALOG = {
    "claude": {
        "display_name": "Claude",
        "launch_command": ["claude"],
        "spawn_env": {
            "AI_TERMINAL_LOG_LINES": "1",
            "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
        },
        "notes": (
            "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1 forces the classic "
            "main-screen renderer instead of the fixed ~60-row alt-screen "
            "matrix, so real scrollback + ST folding work. Added in Claude "
            "Code v2.1.132, takes precedence over CLAUDE_CODE_NO_FLICKER. "
            "Often CUPs to the footer to repaint token/cost between prompt "
            "updates -- caret.py treats that as noise, not a real caret move."
        ),
    },
    "codex": {
        "display_name": "Codex",
        "launch_command": ["codex"],
        "spawn_env": {
            "AI_TERMINAL_LOG_LINES": "1",
            "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
        },
        "notes": None,
    },
    "opencode": {
        "display_name": "OpenCode",
        "launch_command": ["opencode"],
        "spawn_env": {
            "AI_TERMINAL_LOG_LINES": "1",
            "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
        },
        "notes": None,
    },
    "gemini": {
        "display_name": "Gemini",
        "launch_command": ["gemini.cmd"],
        "spawn_env": {
            "AI_TERMINAL_LOG_LINES": "1",
            "GEMINI_PROMPT_QUEUE": "false",
        },
        "notes": "Windows shim is gemini.cmd, not gemini.",
    },
    "grok": {
        "display_name": "Grok Build",
        "launch_command": ["grok"],
        "spawn_env": {"AI_TERMINAL_LOG_LINES": "1"},
        "notes": (
            "Keeps the hardware cursor on its input row (`│ > … │` "
            "in a box) and paints a clock on the same row after a long "
            "prompt (e.g. '7:52 AM'), which find_prompt_row/content_end in "
            "caret.py must not mistake for trailing input text. Live input "
            "box must win over earlier history lines starting with '> '."
        ),
    },
    "qwen": {
        "display_name": "Qwen",
        "launch_command": ["qwen"],
        "spawn_env": {
            "AI_TERMINAL_LOG_LINES": "1",
            "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
        },
        "notes": (
            "Never emits a real ANSI scroll (Screen.history stays empty), "
            "so DEC mouse tracking's click/drag handling broke scrolling "
            "(\"can't scroll past the top\"). This is *why* ai_terminal's "
            "global mouse_handling kill switch defaults off; do not set "
            "mouse_handling: true on this profile."
        ),
    },
    "vibe": {
        "display_name": "Vibe",
        "launch_command": ["vibe"],
        "spawn_env": {
            "AI_TERMINAL_LOG_LINES": "1",
            "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
            "LOG_LEVEL": "DEBUG",
        },
        "mouse_handling": True,
        "notes": (
            "Textual app: manages its own scroll region and never emits a "
            "real ANSI scroll, so history stays empty and PageUp/PageDown "
            "reach nothing -- mouse wheel is the only way to see scrolled-off "
            "content. Opts back into the global mouse_handling kill switch "
            "(safe here: Vibe doesn't enable DEC mouse tracking's click/drag, "
            "which is what broke Qwen)."
        ),
    },
    "kimi": {
        "display_name": "Kimi",
        "launch_command": ["kimi"],
        "spawn_env": {
            "AI_TERMINAL_LOG_LINES": "1",
            "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
        },
        "notes": None,
    },
    "kiro-cli": {
        "display_name": "Kiro",
        "launch_command": ["kiro-cli"],
        "spawn_env": {
            "AI_TERMINAL_LOG_LINES": "1",
            "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
        },
        "notes": None,
    },
    "mimo": {
        "display_name": "Mimo",
        "launch_command": ["mimo"],
        "spawn_env": {
            "AI_TERMINAL_LOG_LINES": "1",
            "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
        },
        "notes": None,
    },
    "jcode": {
        "display_name": "jcode",
        "launch_command": ["jcode"],
        "spawn_env": {
            "AI_TERMINAL_LOG_LINES": "1",
            "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
        },
        "notes": None,
    },
    "junie": {
        "display_name": "Junie",
        "launch_command": ["junie"],
        "spawn_env": {"AI_TERMINAL_LOG_LINES": "1"},
        "notes": (
            "JetBrains Junie CLI. The interactive TUI in the project cwd, "
            "not the PyCharm plugin panel. Its shim is often installed "
            "somewhere not yet on PATH for a long-lived Sublime process "
            "(e.g. ~/.local/bin/junie.bat) -- prefer a full path over the "
            "bare command if launches fail after the shim was installed "
            "post-ST-startup. Pads with spaces before its '>' prompt marker "
            "(see caret.py's spaced-prompt handling)."
        ),
    },
    "agy": {
        "display_name": "Antigravity",
        "launch_command": ["agy"],
        "spawn_env": {
            "AI_TERMINAL_LOG_LINES": "1",
            "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
        },
        "notes": (
            "Google Antigravity CLI. Shim (e.g. "
            "AppData\\Local\\agy\\bin\\agy.exe) is often not on PATH until "
            "the shell that launches ST is restarted -- same reasoning as "
            "Junie: prefer a full path."
        ),
    },
}
