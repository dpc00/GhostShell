"""Keymap structural checks.

Used to also guard "every command class is imported into PluginLoader.py" --
that whole failure mode is gone now that ai_terminal.py sits at the repo root
and IS the top-level plugin module Sublime auto-scans directly (no loader, no
subdir, nothing to forget to register). What's left here has nothing to do
with that: it checks Default.sublime-keymap's own structure directly.
"""

import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─── keymap shadowing ────────────────────────────────────────────────────────
#
# Terminal views bind almost every key to ai_terminal_keypress so keystrokes
# reach the agent. A launcher chord that collided with one of those would work
# in a normal file and do nothing in a terminal, which is the most confusing
# possible outcome: the feature would look broken at random.

LAUNCHER_CHORDS = {
    "ctrl+alt+n": "ai_terminal_launcher",
    "ctrl+alt+h": "ai_terminal_history",
}


def _keymap():
    import json
    import re

    path = os.path.join(REPO, "Default.sublime-keymap")
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    # The keymap allows // comments, which json cannot parse.
    return json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.M))


def test_launcher_chords_are_bound_exactly_once():
    bindings = _keymap()
    for chord, command in LAUNCHER_CHORDS.items():
        hits = [b for b in bindings if b.get("keys") == [chord]]
        assert len(hits) == 1, "%s bound %d times: %r" % (chord, len(hits), hits)
        assert hits[0].get("command") == command


def test_launcher_chords_are_not_shadowed_by_terminal_keypass():
    """The chords must also work while a terminal view has focus."""
    passthrough = [
        b for b in _keymap() if b.get("command") == "ai_terminal_keypress"
    ]
    # Sanity check that we are actually looking at the passthrough block.
    assert len(passthrough) > 100, "expected the bulk keypress bindings"
    claimed = {k for b in passthrough for k in b.get("keys", [])}
    for chord in LAUNCHER_CHORDS:
        assert chord not in claimed, (
            "%s is also bound to ai_terminal_keypress, so it would be swallowed "
            "inside terminal views" % chord
        )
