"""Most recently launched agent profiles (pure Python, unit-testable).

The Launch Agent picker lists the agents you launched most recently first, then the rest
alphabetically, so the few you actually use are at the top and you do not scroll the whole list.

The list is remembered in one tiny JSON file (at most DEFAULT_LIMIT profile names, well under
1 KB), kept by the caller in Sublime's cache folder. It is rebuildable convenience data: deleting
the file only puts the picker back to plain alphabetical order.
"""
import json
import os
import tempfile

# How many recent agents are remembered. Also caps the file size.
DEFAULT_LIMIT = 8


def load(path):
    """Return the remembered profile names, newest first. Missing or damaged file gives []."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [name for name in data if isinstance(name, str) and name]


def record(path, name, limit=DEFAULT_LIMIT):
    """Move `name` to the front of the remembered list and save it. Returns the new list.

    The file is written to a temporary file and moved into place, so a crash cannot leave a
    half-written list behind.
    """
    names = [name] + [existing for existing in load(path) if existing != name]
    names = names[:limit]
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(names, handle)
    os.replace(temporary_path, path)
    return names


def order(names, recent):
    """`names` reordered: recent ones first (newest first), then the rest alphabetically.

    A recent name that is no longer in `names` (for example an agent that was uninstalled) is
    skipped rather than shown.
    """
    wanted = set(names)
    front = [name for name in recent if name in wanted]
    front_set = set(front)
    rest = sorted((name for name in names if name not in front_set), key=lambda n: n.lower())
    return front + rest
