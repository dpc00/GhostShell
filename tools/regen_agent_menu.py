"""Rebuild the Agents and Shells submenus in Main.sublime-menu and Side Bar.sublime-menu.

Sublime Text has no API to add menu items while it runs, so the lists are built here and checked in.
Run this by hand after adding or removing an agent in terminal/agent_catalog.py or a profile in
ai_terminal.sublime-settings:

    python tools/regen_agent_menu.py           # rewrite both menu files
    python tools/regen_agent_menu.py --check   # exit 1 if either is out of date; writes nothing

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
SIDEBAR_PATH = os.path.join(REPO, "Side Bar.sublime-menu")
SETTINGS_PATH = os.path.join(REPO, "ai_terminal.sublime-settings")
TOP_NODE_ID = "ai_terminal"

# Menu items that are removed: the picker, and the "open with the default profile" items (the owner
# does not use a default profile, and pickers are gone).
REMOVED_COMMANDS = ("ai_terminal_launcher", "ai_terminal_history")
REMOVED_CAPTIONS = ("Default Profile", "Open Ai Terminal here...")


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


def ensure_node(items, node_id, caption, position):
    """Return the item with this id, creating an empty submenu at `position` if it is missing."""
    existing = next((item for item in items if item.get("id") == node_id), None)
    if existing is not None:
        return existing
    node = {"id": node_id, "caption": caption, "children": []}
    items.insert(position, node)
    return node


def is_separator(item):
    return item.get("caption") == "-"


def drop_removed_and_tidy(items):
    """Remove the retired items, then collapse separators left doubled, leading or trailing."""
    items[:] = [
        item for item in items
        if item.get("command") not in REMOVED_COMMANDS and item.get("caption") not in REMOVED_CAPTIONS
    ]
    tidy = []
    for item in items:
        if is_separator(item) and (not tidy or is_separator(tidy[-1])):
            continue
        tidy.append(item)
    items[:] = tidy


def place_submenus(items, agents_id, shells_id, agents_caption, shells_caption):
    """Put the Agents and Shells submenus first, followed by one separator. Returns both nodes."""
    agents_node = ensure_node(items, agents_id, agents_caption, 0)
    shells_node = ensure_node(items, shells_id, shells_caption, 1)
    if len(items) < 3 or not is_separator(items[2]):
        items.insert(2, {"caption": "-"})
    return agents_node, shells_node


def sorted_names():
    names = agent_menu.known_profile_names(CATALOG, settings_profile_names())
    return agent_menu.split_agents_and_shells(names)


def build_menu_tree():
    """The tree Main.sublime-menu should hold: the current file with the two submenus rebuilt."""
    with open(MENU_PATH, encoding="utf-8") as handle:
        tree = json.load(handle)
    items = find_node(tree, TOP_NODE_ID)["children"]
    drop_removed_and_tidy(items)
    agents_node, shells_node = place_submenus(
        items, agent_menu.AGENTS_NODE_ID, agent_menu.SHELLS_NODE_ID, "Agents", "Shells"
    )
    agents, shells = sorted_names()
    agents_node["children"] = agent_menu.menu_entries(agents)
    shells_node["children"] = agent_menu.menu_entries(shells)
    return tree


def build_sidebar_tree():
    """The tree Side Bar.sublime-menu should hold. Entries pass "paths" so the right-clicked folder is used."""
    with open(SIDEBAR_PATH, encoding="utf-8") as handle:
        tree = json.load(handle)
    drop_removed_and_tidy(tree)
    agents_node, shells_node = place_submenus(
        tree, agent_menu.SIDEBAR_AGENTS_NODE_ID, agent_menu.SIDEBAR_SHELLS_NODE_ID,
        "Ai Terminal Agents", "Ai Terminal Shells",
    )
    agents, shells = sorted_names()
    agents_node["children"] = agent_menu.menu_entries(agents, extra_args={"paths": []})
    shells_node["children"] = agent_menu.menu_entries(shells, extra_args={"paths": []})
    return tree


def render(tree):
    """The exact text to write: the tree as 4-space-indented JSON."""
    return json.dumps(tree, indent=4, ensure_ascii=False) + "\n"


def update_file(path, tree, check_only):
    """Write `tree` to `path` if it differs (keeping the file's line endings). Returns True if up to date."""
    with open(path, encoding="utf-8", newline="") as handle:
        current = handle.read()
    newline = "\r\n" if "\r\n" in current else "\n"
    wanted = render(tree)
    name = os.path.basename(path)
    if current.replace("\r\n", "\n") == wanted:
        print("%s is up to date" % name)
        return True
    if check_only:
        print("%s is OUT OF DATE; run: python tools/regen_agent_menu.py" % name)
        return False
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(wanted.replace("\n", newline))
    print("%s rewritten" % name)
    return True


def main(argv):
    check_only = "--check" in argv
    results = [
        update_file(MENU_PATH, build_menu_tree(), check_only),
        update_file(SIDEBAR_PATH, build_sidebar_tree(), check_only),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
