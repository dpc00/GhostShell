"""The Agents and Shells submenus: sorted, complete, in step with the code, and picker-free."""
import importlib.util
import json
import os

from terminal import agent_menu
from terminal.agent_catalog import CATALOG

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _regen_module():
    spec = importlib.util.spec_from_file_location(
        "regen_agent_menu_under_test", os.path.join(ROOT, "tools", "regen_agent_menu.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _committed_menu():
    with open(os.path.join(ROOT, "Main.sublime-menu"), encoding="utf-8") as handle:
        return json.load(handle)


def _node(tree, node_id):
    return _regen_module().find_node(tree, node_id)


def test_agents_are_sorted_ignoring_case_without_duplicates():
    agents, _shells = agent_menu.split_agents_and_shells(["b", "A", "c", "b", "a2"])
    assert agents == ["A", "a2", "b", "c"]


def test_shells_are_kept_apart_from_agents():
    agents, shells = agent_menu.split_agents_and_shells(["Claude", "Bash", "WSL Bash", "Amp"])
    assert shells == ["Bash", "WSL Bash"]
    assert agents == ["Amp", "Claude"]


def test_each_entry_launches_its_own_profile():
    entries = agent_menu.menu_entries(["Claude", "Grok Build"])
    assert entries == [
        {"caption": "Claude", "command": "ai_terminal_open_here", "args": {"profile": "Claude"}},
        {"caption": "Grok Build", "command": "ai_terminal_open_here", "args": {"profile": "Grok Build"}},
    ]


def test_checked_in_menu_matches_what_the_script_would_build():
    """Fails when an agent or profile is added or removed and the menu was not regenerated."""
    assert _committed_menu() == _regen_module().build_menu_tree(), (
        "Main.sublime-menu is out of date; run: python tools/regen_agent_menu.py"
    )


def test_every_catalog_agent_and_profile_is_on_exactly_one_submenu():
    regen = _regen_module()
    tree = _committed_menu()
    listed = [
        entry["caption"]
        for node_id in (agent_menu.AGENTS_NODE_ID, agent_menu.SHELLS_NODE_ID)
        for entry in _node(tree, node_id)["children"]
    ]
    assert len(listed) == len(set(listed)), "a name is listed twice"
    expected = agent_menu.known_profile_names(CATALOG, regen.settings_profile_names())
    assert set(listed) == expected


def test_submenus_are_sorted_a_to_z():
    tree = _committed_menu()
    for node_id in (agent_menu.AGENTS_NODE_ID, agent_menu.SHELLS_NODE_ID):
        captions = [entry["caption"] for entry in _node(tree, node_id)["children"]]
        assert captions == sorted(captions, key=str.lower)


def test_pickers_and_default_profile_are_gone_from_menu_palette_and_keymap():
    """Owner's decision (2026-09-21): pickers must go, and there is no default profile."""
    menu_text = json.dumps(_committed_menu())
    assert "ai_terminal_launcher" not in menu_text
    assert "Default Profile" not in menu_text
    for name in ("Default.sublime-commands", "Default.sublime-keymap"):
        with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
            assert "ai_terminal_launcher" not in handle.read(), name
