"""Detect installed coding-agent CLIs and report matching catalog entries.

Local-only, same philosophy as profile_availability.py: checks PATH for each
known command in agent_catalog.CATALOG, never probes a provider or spends
quota. For anything found, prints the remembered profile (launch_command,
spawn_env, quirk notes) as ready-to-paste JSON for ai_terminal.sublime-settings
"profiles", plus whether that profile name already appears in the live
settings file so you know what's actually new.

    python tools/scan_agents.py [path/to/ai_terminal.sublime-settings]
"""

import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "ai", "terminal"))

from agent_catalog import CATALOG, profile_from_entry  # noqa: E402
from profile_availability import command_exists  # noqa: E402

DEFAULT_SETTINGS = os.path.join(REPO, "ai_terminal.sublime-settings")


def _profile_json_snippet(command, entry):
    return json.dumps(
        {entry["display_name"]: profile_from_entry(entry)}, indent=4
    )


def scan(settings_path=DEFAULT_SETTINGS):
    settings_text = ""
    if os.path.isfile(settings_path):
        with open(settings_path, "r", encoding="utf-8") as f:
            settings_text = f.read()

    found = []
    for command, entry in sorted(CATALOG.items()):
        if not command_exists(entry["launch_command"]):
            continue
        already_configured = ('"%s"' % entry["display_name"]) in settings_text
        found.append((command, entry, already_configured))
    return found


def main():
    settings_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SETTINGS
    found = scan(settings_path)

    if not found:
        print("No catalog CLIs detected on PATH.")
        return

    for command, entry, already_configured in found:
        status = "already configured" if already_configured else "NEW -- not in settings"
        print("\n# %s (%s) -- %s" % (entry["display_name"], command, status))
        if entry.get("notes"):
            print("# %s" % entry["notes"])
        if not already_configured:
            print(_profile_json_snippet(command, entry))


if __name__ == "__main__":
    main()
