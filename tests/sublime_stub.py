"""Fake `sublime` / `sublime_plugin` modules, the one definition of them.

`ai_terminal` cannot be imported outside Sublime Text, so both the flow tests
and tools/check_import.py have to install stand-in modules into sys.modules
first. Keeping the two copies in step by hand went wrong exactly the way you
would expect (a constant or an API added for one, missing in the other, so a
symbol the plugin really uses looked fine in one runner and blew up in the
other), so both call `install` here.

The stub is deliberately dumb: it holds no state and drives no behaviour, it
only has to exist and be attribute-complete for import and class definition.
Anything a test wants to observe is passed in (`message_sink`) or monkeypatched
on the returned module.
"""

import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Region:
    def __init__(self, a, b=None):
        self.a, self.b = a, b if b is not None else a


class Settings(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)

    def set(self, k, v):
        self[k] = v

    def add_on_change(self, *a, **k):
        pass

    def clear_on_change(self, *a, **k):
        pass


class Kind(tuple):
    pass


class QuickPanelItem:
    def __init__(self, trigger, details="", annotation="", kind=None):
        self.trigger = trigger
        self.details = details
        self.annotation = annotation
        self.kind = kind

    def __repr__(self):
        return "QuickPanelItem(%r, %r, %r)" % (
            self.trigger,
            self.details,
            self.annotation,
        )


def install(message_sink=None):
    """Install the stubs (once) and return the stub `sublime` module.

    `message_sink`, when given, collects ``(kind, text)`` for every
    status/error/dialog message the plugin emits -- the only way a test can see
    the "nothing found" / "bad path" feedback paths.
    """
    if "sublime" in sys.modules:
        m = sys.modules["sublime"]
        # Test modules are collected in an order pytest does not guarantee.
        # If another suite installed the shared stub first, a later flow test
        # still needs to observe status/error messages in its own sink.
        if message_sink is not None:
            m._message_sink = message_sink
        return m

    def _message(kind):
        def emit(text):
            sink = getattr(m, "_message_sink", None)
            if sink is not None:
                sink.append((kind, text))

        return emit

    m = types.ModuleType("sublime")
    m._message_sink = message_sink
    m.Region = Region
    m.Settings = Settings
    m.Kind = Kind
    m.QuickPanelItem = QuickPanelItem
    m.load_settings = lambda name: Settings()
    m.save_settings = lambda name: None
    m.packages_path = lambda: os.path.join(REPO, ".fake-packages")
    m.cache_path = lambda: os.path.join(REPO, ".fake-cache")
    m.executable_path = lambda: "sublime_text.exe"
    m.set_timeout = lambda fn, ms=0: None
    m.set_timeout_async = lambda fn, ms=0: None
    m.cancel_timeout = lambda tok: None
    m.status_message = _message("status")
    m.error_message = _message("error")
    m.message_dialog = _message("dialog")
    m.windows = lambda: []
    m.active_window = lambda: None
    m.run_command = lambda *a, **k: None
    m.version = lambda: "4169"
    m.platform = lambda: "windows"
    m.arch = lambda: "x64"
    m.expand_variables = lambda s, v: s
    m.find_resources = lambda pattern: []
    m.load_resource = lambda path: ""
    m.get_clipboard = lambda: ""
    m.DRAW_NO_OUTLINE = 256
    m.DRAW_NO_FILL = 32
    m.DRAW_EMPTY = 1
    m.PERSISTENT = 16
    m.HIDDEN = 128
    m.LAYOUT_INLINE = 0
    m.KIND_ID_AMBIGUOUS = 0
    m.MONOSPACE_FONT = 1
    m.HOVER_TEXT = 1
    sys.modules["sublime"] = m

    sp = types.ModuleType("sublime_plugin")

    class _Base:
        # Sublime passes the window/view a command belongs to; which of the two
        # it is depends on the base class, so a stub sets both.
        def __init__(self, arg=None):
            self.window = arg
            self.view = arg

    for name in (
        "WindowCommand",
        "TextCommand",
        "ApplicationCommand",
        "EventListener",
        "ViewEventListener",
        "TextChangeListener",
    ):
        setattr(sp, name, type(name, (_Base,), {}))
    sys.modules["sublime_plugin"] = sp
    return m
