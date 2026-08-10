"""ctypes bindings for libghostty-vt (subset used by ghostty_engine.py).

Pure Python -- no Sublime imports. Safe to unit-test outside ST.

Only wraps the C API surface GhostyParser actually needs: terminal
lifecycle, VT feed, mode/data queries, the render-state row/cell
iterators for the active grid, and grid_ref for scrollback rows (which
sit outside the render-state's viewport-only scope). See
include/ghostty/vt/*.h in the ghostty checkout for the full API.

DLL ships in-repo at terminal/bin/ghostty-vt.dll (built from the Zig
checkout at ~/tools/ghostty/zig-out/bin/ghostty-vt.dll -- copy a fresh build
over that file to update). Override via GHOSTTY_VT_DLL env var to point at
a different build during development.
"""
import ctypes
import os

DEFAULT_DLL_PATH = os.path.join(os.path.dirname(__file__), "bin", "ghostty-vt.dll")


def load_library(path=None):
    path = path or os.environ.get("GHOSTTY_VT_DLL", DEFAULT_DLL_PATH)
    return ctypes.CDLL(path)


# ---- types.h ----

GhosttyResult = ctypes.c_int
SUCCESS = 0
OUT_OF_MEMORY = -1
INVALID_VALUE = -2
OUT_OF_SPACE = -3
NO_VALUE = -4

GhosttyTerminal = ctypes.c_void_p
GhosttyRenderState = ctypes.c_void_p
GhosttyRenderStateRowIterator = ctypes.c_void_p
GhosttyRenderStateRowCells = ctypes.c_void_p


class GhosttyBuffer(ctypes.Structure):
    _fields_ = [
        ("ptr", ctypes.POINTER(ctypes.c_uint8)),
        ("cap", ctypes.c_size_t),
        ("len", ctypes.c_size_t),
    ]


class GhosttyString(ctypes.Structure):
    """Borrowed byte string (pointer + length); not caller-provided like
    GhosttyBuffer. Valid only until the next terminal_vt_write()/reset()."""
    _fields_ = [
        ("ptr", ctypes.POINTER(ctypes.c_uint8)),
        ("len", ctypes.c_size_t),
    ]


# ---- color.h / style.h ----

class GhosttyColorRgb(ctypes.Structure):
    _fields_ = [("r", ctypes.c_uint8), ("g", ctypes.c_uint8), ("b", ctypes.c_uint8)]


STYLE_COLOR_NONE = 0
STYLE_COLOR_PALETTE = 1
STYLE_COLOR_RGB = 2


class _GhosttyStyleColorValue(ctypes.Union):
    _fields_ = [
        ("palette", ctypes.c_uint8),
        ("rgb", GhosttyColorRgb),
        ("_padding", ctypes.c_uint64),
    ]


class GhosttyStyleColor(ctypes.Structure):
    _fields_ = [("tag", ctypes.c_int), ("value", _GhosttyStyleColorValue)]


class GhosttyStyle(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("fg_color", GhosttyStyleColor),
        ("bg_color", GhosttyStyleColor),
        ("underline_color", GhosttyStyleColor),
        ("bold", ctypes.c_bool),
        ("italic", ctypes.c_bool),
        ("faint", ctypes.c_bool),
        ("blink", ctypes.c_bool),
        ("inverse", ctypes.c_bool),
        ("invisible", ctypes.c_bool),
        ("strikethrough", ctypes.c_bool),
        ("overline", ctypes.c_bool),
        ("underline", ctypes.c_int),
    ]

    def init(self):
        self.size = ctypes.sizeof(GhosttyStyle)
        return self


# ---- point.h ----

POINT_TAG_ACTIVE = 0
POINT_TAG_VIEWPORT = 1
POINT_TAG_SCREEN = 2
POINT_TAG_HISTORY = 3


class GhosttyPointCoordinate(ctypes.Structure):
    _fields_ = [("x", ctypes.c_uint16), ("y", ctypes.c_uint32)]


class _GhosttyPointValue(ctypes.Union):
    _fields_ = [
        ("coordinate", GhosttyPointCoordinate),
        ("_padding", ctypes.c_uint64 * 2),
    ]


class GhosttyPoint(ctypes.Structure):
    _fields_ = [("tag", ctypes.c_int), ("value", _GhosttyPointValue)]


def point(tag, x, y):
    p = GhosttyPoint()
    p.tag = tag
    p.value.coordinate.x = x
    p.value.coordinate.y = y
    return p


# ---- grid_ref.h ----

class GhosttyGridRef(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("node", ctypes.c_void_p),
        ("x", ctypes.c_uint16),
        ("y", ctypes.c_uint16),
    ]

    def init(self):
        self.size = ctypes.sizeof(GhosttyGridRef)
        return self


# ---- terminal.h ----

class GhosttyTerminalOptions(ctypes.Structure):
    _fields_ = [
        ("cols", ctypes.c_uint16),
        ("rows", ctypes.c_uint16),
        ("max_scrollback", ctypes.c_size_t),
    ]


TERMINAL_DATA_COLS = 1
TERMINAL_DATA_ROWS = 2
TERMINAL_DATA_CURSOR_X = 3
TERMINAL_DATA_CURSOR_Y = 4
TERMINAL_DATA_CURSOR_PENDING_WRAP = 5
TERMINAL_DATA_ACTIVE_SCREEN = 6
TERMINAL_DATA_CURSOR_VISIBLE = 7
TERMINAL_DATA_TOTAL_ROWS = 14
TERMINAL_DATA_SCROLLBACK_ROWS = 15
TERMINAL_DATA_MOUSE_TRACKING = 11
TERMINAL_DATA_TITLE = 12  # OSC 0/2 window title (GhosttyString)
TERMINAL_DATA_COLOR_PALETTE = 21
TERMINAL_DATA_VIEWPORT_ACTIVE = 32

TERMINAL_OPT_COLOR_PALETTE = 14

SCREEN_PRIMARY = 0
SCREEN_ALTERNATE = 1

# DEC private modes (ansi=false -> packed value == raw mode number,
# see ghostty_mode_new() in modes.h: value | (ansi << 15)).
MODE_NORMAL_MOUSE = 1000
MODE_BUTTON_MOUSE = 1002
MODE_ANY_MOUSE = 1003
MODE_SGR_MOUSE = 1006
MODE_BRACKETED_PASTE = 2004
MODE_ALT_SCREEN_SAVE = 1049


# ---- render.h ----

RENDER_STATE_DATA_COLS = 1
RENDER_STATE_DATA_ROWS = 2
RENDER_STATE_DATA_DIRTY = 3
RENDER_STATE_DATA_ROW_ITERATOR = 4
RENDER_STATE_DATA_CURSOR_VISUAL_STYLE = 10  # DECSCUSR shape (GhosttyRenderStateCursorVisualStyle)

# GhosttyRenderStateCursorVisualStyle -- NOT the same as TERMINAL_DATA_CURSOR_
# STYLE (=10 on the *terminal* enum, data id 10 there means the SGR text
# style applied to newly-typed characters, a different thing entirely).
RENDER_STATE_CURSOR_VISUAL_STYLE_BAR = 0
RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK = 1
RENDER_STATE_CURSOR_VISUAL_STYLE_UNDERLINE = 2
RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK_HOLLOW = 3

RENDER_STATE_DIRTY_FALSE = 0
RENDER_STATE_DIRTY_PARTIAL = 1
RENDER_STATE_DIRTY_FULL = 2

RENDER_STATE_OPTION_DIRTY = 0

RENDER_STATE_ROW_DATA_DIRTY = 1
RENDER_STATE_ROW_DATA_CELLS = 3

RENDER_STATE_ROW_OPTION_DIRTY = 0

RENDER_STATE_ROW_CELLS_DATA_STYLE = 2
RENDER_STATE_ROW_CELLS_DATA_BG_COLOR = 5
RENDER_STATE_ROW_CELLS_DATA_FG_COLOR = 6
RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8 = 9

# ---- key/event.h (GhosttyKeyAction, GhosttyMods, GhosttyKey) ----

KEY_ACTION_RELEASE = 0
KEY_ACTION_PRESS = 1
KEY_ACTION_REPEAT = 2

# GhosttyMods bitmask (uint16_t).
MODS_SHIFT = 1 << 0
MODS_CTRL  = 1 << 1
MODS_ALT   = 1 << 2
MODS_SUPER = 1 << 3
MODS_CAPS_LOCK = 1 << 4
MODS_NUM_LOCK  = 1 << 5

# GhosttyKey enum values — sequential starting from 1, matching event.h exactly.
# GHOSTTY_KEY_UNIDENTIFIED = 0 (default / not set).
# Writing System Keys (§ 3.1.1):
KEY_BACKQUOTE       = 1
KEY_BACKSLASH       = 2
KEY_BRACKET_LEFT    = 3
KEY_BRACKET_RIGHT   = 4
KEY_COMMA           = 5
KEY_DIGIT_0         = 6
KEY_DIGIT_1         = 7
KEY_DIGIT_2         = 8
KEY_DIGIT_3         = 9
KEY_DIGIT_4         = 10
KEY_DIGIT_5         = 11
KEY_DIGIT_6         = 12
KEY_DIGIT_7         = 13
KEY_DIGIT_8         = 14
KEY_DIGIT_9         = 15
KEY_EQUAL           = 16
KEY_INTL_BACKSLASH  = 17
KEY_INTL_RO         = 18
KEY_INTL_YEN        = 19
KEY_A = 20; KEY_B = 21; KEY_C = 22; KEY_D = 23; KEY_E = 24
KEY_F = 25; KEY_G = 26; KEY_H = 27; KEY_I = 28; KEY_J = 29
KEY_K = 30; KEY_L = 31; KEY_M = 32; KEY_N = 33; KEY_O = 34
KEY_P = 35; KEY_Q = 36; KEY_R = 37; KEY_S = 38; KEY_T = 39
KEY_U = 40; KEY_V = 41; KEY_W = 42; KEY_X = 43; KEY_Y = 44
KEY_Z = 45
KEY_MINUS           = 46
KEY_PERIOD          = 47
KEY_QUOTE           = 48
KEY_SEMICOLON       = 49
KEY_SLASH           = 50
# Functional Keys (§ 3.1.2):
KEY_ALT_LEFT        = 51
KEY_ALT_RIGHT       = 52
KEY_BACKSPACE       = 53
KEY_CAPS_LOCK       = 54
KEY_CONTEXT_MENU    = 55
KEY_CONTROL_LEFT    = 56
KEY_CONTROL_RIGHT   = 57
KEY_ENTER           = 58
KEY_META_LEFT       = 59
KEY_META_RIGHT      = 60
KEY_SHIFT_LEFT      = 61
KEY_SHIFT_RIGHT     = 62
KEY_SPACE           = 63
KEY_TAB             = 64
KEY_CONVERT         = 65
KEY_KANA_MODE       = 66
KEY_NON_CONVERT     = 67
# Control Pad (§ 3.2):
KEY_DELETE          = 68
KEY_END             = 69
KEY_HELP            = 70
KEY_HOME            = 71
KEY_INSERT          = 72
KEY_PAGE_DOWN       = 73
KEY_PAGE_UP         = 74
# Arrow Pad (§ 3.3):
KEY_ARROW_DOWN      = 75
KEY_ARROW_LEFT      = 76
KEY_ARROW_RIGHT     = 77
KEY_ARROW_UP        = 78
# Numpad (§ 3.4):
KEY_NUM_LOCK             = 79
KEY_NUMPAD_0             = 80
KEY_NUMPAD_1             = 81
KEY_NUMPAD_2             = 82
KEY_NUMPAD_3             = 83
KEY_NUMPAD_4             = 84
KEY_NUMPAD_5             = 85
KEY_NUMPAD_6             = 86
KEY_NUMPAD_7             = 87
KEY_NUMPAD_8             = 88
KEY_NUMPAD_9             = 89
KEY_NUMPAD_ADD           = 90
KEY_NUMPAD_BACKSPACE     = 91
KEY_NUMPAD_CLEAR         = 92
KEY_NUMPAD_CLEAR_ENTRY   = 93
KEY_NUMPAD_COMMA         = 94
KEY_NUMPAD_DECIMAL       = 95
KEY_NUMPAD_DIVIDE        = 96
KEY_NUMPAD_ENTER         = 97
KEY_NUMPAD_EQUAL         = 98
KEY_NUMPAD_MEMORY_ADD    = 99
KEY_NUMPAD_MEMORY_CLEAR  = 100
KEY_NUMPAD_MEMORY_RECALL = 101
KEY_NUMPAD_MEMORY_STORE  = 102
KEY_NUMPAD_MEMORY_SUBTRACT = 103
KEY_NUMPAD_MULTIPLY      = 104
KEY_NUMPAD_PAREN_LEFT    = 105
KEY_NUMPAD_PAREN_RIGHT   = 106
KEY_NUMPAD_SUBTRACT      = 107
KEY_NUMPAD_SEPARATOR     = 108
KEY_NUMPAD_UP            = 109
KEY_NUMPAD_DOWN          = 110
KEY_NUMPAD_RIGHT         = 111
KEY_NUMPAD_LEFT          = 112
KEY_NUMPAD_BEGIN         = 113
KEY_NUMPAD_HOME          = 114
KEY_NUMPAD_END           = 115
KEY_NUMPAD_INSERT        = 116
KEY_NUMPAD_DELETE        = 117
KEY_NUMPAD_PAGE_UP       = 118
KEY_NUMPAD_PAGE_DOWN     = 119
# Function Section (§ 3.5):
KEY_ESCAPE       = 120
KEY_F1  = 121; KEY_F2  = 122; KEY_F3  = 123; KEY_F4  = 124
KEY_F5  = 125; KEY_F6  = 126; KEY_F7  = 127; KEY_F8  = 128
KEY_F9  = 129; KEY_F10 = 130; KEY_F11 = 131; KEY_F12 = 132
KEY_F13 = 133; KEY_F14 = 134; KEY_F15 = 135; KEY_F16 = 136
KEY_F17 = 137; KEY_F18 = 138; KEY_F19 = 139; KEY_F20 = 140
KEY_F21 = 141; KEY_F22 = 142; KEY_F23 = 143; KEY_F24 = 144
KEY_F25         = 145
KEY_FN          = 146
KEY_FN_LOCK     = 147
KEY_PRINT_SCREEN = 148
KEY_SCROLL_LOCK  = 149
KEY_PAUSE        = 150

# ---- key/encoder.h ----

# GhosttyKeyEncoder / GhosttyKeyEvent are opaque pointer types (struct pointers
# in C, treated as c_void_p in ctypes).
GhosttyKeyEncoder = ctypes.c_void_p
GhosttyKeyEvent   = ctypes.c_void_p

# KEY_ENCODER_OPT_* (GhosttyKeyEncoderOption enum values from encoder.h)
KEY_ENCODER_OPT_CURSOR_KEY_APPLICATION   = 0
KEY_ENCODER_OPT_KEYPAD_KEY_APPLICATION   = 1
KEY_ENCODER_OPT_IGNORE_KEYPAD_WITH_NUMLOCK = 2
KEY_ENCODER_OPT_ALT_ESC_PREFIX          = 3
KEY_ENCODER_OPT_MODIFY_OTHER_KEYS_STATE_2 = 4
KEY_ENCODER_OPT_KITTY_FLAGS              = 5
KEY_ENCODER_OPT_MACOS_OPTION_AS_ALT      = 6
KEY_ENCODER_OPT_BACKARROW_KEY_MODE       = 7

# ---- ST key name → GhosttyKey mapping ----
# Sublime Text key names (from Default.sublime-keymap / on_key commands) mapped
# to GhosttyKey enum values. Single printable characters are handled separately
# via the utf8 field. Only named/special keys need an entry here.
ST_KEY_TO_GHOSTTY = {
    # Alpha
    "a": KEY_A, "b": KEY_B, "c": KEY_C, "d": KEY_D, "e": KEY_E,
    "f": KEY_F, "g": KEY_G, "h": KEY_H, "i": KEY_I, "j": KEY_J,
    "k": KEY_K, "l": KEY_L, "m": KEY_M, "n": KEY_N, "o": KEY_O,
    "p": KEY_P, "q": KEY_Q, "r": KEY_R, "s": KEY_S, "t": KEY_T,
    "u": KEY_U, "v": KEY_V, "w": KEY_W, "x": KEY_X, "y": KEY_Y,
    "z": KEY_Z,
    # Digits
    "0": KEY_DIGIT_0, "1": KEY_DIGIT_1, "2": KEY_DIGIT_2,
    "3": KEY_DIGIT_3, "4": KEY_DIGIT_4, "5": KEY_DIGIT_5,
    "6": KEY_DIGIT_6, "7": KEY_DIGIT_7, "8": KEY_DIGIT_8,
    "9": KEY_DIGIT_9,
    # Punctuation (ST names match the literal character)
    "`": KEY_BACKQUOTE, "\\": KEY_BACKSLASH,
    "[": KEY_BRACKET_LEFT, "]": KEY_BRACKET_RIGHT,
    ",": KEY_COMMA, "=": KEY_EQUAL, "-": KEY_MINUS,
    ".": KEY_PERIOD, "'": KEY_QUOTE, ";": KEY_SEMICOLON, "/": KEY_SLASH,
    # Special / named keys
    "enter":    KEY_ENTER,
    "backspace": KEY_BACKSPACE,
    "tab":      KEY_TAB,
    "space":    KEY_SPACE,
    "escape":   KEY_ESCAPE,
    "delete":   KEY_DELETE,
    "insert":   KEY_INSERT,
    "home":     KEY_HOME,
    "end":      KEY_END,
    "pageup":   KEY_PAGE_UP,
    "pagedown": KEY_PAGE_DOWN,
    "up":       KEY_ARROW_UP,
    "down":     KEY_ARROW_DOWN,
    "left":     KEY_ARROW_LEFT,
    "right":    KEY_ARROW_RIGHT,
    # Function keys
    "f1":  KEY_F1,  "f2":  KEY_F2,  "f3":  KEY_F3,  "f4":  KEY_F4,
    "f5":  KEY_F5,  "f6":  KEY_F6,  "f7":  KEY_F7,  "f8":  KEY_F8,
    "f9":  KEY_F9,  "f10": KEY_F10, "f11": KEY_F11, "f12": KEY_F12,
    "f13": KEY_F13, "f14": KEY_F14, "f15": KEY_F15, "f16": KEY_F16,
    "f17": KEY_F17, "f18": KEY_F18, "f19": KEY_F19, "f20": KEY_F20,
    # Numpad
    "keypad0": KEY_NUMPAD_0, "keypad1": KEY_NUMPAD_1,
    "keypad2": KEY_NUMPAD_2, "keypad3": KEY_NUMPAD_3,
    "keypad4": KEY_NUMPAD_4, "keypad5": KEY_NUMPAD_5,
    "keypad6": KEY_NUMPAD_6, "keypad7": KEY_NUMPAD_7,
    "keypad8": KEY_NUMPAD_8, "keypad9": KEY_NUMPAD_9,
    "keypad_period":   KEY_NUMPAD_DECIMAL,
    "keypad_divide":   KEY_NUMPAD_DIVIDE,
    "keypad_multiply": KEY_NUMPAD_MULTIPLY,
    "keypad_minus":    KEY_NUMPAD_SUBTRACT,
    "keypad_plus":     KEY_NUMPAD_ADD,
    "keypad_enter":    KEY_NUMPAD_ENTER,
}


def st_mods_to_ghostty(ctrl=False, alt=False, shift=False):
    """Convert ST ctrl/alt/shift booleans to a GhosttyMods uint16 bitmask."""
    m = 0
    if shift: m |= MODS_SHIFT
    if ctrl:  m |= MODS_CTRL
    if alt:   m |= MODS_ALT
    return m


class Ghostty:
    """Thin function-signature layer over a loaded ghostty-vt CDLL."""

    def __init__(self, lib):
        self.lib = lib
        self._bind()

    def _bind(self):
        lib = self.lib

        def sig(name, argtypes, restype):
            fn = getattr(lib, name)
            fn.argtypes = argtypes
            fn.restype = restype
            return fn

        p = ctypes.c_void_p
        u16 = ctypes.c_uint16
        u32 = ctypes.c_uint32
        sz = ctypes.c_size_t
        i = ctypes.c_int

        self.terminal_new = sig(
            "ghostty_terminal_new",
            [p, ctypes.POINTER(GhosttyTerminal), GhosttyTerminalOptions],
            GhosttyResult,
        )
        self.terminal_free = sig("ghostty_terminal_free", [GhosttyTerminal], None)
        self.terminal_reset = sig("ghostty_terminal_reset", [GhosttyTerminal], None)
        self.terminal_resize = sig(
            "ghostty_terminal_resize", [GhosttyTerminal, u16, u16, u32, u32], GhosttyResult
        )
        self.terminal_vt_write = sig(
            "ghostty_terminal_vt_write", [GhosttyTerminal, ctypes.c_char_p, sz], None
        )
        self.terminal_mode_get = sig(
            "ghostty_terminal_mode_get",
            [GhosttyTerminal, u16, ctypes.POINTER(ctypes.c_bool)],
            GhosttyResult,
        )
        self.terminal_get = sig(
            "ghostty_terminal_get", [GhosttyTerminal, i, p], GhosttyResult
        )
        self.terminal_set = sig(
            "ghostty_terminal_set", [GhosttyTerminal, i, p], GhosttyResult
        )
        self.terminal_grid_ref = sig(
            "ghostty_terminal_grid_ref",
            [GhosttyTerminal, GhosttyPoint, ctypes.POINTER(GhosttyGridRef)],
            GhosttyResult,
        )

        self.grid_ref_style = sig(
            "ghostty_grid_ref_style",
            [ctypes.POINTER(GhosttyGridRef), ctypes.POINTER(GhosttyStyle)],
            GhosttyResult,
        )
        self.grid_ref_graphemes = sig(
            "ghostty_grid_ref_graphemes",
            [ctypes.POINTER(GhosttyGridRef), ctypes.POINTER(ctypes.c_uint32), sz, ctypes.POINTER(sz)],
            GhosttyResult,
        )

        self.render_state_new = sig(
            "ghostty_render_state_new", [p, ctypes.POINTER(GhosttyRenderState)], GhosttyResult
        )
        self.render_state_free = sig("ghostty_render_state_free", [GhosttyRenderState], None)
        self.render_state_update = sig(
            "ghostty_render_state_update", [GhosttyRenderState, GhosttyTerminal], GhosttyResult
        )
        self.render_state_get = sig(
            "ghostty_render_state_get", [GhosttyRenderState, i, p], GhosttyResult
        )
        self.render_state_set = sig(
            "ghostty_render_state_set", [GhosttyRenderState, i, p], GhosttyResult
        )
        self.render_state_row_iterator_new = sig(
            "ghostty_render_state_row_iterator_new",
            [p, ctypes.POINTER(GhosttyRenderStateRowIterator)],
            GhosttyResult,
        )
        self.render_state_row_iterator_free = sig(
            "ghostty_render_state_row_iterator_free", [GhosttyRenderStateRowIterator], None
        )
        self.render_state_row_iterator_next = sig(
            "ghostty_render_state_row_iterator_next",
            [GhosttyRenderStateRowIterator],
            ctypes.c_bool,
        )
        self.render_state_row_get = sig(
            "ghostty_render_state_row_get", [GhosttyRenderStateRowIterator, i, p], GhosttyResult
        )
        self.render_state_row_set = sig(
            "ghostty_render_state_row_set", [GhosttyRenderStateRowIterator, i, p], GhosttyResult
        )
        self.render_state_row_cells_new = sig(
            "ghostty_render_state_row_cells_new",
            [p, ctypes.POINTER(GhosttyRenderStateRowCells)],
            GhosttyResult,
        )
        self.render_state_row_cells_free = sig(
            "ghostty_render_state_row_cells_free", [GhosttyRenderStateRowCells], None
        )
        self.render_state_row_cells_next = sig(
            "ghostty_render_state_row_cells_next", [GhosttyRenderStateRowCells], ctypes.c_bool
        )
        self.render_state_row_cells_get = sig(
            "ghostty_render_state_row_cells_get",
            [GhosttyRenderStateRowCells, i, p],
            GhosttyResult,
        )

        self._bind_key_encoder(sig, p, sz)

    def _bind_key_encoder(self, sig, p, sz):
        """Bind the key encoder + event API (key/encoder.h, key/event.h)."""
        i = ctypes.c_int

        # Opaque handles (pointer-sized).
        # GhosttyKeyEncoder and GhosttyKeyEvent are struct pointers; treat as
        # c_void_p for ctypes purposes — the DLL manages their layout.

        self.key_encoder_new = sig(
            "ghostty_key_encoder_new",
            [p, ctypes.POINTER(GhosttyKeyEncoder)],
            GhosttyResult,
        )
        self.key_encoder_free = sig(
            "ghostty_key_encoder_free", [GhosttyKeyEncoder], None
        )
        self.key_encoder_setopt = sig(
            "ghostty_key_encoder_setopt",
            [GhosttyKeyEncoder, i, p],
            None,
        )
        self.key_encoder_setopt_from_terminal = sig(
            "ghostty_key_encoder_setopt_from_terminal",
            [GhosttyKeyEncoder, GhosttyTerminal],
            None,
        )
        self.key_encoder_encode = sig(
            "ghostty_key_encoder_encode",
            [GhosttyKeyEncoder, GhosttyKeyEvent, ctypes.c_char_p, sz,
             ctypes.POINTER(sz)],
            GhosttyResult,
        )

        self.key_event_new = sig(
            "ghostty_key_event_new",
            [p, ctypes.POINTER(GhosttyKeyEvent)],
            GhosttyResult,
        )
        self.key_event_free = sig(
            "ghostty_key_event_free", [GhosttyKeyEvent], None
        )
        self.key_event_set_action = sig(
            "ghostty_key_event_set_action", [GhosttyKeyEvent, i], None
        )
        self.key_event_set_key = sig(
            "ghostty_key_event_set_key", [GhosttyKeyEvent, i], None
        )
        self.key_event_set_mods = sig(
            "ghostty_key_event_set_mods",
            [GhosttyKeyEvent, ctypes.c_uint16],
            None,
        )
        self.key_event_set_utf8 = sig(
            "ghostty_key_event_set_utf8",
            [GhosttyKeyEvent, ctypes.c_char_p, sz],
            None,
        )
        self.key_event_set_unshifted_codepoint = sig(
            "ghostty_key_event_set_unshifted_codepoint",
            [GhosttyKeyEvent, ctypes.c_uint32],
            None,
        )
