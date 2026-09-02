"""Regenerate the checked-in "All Agents" list in Main.sublime-menu.

Sublime Text has no API for dynamic menu items (confirmed by Sublime's
creator: https://forum.sublimetext.com/t/dynamic-menu-items/5352). Unlike the
per-machine generated settings file, the agent list here is *not* rewritten
at runtime -- a shipped package's tracked files must not carry one
contributor's local CLI inventory (see ai/SETTINGS_HYGIENE.md). Instead this
script computes the full known-agent set (agent_catalog.CATALOG's display
names, plus the shipped ai_terminal.sublime-settings profiles, minus the
Shells profiles which get their own separate Tools > Shells submenu) and
splices it into Main.sublime-menu's hand-authored "all-agents" node by id,
leaving every other entry in that file untouched.

Run this by hand after adding/removing a CATALOG entry or a hand-authored
profile, then commit the result:

    python tools/regen_agent_menu.py

tests/test_launcher_flow.py has a drift check that fails if this script's
output would differ from what is currently committed.
"""

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.sublime_stub import install as install_sublime_stubs  # noqa: E402

SHELL_PROFILE_NAMES = {"Bash", "PowerShell", "Dos Console", "WSL Bash"}


def _strip_json_comments(text):
    """Strip // line comments and trailing commas from a .sublime-settings
    file, respecting string literals, so it can be parsed as plain JSON."""
    out = []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    cleaned = "".join(out)
    return re.sub(r",\s*([}\]])", r"\1", cleaned)


def _hand_authored_profile_names():
    path = os.path.join(REPO, "ai_terminal.sublime-settings")
    with open(path, encoding="utf-8") as f:
        data = json.loads(_strip_json_comments(f.read()))
    return set(data.get("profiles", {}).keys())


def known_agent_profile_names(ai_terminal_module):
    catalog_names = {
        entry["display_name"] for entry in ai_terminal_module._AGENT_CATALOG.values()
    }
    return (catalog_names | _hand_authored_profile_names()) - SHELL_PROFILE_NAMES


def _find_menu_node(tree, node_id):
    stack = list(tree) if isinstance(tree, list) else [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("id") == node_id:
                return node
            stack.extend(node.get("children") or [])
    return None


def regen(ai_terminal_module, menu_path):
    with open(menu_path, encoding="utf-8") as f:
        tree = json.load(f)
    node = _find_menu_node(tree, "all-agents")
    if node is None:
        raise SystemExit("regen_agent_menu: Main.sublime-menu has no 'all-agents' node")
    names = known_agent_profile_names(ai_terminal_module)
    node["children"] = ai_terminal_module.build_agent_menu_json(names)
    with open(menu_path, "w", encoding="utf-8") as f:
        json.dump(tree, f, indent=4, ensure_ascii=False)
        f.write("\n")
    return len(names)


def main():
    install_sublime_stubs()
    import ai_terminal

    menu_path = os.path.join(REPO, "Main.sublime-menu")
    count = regen(ai_terminal, menu_path)
    print("regen_agent_menu: wrote %d agents to %s" % (count, menu_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
