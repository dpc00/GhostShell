"""Local-only availability checks for ai_terminal launch profiles.

This module deliberately performs no network requests, provider probes, OAuth,
or model inference. A configured profile is launchable when its executable
exists locally.
"""

import os
import shutil
import time


# Sublime asks every menu entry for is_enabled, is_visible and description each time a menu opens, on the main
# thread. With ~50 profiles and a ~50-entry PATH that was ~0.5 s of PATH walking per open, and far more when a PATH
# directory is slow. An answer stays good for a few seconds: a CLI installed meanwhile shows up on the next open after.
_CACHE_SECONDS = 15.0
_cache = {}


def command_exists(argv, path=None):
    """Return whether the first argv item can be launched on this machine."""
    if not isinstance(argv, (list, tuple)) or not argv or not isinstance(argv[0], str):
        return False
    executable = os.path.expandvars(os.path.expanduser(argv[0]))
    key = (executable, path)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < _CACHE_SECONDS:
        return hit[1]
    if os.path.isabs(executable) or os.path.dirname(executable):
        found = os.path.isfile(executable)
    else:
        found = shutil.which(executable, path=path) is not None
    if len(_cache) > 512:
        _cache.clear()
    _cache[key] = (now, found)
    return found


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

