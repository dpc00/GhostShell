"""The Agents and Shells submenus of the Ai Terminal menus (pure Python, unit-testable).

Sublime menus are static files: the plugin API has no call to add menu items. So the list of
agents is built ahead of time by tools/regen_agent_menu.py and checked in, and a test fails if
the checked-in menus and what the script would build ever differ.

The same shipped menus serve every machine. An agent whose program is not installed is hidden
at run time (AiTerminalOpenHereCommand.is_visible), so each user sees only what they have.
"""
# The profiles that are shells rather than agents. They are listed apart from the agents on the menus.
SHELL_PROFILES = ("Bash", "PowerShell", "Dos Console", "WSL Bash")

# Ids of the submenu nodes: inside the Ai Terminal menu (Main.sublime-menu) ...
AGENTS_NODE_ID = "all-agents"
SHELLS_NODE_ID = "shells"
# ... and at the top of the sidebar's right-click menu (Side Bar.sublime-menu).
SIDEBAR_AGENTS_NODE_ID = "sidebar-agents"
SIDEBAR_SHELLS_NODE_ID = "sidebar-shells"


def known_profile_names(settings_profile_names):
    """Every name that belongs on the menus: the profiles defined in ai_terminal.sublime-settings."""
    return set(settings_profile_names)


def split_agents_and_shells(names):
    """Return (agents, shells), each sorted A-Z ignoring case. Shells are the names in SHELL_PROFILES."""
    ordered = sorted(set(names), key=lambda name: name.lower())
    shells = [name for name in ordered if name in SHELL_PROFILES]
    agents = [name for name in ordered if name not in SHELL_PROFILES]
    return agents, shells


def menu_entries(names, extra_args=None):
    """One menu entry per name. Choosing it launches that profile.

    `extra_args` are added to every entry's arguments. The sidebar menu passes {"paths": []} so
    Sublime fills in the folder that was right-clicked.
    """
    extra_args = extra_args or {}
    return [
        {
            "caption": name,
            "command": "ai_terminal_open_here",
            "args": dict(extra_args, profile=name),
        }
        for name in names
    ]
