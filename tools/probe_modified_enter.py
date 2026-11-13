"""What does GhostShell put on the wire for Ctrl+Enter, and under which modes?

The parser syncs from live terminal state on every encode_key call, so the mode
sequences fed here are the app's negotiation: a TUI that wants modified keys must
ask for them (Kitty keyboard protocol, or xterm modifyOtherKeys).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from terminal import ghostty_vt as gvt  # noqa: F401
from terminal.ghostty_engine import GhosttyParser
from terminal.screen import Screen

DLL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "terminal", "bin", "ghostty-vt.dll")


def fresh():
    return GhosttyParser(Screen(120, 40, history_cap=100), dll_path=DLL)


def enc(parser, key, **mods):
    got = parser.encode_key(key, **mods)
    if got is None:
        return "None (falls back to the legacy table)"
    return repr(bytes(got))


print("== no negotiation (what a TUI that never asks receives)")
p = fresh()
p.feed("hello")
print("   enter            ->", enc(p, "enter"))
print("   ctrl+enter       ->", enc(p, "enter", ctrl=True))
print("   shift+enter      ->", enc(p, "enter", shift=True))
print("   alt+enter        ->", enc(p, "enter", alt=True))

print("== app enables the Kitty keyboard protocol (CSI > 1 u)")
p = fresh()
p.feed("\x1b[>1u")
print("   enter            ->", enc(p, "enter"))
print("   ctrl+enter       ->", enc(p, "enter", ctrl=True))
print("   shift+enter      ->", enc(p, "enter", shift=True))
print("   alt+enter        ->", enc(p, "enter", alt=True))

print("== app enables xterm modifyOtherKeys level 2 (CSI > 4 ; 2 m)")
p = fresh()
p.feed("\x1b[>4;2m")
print("   ctrl+enter       ->", enc(p, "enter", ctrl=True))
print("   shift+enter      ->", enc(p, "enter", shift=True))

print("== keys that need no negotiation")
p = fresh()
print("   ctrl+q           ->", enc(p, "q", ctrl=True))
print("   alt+enter        ->", enc(p, "enter", alt=True))
