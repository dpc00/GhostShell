"""Rebuild the Agents and Shells submenus in Main.sublime-menu.

Sublime Text has no API to add menu items while it runs, so the list is built here and checked in.
Run this by hand after adding or removing an agent in terminal/agent_catalog.py or a profile in
ai_terminal.sublime-settings:

    python tools/regen_agent_menu.py           # rewrite Main.sublime-menu
    python tools/regen_agent_menu.py --check   # exit 1 if the menu is out of date; writes nothing

tests/test_agent_menu.py runs the same check, so a stale menu fails the test suite.
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from terminal import agent_menu  # noqa: E402
from terminal.agent_catalog import CATALOG  # noqa: E402

MENU_PATH = os.path.join(REPO, "Main.sublime-menu")
SETTINGS_PATH = os.path.join(REPO, "ai_terminal.sublime-settings")
TOP_NODE_ID = "ai_terminal"

# Commands whose menu items are removed: the two pickers and the "default profile" launcher
# (the owner does not use a default profile, and pickers are being removed).
REMOVED_COMMANDS = ("ai_terminal_launcher",)
REMOVED_CAPTIONS = ("Default Profile",)


def strip_json_comments(text):
    """Remove // comments and trailing commas from a .sublime-settings file so json can read it.

    String literals are respected, so a "//" inside a quoted value is left alone.
    """
    kept = []
    inside_string = False
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if inside_string:
            kept.append(char)
            if char == "\\" and index + 1 < length:
                kept.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                inside_string = False
            index += 1
            continue
        if char == '"':
            inside_string = True
            kept.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] != "\n":
                index += 1
            continue
        kept.append(char)
        index += 1
    return re.sub(r",\s*([}\]])", r"\1", "".join(kept))


def settings_profile_names():
    """The profile names defined in ai_terminal.sublime-settings."""
    with open(SETTINGS_PATH, encoding="utf-8") as handle:
        data = json.loads(strip_json_comments(handle.read()))
    return set(data.get("profiles", {}))


def find_node(nodes, node_id):
    """Depth-first search for the menu node with this id, or None."""
    for node in nodes:
        if node.get("id") == node_id:
            return node
        found = find_node(node.get("children") or [], node_id)
        if found is not None:
            return found
    return None


def ensure_node(children, node_id, caption, position):
    """Return the child with this id, creating an empty one at `position` if it is missing."""
    existing = next((child for child in children if child.get("id") == node_id), None)
    if existing is not None:
        return existing
    node = {"id": node_id, "caption": caption, "children": []}
    children.insert(position, node)
    return node


def is_separator(item):
    return item.get("caption") == "-"


def build_menu_tree():
    """The tree Main.sublime-menu should hold: the current file with the two submenus rebuilt."""
    with open(MENU_PATH, encoding="utf-8") as handle:
        tree = json.load(handle)
    top = find_node(tree, TOP_NODE_ID)
    children = top["children"]

    children[:] = [
        child for child in children
        if child.get("command") not in REMOVED_COMMANDS and child.get("caption") not in REMOVED_CAPTIONS
    ]
    # Collapse doubled separators that removing an item may have left behind.
    tidy = []
    for child in children:
        if is_separator(child) and (not tidy or is_separator(tidy[-1])):
            continue
        tidy.append(child)
    children[:] = tidy

    agents_node = ensure_node(children, agent_menu.AGENTS_NODE_ID, "Agents", 0)
    shells_node = ensure_node(children, agent_menu.SHELLS_NODE_ID, "Shells", 1)
    if len(children) < 3 or not is_separator(children[2]):
        children.insert(2, {"caption": "-"})

    names = agent_menu.known_profile_names(CATALOG, settings_profile_names())
    agents, shells = agent_menu.split_agents_and_shells(names)
    agents_node["children"] = agent_menu.menu_entries(agents)
    shells_node["children"] = agent_menu.menu_entries(shells)
    return tree


def render_menu_text():
    """The exact text to write: the tree as 4-space-indented JSON."""
    return json.dumps(build_menu_tree(), indent=4, ensure_ascii=False) + "\n"


def main(argv):
    with open(MENU_PATH, encoding="utf-8", newline="") as handle:
        current = handle.read()
    newline = "\r\n" if "\r\n" in current else "\n"
    wanted = render_menu_text()
    if current.replace("\r\n", "\n") == wanted:
        print("Main.sublime-menu is up to date")
        return 0
    if "--check" in argv:
        print("Main.sublime-menu is OUT OF DATE; run: python tools/regen_agent_menu.py")
        return 1
    with open(MENU_PATH, "w", encoding="utf-8", newline="") as handle:
        handle.write(wanted.replace("\n", newline))
    print("Main.sublime-menu rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
