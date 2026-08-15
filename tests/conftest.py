"""Put the sibling STLogs repo on sys.path so `import STLogs` works in tests."""
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECTS = os.path.dirname(_REPO)
if _PROJECTS not in sys.path:
    sys.path.insert(0, _PROJECTS)
