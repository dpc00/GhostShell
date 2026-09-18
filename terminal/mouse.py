"""Xterm mouse protocol encoding (host → PTY).

Apps enable tracking with DECSET:
  CSI ? 1000 h  — click only
  CSI ? 1002 h  — click + drag
  CSI ? 1003 h  — any-event (motion)
  CSI ? 1006 h  — SGR extended coordinates (preferred)

Reports (when 1006 / SGR is on):
  CSI < Cb ; Cx ; Cy M   press / wheel / motion
  CSI < Cb ; Cx ; Cy m   release

Cb: 0=left, 1=middle, 2=right, 64=wheel-up, 65=wheel-down;
    +32 motion, +4 shift, +8 meta, +16 ctrl.
Cx/Cy are 1-based cell columns/rows within the PTY grid.

Without 1006, legacy X10 (CSI M Cb+32 Cx+32 Cy+32) is used (coords ≤ 223).
"""
import ctypes


# Button codes (base, before modifier bits)
BTN_LEFT = 0
BTN_MIDDLE = 1
BTN_RIGHT = 2
BTN_RELEASE_X10 = 3  # X10-only "all buttons released"
BTN_WHEEL_UP = 64
BTN_WHEEL_DOWN = 65
BTN_WHEEL_LEFT = 66
BTN_WHEEL_RIGHT = 67

# ST button numbers (from drag_select event["button"]) → protocol base
_ST_BUTTON_TO_PROTO = {
    1: BTN_LEFT,
    2: BTN_MIDDLE,
    3: BTN_RIGHT,
}


def st_button_to_proto(st_button):
    """Map Sublime event button (1/2/3) to xterm button code, or None."""
    return _ST_BUTTON_TO_PROTO.get(int(st_button))


def _encode_mouse_legacy(
    button,
    col,
    row,
    *,
    press=True,
    motion=False,
    sgr=True,
    shift=False,
    meta=False,
    ctrl=False,
):
    """Hand-rolled xterm bytes. Used only if the native encoder is unavailable
    or cannot represent this (button, press, motion) triple."""
    cb = int(button)
    if motion:
        cb |= 32
    if shift:
        cb |= 4
    if meta:
        cb |= 8
    if ctrl:
        cb |= 16
    col = max(1, int(col))
    row = max(1, int(row))
    if sgr:
        final = "M" if press else "m"
        return f"\x1b[<{cb};{col};{row}{final}"
    col = min(col, 223)
    row = min(row, 223)
    if not press and button < 64:
        cb = BTN_RELEASE_X10
        if shift:
            cb |= 4
        if meta:
            cb |= 8
        if ctrl:
            cb |= 16
    cb = min(cb, 223)
    return "\x1b[M" + chr(cb + 32) + chr(col + 32) + chr(row + 32)


_NATIVE = None  # (gvt, Ghostty, encoder, event) or False

_PROTO_TO_GHOSTTY_BUTTON = {
    BTN_LEFT: 1,  # GHOSTTY_MOUSE_BUTTON_LEFT
    BTN_MIDDLE: 3,
    BTN_RIGHT: 2,
    BTN_WHEEL_UP: 4,
    BTN_WHEEL_DOWN: 5,
    BTN_WHEEL_LEFT: 6,
    BTN_WHEEL_RIGHT: 7,
}


def _native_handles():
    """Lazy process-wide encoder. False after a failed init (don't retry)."""
    global _NATIVE
    if _NATIVE is False:
        return None
    if _NATIVE is not None:
        return _NATIVE
    try:
        from . import ghostty_vt as gvt

        g = gvt.Ghostty(gvt.load_library())
        enc = gvt.GhosttyMouseEncoder()
        if g.mouse_encoder_new(None, ctypes.byref(enc)) != gvt.SUCCESS:
            _NATIVE = False
            return None
        evt = gvt.GhosttyMouseEvent()
        if g.mouse_event_new(None, ctypes.byref(evt)) != gvt.SUCCESS:
            g.mouse_encoder_free(enc)
            _NATIVE = False
            return None
        # encode_mouse() is "format these bytes", not "should we report".
        # Callers already gate on Screen.mouse_tracking. Force ANY so press,
        # release, motion, and wheel all encode. Format is set per call.
        g.mouse_encoder_setopt_int(enc, gvt.MOUSE_ENCODER_OPT_EVENT, gvt.MOUSE_TRACKING_ANY)
        size = gvt.mouse_encoder_size(4096, 4096, 1, 1)
        g.mouse_encoder_setopt(enc, gvt.MOUSE_ENCODER_OPT_SIZE, ctypes.byref(size))
        _NATIVE = (gvt, g, enc, evt)
        return _NATIVE
    except Exception:
        _NATIVE = False
        return None


def encode_mouse(
    button,
    col,
    row,
    *,
    press=True,
    motion=False,
    sgr=True,
    shift=False,
    meta=False,
    ctrl=False,
):
    """Encode one mouse report for the PTY.

    button: protocol base (0/1/2/64/65/…) — not ST's 1-based button id.
    col/row: 1-based cell coordinates within the PTY screen.
    press: True → final M (SGR); False → final m (SGR release).
    motion: set +32 (button-event / any-event drag).
    """
    seq = _encode_mouse_native(
        button, col, row, press=press, motion=motion, sgr=sgr,
        shift=shift, meta=meta, ctrl=ctrl,
    )
    if seq is not None:
        return seq
    return _encode_mouse_legacy(
        button, col, row, press=press, motion=motion, sgr=sgr,
        shift=shift, meta=meta, ctrl=ctrl,
    )


def _encode_mouse_native(
    button,
    col,
    row,
    *,
    press=True,
    motion=False,
    sgr=True,
    shift=False,
    meta=False,
    ctrl=False,
):
    handles = _native_handles()
    if handles is None:
        return None
    gvt, g, enc, evt = handles

    proto = int(button)
    no_button = motion and proto == BTN_RELEASE_X10
    gbtn = None if no_button else _PROTO_TO_GHOSTTY_BUTTON.get(proto)
    if gbtn is None and not no_button:
        return None

    if not press:
        action = gvt.MOUSE_ACTION_RELEASE
    elif motion:
        action = gvt.MOUSE_ACTION_MOTION
    else:
        action = gvt.MOUSE_ACTION_PRESS

    col = max(1, int(col))
    row = max(1, int(row))
    if not sgr:
        col = min(col, 223)
        row = min(row, 223)

    fmt = gvt.MOUSE_FORMAT_SGR if sgr else gvt.MOUSE_FORMAT_X10
    g.mouse_encoder_setopt_int(enc, gvt.MOUSE_ENCODER_OPT_FORMAT, fmt)
    g.mouse_encoder_setopt_bool(
        enc,
        gvt.MOUSE_ENCODER_OPT_ANY_BUTTON_PRESSED,
        press or (motion and not no_button),
    )

    g.mouse_event_set_action(evt, action)
    if no_button:
        g.mouse_event_clear_button(evt)
    else:
        g.mouse_event_set_button(evt, gbtn)
    mods = 0
    if shift:
        mods |= gvt.MODS_SHIFT
    if ctrl:
        mods |= gvt.MODS_CTRL
    if meta:
        mods |= gvt.MODS_ALT
    g.mouse_event_set_mods(evt, mods)
    # 1×1 px cells: 1-based (col,row) → top-left of that cell in surface space.
    g.mouse_event_set_position(
        evt, gvt.GhosttyMousePosition(float(col - 1), float(row - 1))
    )

    buf = ctypes.create_string_buffer(128)
    written = ctypes.c_size_t(0)
    rc = g.mouse_encoder_encode(enc, evt, buf, len(buf), ctypes.byref(written))
    if rc != gvt.SUCCESS or written.value == 0:
        return None
    return buf.raw[: written.value].decode("latin-1")



def encode_click(button, col, row, *, sgr=True, shift=False, meta=False, ctrl=False):
    """Press + release for a simple click (1000-mode apps)."""
    press = encode_mouse(
        button, col, row, press=True, sgr=sgr, shift=shift, meta=meta, ctrl=ctrl
    )
    release = encode_mouse(
        button, col, row, press=False, sgr=sgr, shift=shift, meta=meta, ctrl=ctrl
    )
    return press + release


def encode_wheel(direction_up, col, row, *, sgr=True, shift=False, meta=False, ctrl=False):
    """Wheel event (press only; no release in xterm). direction_up True → 64."""
    btn = BTN_WHEEL_UP if direction_up else BTN_WHEEL_DOWN
    return encode_mouse(
        btn, col, row, press=True, sgr=sgr, shift=shift, meta=meta, ctrl=ctrl
    )


def view_point_to_cell(row, col, *, hist_len, screen_rows, screen_cols):
    """Map a view (row, col) to 1-based PTY cell (cx, cy), or None if off-grid.

    hist_len: scrollback lines prepended in the view (0 on alt screen).
    row/col: 0-based Sublime rowcol of the click point.
    """
    screen_row = int(row) - int(hist_len)
    if screen_row < 0 or screen_row >= screen_rows:
        return None
    screen_col = max(0, min(int(col), screen_cols - 1))
    return screen_col + 1, screen_row + 1
