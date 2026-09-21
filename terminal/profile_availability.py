"""Local-only availability checks for ai_terminal launch profiles.

This module deliberately performs no network requests, provider probes, OAuth,
or model inference. A configured profile is launchable when its executable
exists locally.
"""

import os
import shutil


def command_exists(argv, path=None):
    """Return whether the first argv item can be launched on this machine."""
    if not isinstance(argv, (list, tuple)) or not argv or not isinstance(argv[0], str):
        return False
    executable = os.path.expandvars(os.path.expanduser(argv[0]))
    if os.path.isabs(executable) or os.path.dirname(executable):
        return os.path.isfile(executable)
    return shutil.which(executable, path=path) is not None


def profile_is_available(name, profile, path=None):
    """Return whether one configured profile has a launchable executable."""
    if not name or not isinstance(profile, dict):
        return False
    return command_exists(profile.get("launch_command"), path=path)


def menu_caption(name, executable_ok=True):
    """Compact menu caption for one profile: its name, plus "not installed" when the program is missing."""
    if not executable_ok:
        return "%s — not installed" % name
    return name

