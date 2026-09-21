"""The Agents and Shells submenus of the Ai Terminal menu (pure Python, unit-testable).

Sublime menus are static files: the plugin API has no call to add menu items. So the list of
agents is built ahead of time by tools/regen_agent_menu.py and checked in, and a test fails if
the checked-in menu and what the script would build ever differ.

The same shipped menu serves every machine. An agent whose program is not installed is hidden
at run time (AiTerminalOpenHereCommand.is_visible), so each user sees only what they have.
"""
from .launcher import SHELL_PROFILES

# Ids of the two submenu nodes inside the Ai Terminal menu in Main.sublime-menu.
AGENTS_NODE_ID = "all-agents"
SHELLS_NODE_ID = "shells"


def known_profile_names(catalog, settings_profile_names):
    """Every name that belongs on the menus.

    That is the display name of each agent in the catalog, plus every profile defined in
    ai_terminal.sublime-settings.
    """
    names = {entry["display_name"] for entry in catalog.values()}
    names.update(settings_profile_names)
    return names


def split_agents_and_shells(names):
    """Return (agents, shells), each sorted A-Z ignoring case. Shells are the names in SHELL_PROFILES."""
    ordered = sorted(set(names), key=lambda name: name.lower())
    shells = [name for name in ordered if name in SHELL_PROFILES]
    agents = [name for name in ordered if name not in SHELL_PROFILES]
    return agents, shells


def menu_entries(names):
    """One menu entry per name. Choosing it launches that profile in the working directory."""
    return [
        {"caption": name, "command": "ai_terminal_open_here", "args": {"profile": name}}
        for name in names
    ]
