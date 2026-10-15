"""recover_console.py -- attach a Windows console to a running agent_broker
session as a raw VT client (same I/O model as GhostShell's Sublime tab).

Brokers are discovered from the GhostShell registry (each Sublime Text keeps its own in
<its data folder>\\Local\\GhostShell\\broker_sessions; --registry-dir names it, and the old
machine-wide %LOCALAPPDATA%\\GhostShell\\broker_sessions is the default), not from process command
lines: production brokers start with --launch-file and never expose
--pipe-name on the command line.

This is a VT byte relay, not `grok.exe` hosted by Windows Terminal. Grok
stays bound to the broker ConPTY. Closing this console detaches the relay
and does NOT kill the broker; run it again with the same pipe to reattach.

Usage:
    python recover_console.py                  # auto-detect, prompt if >1 match
    python recover_console.py --pipe-name NAME # attach to a known pipe directly
    python recover_console.py --list           # just list candidate sessions
"""
import argparse
import codecs
import re
import ctypes
import json
import os
import sys
import threading
import time
import traceback
from ctypes import POINTER, Structure, byref, c_char, c_void_p
from ctypes.wintypes import BOOL, DWORD, FILETIME, HANDLE, LPCWSTR, SHORT, WORD

if sys.platform != "win32":
    sys.exit("attach_console.py is Windows-only (named pipes).")

# --- Win32 constants (values from the Windows SDK headers named per group) ---
# CreateFileW access rights (winnt.h): read the broker's output pipe, write
# its input and control pipes.
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
# CreateFileW creation disposition (fileapi.h): open a pipe that must already
# exist (the broker created it); never create one.
_OPEN_EXISTING = 3
# What CreateFileW returns on failure: the HANDLE value -1.
_INVALID_HANDLE_VALUE = HANDLE(-1).value
# GetLastError codes (winerror.h) this file reacts to:
_ERROR_PIPE_BUSY = 231       # every pipe instance has a client; wait and retry
_ERROR_BROKEN_PIPE = 109     # the broker closed its end; stop relaying
_ERROR_HANDLE_EOF = 38       # end of data on the pipe; stop relaying
_ERROR_NO_DATA = 232         # the pipe is being closed; stop relaying
_ERROR_FILE_NOT_FOUND = 2    # the pipe does not exist (yet); retry until timeout
# GetExitCodeProcess result while the process is still running (minwinbase.h).
_STILL_ACTIVE = 259
# OpenProcess access right (winnt.h): the least access that still allows
# GetExitCodeProcess, QueryFullProcessImageNameW and GetProcessTimes.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# GetStdHandle selectors (processenv.h): this console's keyboard and screen.
_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
# Console INPUT mode flags (consoleapi.h), see _enable_raw_vt for which are
# cleared and set:
_ENABLE_PROCESSED_INPUT = 0x0001          # console handles Ctrl+C itself
_ENABLE_LINE_INPUT = 0x0002               # ReadFile waits for Enter
_ENABLE_ECHO_INPUT = 0x0004               # console echoes typed keys
_ENABLE_MOUSE_INPUT = 0x0010              # report mouse events as input
_ENABLE_EXTENDED_FLAGS = 0x0080           # required to change QUICK_EDIT
_ENABLE_QUICK_EDIT_MODE = 0x0040          # mouse selects text in the console
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200   # keys arrive as VT escape bytes
# Console OUTPUT mode flags (consoleapi.h), all set by _enable_raw_vt:
_ENABLE_PROCESSED_OUTPUT = 0x0001             # act on control characters
_ENABLE_WRAP_AT_EOL_OUTPUT = 0x0002           # wrap at the right edge
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004  # interpret VT escape sequences
_ENABLE_DISABLE_NEWLINE_AUTO_RETURN = 0x0008  # LF moves down only, like a VT
# Code page for SetConsoleCP/SetConsoleOutputCP: UTF-8, so bytes relayed from
# the broker are shown as the child wrote them.
_CP_UTF8 = 65001


# Win32 COORD (wincontypes.h): a column/row pair in the console buffer.
class _COORD(Structure):
    _fields_ = [("X", SHORT), ("Y", SHORT)]


# Win32 SMALL_RECT (wincontypes.h): the visible window inside the console
# buffer, inclusive on all four sides.
class _SMALL_RECT(Structure):
    _fields_ = [("Left", SHORT), ("Top", SHORT), ("Right", SHORT), ("Bottom", SHORT)]


# Win32 CONSOLE_SCREEN_BUFFER_INFO (wincon.h), filled by
# GetConsoleScreenBufferInfo. Only srWindow is used: _console_size turns it
# into the visible columns x rows sent to the broker as RESIZE.
class _CONSOLE_SCREEN_BUFFER_INFO(Structure):
    _fields_ = [
        ("dwSize", _COORD),
        ("dwCursorPosition", _COORD),
        ("wAttributes", WORD),
        ("srWindow", _SMALL_RECT),
        ("dwMaximumWindowSize", _COORD),
    ]


# kernel32.dll, the Win32 base API. use_last_error=True keeps each call's
# GetLastError value for ctypes.get_last_error(), which connect() and the
# pumps read to tell a busy or closed pipe from a real failure.
# argtypes/restype are declared for every function so ctypes passes 64-bit
# HANDLEs and pointers correctly instead of guessing int.
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
# Named-pipe CLIENT group (fileapi.h, namedpipeapi.h): open the broker's
# pipes by name (\\.\pipe\NAME, NAME-in, NAME-ctl), read and write bytes on
# them, close them, and wait for a busy pipe to free an instance.
_k32.CreateFileW.argtypes = [LPCWSTR, DWORD, DWORD, c_void_p, DWORD, DWORD, HANDLE]
_k32.CreateFileW.restype = HANDLE
_k32.ReadFile.argtypes = [HANDLE, POINTER(c_char), DWORD, POINTER(DWORD), c_void_p]
_k32.ReadFile.restype = BOOL
_k32.WriteFile.argtypes = [HANDLE, ctypes.c_char_p, DWORD, POINTER(DWORD), c_void_p]
_k32.WriteFile.restype = BOOL
_k32.CloseHandle.argtypes = [HANDLE]
_k32.CloseHandle.restype = BOOL
_k32.WaitNamedPipeW.argtypes = [LPCWSTR, DWORD]
_k32.WaitNamedPipeW.restype = BOOL
# Console group (processenv.h, consoleapi.h, wincon.h): get this window's
# keyboard and screen handles, switch them to raw VT mode and back, read the
# visible size for RESIZE, set UTF-8, and stop Ctrl+C from killing the relay
# so the byte 0x03 reaches the child instead.
_k32.GetStdHandle.argtypes = [ctypes.c_int]
_k32.GetStdHandle.restype = HANDLE
_k32.GetConsoleMode.argtypes = [HANDLE, POINTER(DWORD)]
_k32.GetConsoleMode.restype = BOOL
_k32.SetConsoleMode.argtypes = [HANDLE, DWORD]
_k32.SetConsoleMode.restype = BOOL
_k32.GetConsoleScreenBufferInfo.argtypes = [
    HANDLE, POINTER(_CONSOLE_SCREEN_BUFFER_INFO)
]
_k32.GetConsoleScreenBufferInfo.restype = BOOL
_k32.SetConsoleCP.argtypes = [DWORD]
_k32.SetConsoleCP.restype = BOOL
_k32.SetConsoleOutputCP.argtypes = [DWORD]
_k32.SetConsoleOutputCP.restype = BOOL
_k32.SetConsoleCtrlHandler.argtypes = [c_void_p, BOOL]
_k32.SetConsoleCtrlHandler.restype = BOOL
# Process-check group (processthreadsapi.h, winbase.h): used only by
# _pid_is_alive and _broker_process_matches to confirm a registry record's
# broker_pid is still the same live python broker, not a recycled PID.
_k32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
_k32.OpenProcess.restype = HANDLE
_k32.GetExitCodeProcess.argtypes = [HANDLE, POINTER(DWORD)]
_k32.GetExitCodeProcess.restype = BOOL
_k32.QueryFullProcessImageNameW.argtypes = [HANDLE, DWORD, LPCWSTR, POINTER(DWORD)]
_k32.QueryFullProcessImageNameW.restype = BOOL
_k32.GetProcessTimes.argtypes = [
    HANDLE, POINTER(FILETIME), POINTER(FILETIME), POINTER(FILETIME), POINTER(FILETIME),
]
_k32.GetProcessTimes.restype = BOOL


def _pid_is_alive(pid):
    """True if a process with this PID exists and has not exited."""
    try:
        # Least-privilege handle: enough for GetExitCodeProcess below.
        handle = _k32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
    except (TypeError, ValueError):
        print("[attach] pid liveness check: OpenProcess failed:\n%s" % traceback.format_exc(),
              file=sys.stderr)
        return False
    if not handle:
        return False  # no such process, or not ours to open
    try:
        # Exit code STILL_ACTIVE (259) means the process is still running.
        code = DWORD(0)
        return bool(_k32.GetExitCodeProcess(handle, byref(code))) and (
            code.value == _STILL_ACTIVE
        )
    finally:
        _k32.CloseHandle(handle)


_EPOCH_AS_FILETIME = 116444736000000000  # 1601-01-01 -> 1970-01-01, in 100ns units


def _filetime_to_unix(ft):
    """Win32 FILETIME (100 ns ticks since 1601, split in two 32-bit halves)
    to Unix seconds, to compare with the registry's created_at."""
    value = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return (value - _EPOCH_AS_FILETIME) / 10000000.0


def _broker_process_matches(pid, created_at):
    """True if `pid` is alive AND plausibly the broker recorded at
    created_at, not an unrelated process that later recycled the same PID.

    A crashed broker that never reached its own registry cleanup leaves a
    stale record behind indefinitely; a bare PID-alive check would call any
    later process on that PID "live". Brokers always run as
    python.exe/pythonw.exe, and a genuine broker's process start time sits
    within seconds of when it published its registry record -- cross-check
    both.
    """
    if not _pid_is_alive(pid):
        return False
    try:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    except (TypeError, ValueError):
        print("[attach] broker process match: OpenProcess failed:\n%s" % traceback.format_exc(),
              file=sys.stderr)
        return False
    if not handle:
        return False
    try:
        # Full path of the process's .exe. 260 = MAX_PATH; size is in and
        # out (buffer length in, characters written out). Flag 0 = Win32
        # path format.
        size = DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if not _k32.QueryFullProcessImageNameW(handle, 0, buf, byref(size)):
            return False
        exe_name = os.path.basename(buf.value).lower()
        if exe_name not in ("python.exe", "pythonw.exe"):
            return False
        if created_at is None:
            return True
        # GetProcessTimes fills all four; only the creation time is used.
        creation = FILETIME()
        exit_t = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not _k32.GetProcessTimes(
            handle, byref(creation), byref(exit_t), byref(kernel), byref(user)
        ):
            return True  # can't read the start time; the exe check passed
        # Same broker if it started within 5 minutes of its registry record.
        started = _filetime_to_unix(creation)
        return abs(started - float(created_at)) < 300
    finally:
        _k32.CloseHandle(handle)


_registry_dir_override = None


def _registry_dir():
    """The folder with the session records: the one given with --registry-dir (Sublime Text keeps each
    instance's sessions in its own data folder), else the old machine-wide one."""
    if _registry_dir_override:
        return _registry_dir_override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "GhostShell", "broker_sessions")


def find_sessions():
    """Live brokers from the GhostShell registry: (pipe_name, cwd, child_cmd)."""
    folder = _registry_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        print("[attach] find_sessions: listdir %s failed:\n%s" % (folder, traceback.format_exc()),
              file=sys.stderr)
        return []
    sessions = []
    for name in names:
        if not name.startswith("ghostshell_") or not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            print("[attach] find_sessions: record %s unreadable:\n%s"
                  % (name, traceback.format_exc()), file=sys.stderr)
            continue
        pipe_name = record.get("pipe_name")
        if not pipe_name or pipe_name != name[:-5]:
            continue
        if not _broker_process_matches(record.get("broker_pid"), record.get("created_at")):
            continue
        child = record.get("child_argv") or []
        if isinstance(child, list):
            child = " ".join(str(part) for part in child)
        sessions.append((
            pipe_name,
            record.get("cwd") or "?",
            child or record.get("profile_name") or "?",
        ))
    sessions.sort(key=lambda item: item[0])
    return sessions


def connect(path, access, timeout_s=10.0):
    """Open one broker pipe as a client, retrying until timeout_s.

    Retries while the pipe is busy (another client holds every instance) or
    not created yet; any other error is raised at once.
    """
    deadline = time.time() + timeout_s
    while True:
        # Arguments: pipe path, read or write access, no sharing, default
        # security, open an existing pipe only, no flags, no template file.
        h = _k32.CreateFileW(path, access, 0, None, _OPEN_EXISTING, 0, None)
        if h != _INVALID_HANDLE_VALUE:
            return h
        err = ctypes.get_last_error()
        if time.time() > deadline:
            raise ctypes.WinError(err)
        if err == _ERROR_PIPE_BUSY:
            # Block up to 2 s for an instance to free, then try again.
            _k32.WaitNamedPipeW(path, 2000)
        elif err == _ERROR_FILE_NOT_FOUND:
            time.sleep(0.2)
        else:
            raise ctypes.WinError(err)


def connect_output(pipe_name):
    """Connect to output, recovering a quiet stale output attachment.

    Old brokers only discover a disconnected output reader on their next
    write.  If output is busy but input is free, no complete client is
    attached: briefly send Ctrl+L on input to request a harmless redraw.  The
    resulting output makes the old broker discard its dead output handle.
    An active session has a busy input pipe, so this cannot steal it.
    """
    out_path = "\\\\.\\pipe\\" + pipe_name
    output_error = None
    try:
        return connect(out_path, _GENERIC_READ)
    except OSError as error:
        if getattr(error, "winerror", None) != _ERROR_PIPE_BUSY:
            raise
        output_error = error

    in_path = "\\\\.\\pipe\\" + pipe_name + "-in"
    try:
        h_in = connect(in_path, _GENERIC_WRITE, timeout_s=0.5)
    except OSError:
        raise output_error

    try:
        # One Ctrl+L byte on the input pipe; WriteFile reports the bytes
        # written through `written`, and None = no overlapped I/O.
        written = DWORD(0)
        redraw = b"\x0c"
        if not _k32.WriteFile(h_in, redraw, len(redraw), byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _k32.CloseHandle(h_in)

    print("[attach] released a stale quiet output attachment; retrying", file=sys.stderr)
    return connect(out_path, _GENERIC_READ, timeout_s=5.0)


def _console_size(h_out):
    """Visible (columns, rows) of this console window, or None if unknown.

    srWindow is the visible part of the (possibly taller) console buffer;
    its edges are inclusive, hence the +1.
    """
    info = _CONSOLE_SCREEN_BUFFER_INFO()
    if not _k32.GetConsoleScreenBufferInfo(h_out, byref(info)):
        return None
    cols = int(info.srWindow.Right - info.srWindow.Left + 1)
    rows = int(info.srWindow.Bottom - info.srWindow.Top + 1)
    if cols < 1 or rows < 1:
        return None
    return cols, rows


def _send_resize(h_ctl, cols, rows):
    """Tell the broker the new size over its control pipe ("RESIZE c r\\n",
    the same line ai_terminal.py sends). Result ignored: a lost resize
    only leaves the old size until the next change."""
    if h_ctl is None:
        return
    line = ("RESIZE %d %d\n" % (cols, rows)).encode("utf-8")
    written = DWORD(0)
    _k32.WriteFile(h_ctl, line, len(line), byref(written), None)


def _enable_raw_vt(h_in, h_out):
    """Put this console in raw VT mode; return the old (input, output) modes
    so main() can restore them on exit."""
    in_mode = DWORD(0)
    out_mode = DWORD(0)
    if not _k32.GetConsoleMode(h_in, byref(in_mode)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not _k32.GetConsoleMode(h_out, byref(out_mode)):
        raise ctypes.WinError(ctypes.get_last_error())
    # golang.org/x/term makeRaw on Windows, plus Microsoft's VT pairing:
    # disable line/echo/processed so ReadFile returns keys immediately and
    # Ctrl+C is a byte, not a console abort. ENABLE_ECHO_INPUT is what drew
    # the dark-gray local echo on top of Grok's TUI. ENABLE_MOUSE_INPUT must
    # also be cleared here -- disabling ENABLE_QUICK_EDIT_MODE (required
    # above) without also disabling this turns on real mouse-move event
    # reporting; with ENABLE_VIRTUAL_TERMINAL_INPUT set, conhost/Windows
    # Terminal encodes every mouse move as a VT escape sequence and this
    # relay forwards it straight to the child's stdin as if typed. Confirmed
    # live 2026-09-05: Continue (cn), which never requested DECSET mouse
    # tracking and doesn't parse it, received raw mouse-move escapes as
    # literal keystrokes in its own command line.
    new_in = in_mode.value
    new_in &= ~(
        _ENABLE_LINE_INPUT
        | _ENABLE_ECHO_INPUT
        | _ENABLE_PROCESSED_INPUT
        | _ENABLE_QUICK_EDIT_MODE
        | _ENABLE_MOUSE_INPUT
    )
    new_in |= _ENABLE_EXTENDED_FLAGS | _ENABLE_VIRTUAL_TERMINAL_INPUT
    new_out = (
        out_mode.value
        | _ENABLE_PROCESSED_OUTPUT
        | _ENABLE_WRAP_AT_EOL_OUTPUT
        | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
        | _ENABLE_DISABLE_NEWLINE_AUTO_RETURN
    )
    if not _k32.SetConsoleMode(h_in, new_in):
        raise ctypes.WinError(ctypes.get_last_error())
    if not _k32.SetConsoleMode(h_out, new_out):
        raise ctypes.WinError(ctypes.get_last_error())
    _k32.SetConsoleCP(_CP_UTF8)
    _k32.SetConsoleOutputCP(_CP_UTF8)
    # Ignore the default Ctrl+C handler so 0x03 is readable and forwarded.
    _k32.SetConsoleCtrlHandler(None, True)
    return in_mode.value, out_mode.value


_REPLAY_END_TEXT = "\x1b]777;GhostShellReplayEnd\x07"   # agent_broker._REPLAY_END: history ends, live output starts
_LIVE_PANELS = 3                # newest panels kept redrawable; older ones are written once
_ANSI_SEQ = re.compile(r"\x1b\[[0-9;]*m")


class _TspRelay:
    """Show a program that speaks the Tern Surface Protocol (omp) in this plain terminal.

    omp drew its screen through TSP when the Sublime tab started it, so the bytes it sends are protocol
    messages that a terminal ignores: this window stayed blank.  Here the same TspHost the Sublime tab
    uses answers the messages and the surface is repainted as text (terminal/tsp_text.py).  The replay
    of old output is history: its messages are dropped unanswered, then the program is asked to send
    its whole document again (TspHost.request_resync), as when a Sublime tab reattaches.
    """

    def __init__(self, host, size):
        self._host = host
        self._size = size           # function returning (cols, rows)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._replaying = True
        self._held = ""             # replay text kept back while the end marker may be split
        self._surface_id = None     # the surface being followed
        self._committed = 0         # main panels already written to the window's scrollback
        self._live_rows = 0         # screen rows the live part occupies now

    def process(self, data):
        """Bytes from the broker -> bytes for this console."""
        text = self._decoder.decode(data)
        out = ""
        if self._replaying:
            self._held += text
            at = self._held.find(_REPLAY_END_TEXT)
            if at == -1:
                keep = len(_REPLAY_END_TEXT) - 1
                cut = max(0, len(self._held) - keep)
                out = self._host.strip(self._held[:cut])
                self._held = self._held[cut:]
                return out.encode("utf-8")
            out = self._host.strip(self._held[:at])
            text = self._held[at + len(_REPLAY_END_TEXT):]
            self._held = ""
            self._replaying = False
            self._host.request_resync()
        out += self._host.filter(text)
        if self._host.active_surface() is not None:
            try:
                out += self._paint()
            except Exception as error:     # one undrawable panel must not turn the window into raw protocol bytes
                self._live_rows = 0
                out += "\r\n[GhostShell: the surface could not be drawn as text: %r]\r\n" % (error,)
        return out.encode("utf-8")

    def _paint(self):
        """What to write so this window follows the program's surface like a terminal log.

        Clearing and redrawing the whole screen on every frame (the old way) pushed each half-finished picture
        into the window's scrollback, which came out garbled.  Now a panel is written once, when it is no longer
        among the newest few, and only the live part (the newest panels, an open overlay, the input box) is
        erased and drawn again at the bottom.
        """
        host = self._host
        if not host._changed:
            return ""
        del host._changed[:]
        surface_id = host.active_surface()
        document = host.documents.get(surface_id)
        if document is None:
            return ""
        cols, rows = self._size()
        regions = {r.get("id"): r.get("c") or [] for r in document.snapshot().get("c") or []}
        main = regions.get("main", [])
        if surface_id != self._surface_id:
            self._surface_id = surface_id        # a new surface (after a resync): its whole history is new here
            self._committed = 0
        if self._committed > len(main):
            self._committed = 0
        overlays = regions.get("layer", [])
        stable = max(0, len(main) - _LIVE_PANELS)
        out = ""
        if self._live_rows:
            out += "\r\x1b[%dA\x1b[J" % (self._live_rows - 1) if self._live_rows > 1 else "\r\x1b[J"
            self._live_rows = 0
        if not overlays:
            for node in main[self._committed:stable]:
                lines = self._lines(node, cols)
                if lines:
                    out += "\r\n".join(lines) + "\r\n"
            self._committed = max(self._committed, stable)
        live = []
        if overlays:
            live += self._lines(overlays[-1], cols)
        else:
            for node in main[self._committed:]:
                live += self._lines(node, cols)
        for node in regions.get("dock", []):
            live += self._lines(node, cols)
        fitted, used = [], 0
        for line in reversed(live):              # keep the bottom of it when it is taller than the window
            height = max(1, -(-len(_ANSI_SEQ.sub("", line)) // max(1, cols)))
            if used + height > max(1, rows - 1):
                break
            fitted.append(line)
            used += height
        fitted.reverse()
        out += "\r\n".join(fitted)
        self._live_rows = max(1, used)
        return out

    @staticmethod
    def _lines(node, cols):
        return [line for line in tsp_text._draw(node, cols)]


def _make_tsp_relay(h_in, write_lock, h_con_out):
    """A _TspRelay for this session, or None when the TSP modules cannot be loaded (then bytes pass through)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from terminal.tsp_host import TspHost
        global tsp_text
        from terminal import tsp_text
    except Exception:
        print("[attach] TSP support unavailable, relaying raw bytes:\n%s" % traceback.format_exc(), file=sys.stderr)
        return None

    def size():
        return _console_size(h_con_out) or (100, 30)

    def send(text):
        raw = text.encode("utf-8")
        written = DWORD(0)
        with write_lock:
            _k32.WriteFile(h_in, raw, len(raw), byref(written), None)

    host = TspHost(send, windows_conpty=True, get_cols=lambda: size()[0])
    return _TspRelay(host, size)


def _pump_output(handle, h_con_out, stop_evt, relay=None):
    """Copy the broker's output pipe to this console until either side ends.

    WriteFile to the console handle is the direct path; if it fails (output
    redirected, no console) the bytes go to sys.stdout instead.  With a
    ``relay`` the bytes first pass through it (TSP messages answered, the
    program's surface drawn as text).
    """
    buf = (c_char * 4096)()  # 4 KB read buffer
    n = DWORD(0)
    while not stop_evt.is_set():
        ok = _k32.ReadFile(handle, buf, 4096, byref(n), None)
        if not ok:
            err = ctypes.get_last_error()
            if err in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_HANDLE_EOF):
                break
            print("\n[attach] ReadFile failed (GetLastError %d)" % err, file=sys.stderr)
            break
        if n.value == 0:
            break
        written = DWORD(0)
        data = bytes(buf[: n.value])
        if relay is not None:
            try:
                data = relay.process(data)
            except Exception:
                # Never fall back to the raw bytes: omp's protocol messages are what turned the window into noise.
                # Show the fault where it can be read, then go on with the next chunk.
                text = traceback.format_exc().replace("\n", "\r\n")
                data = ("\r\n[attach] TSP relay error (this chunk was dropped):\r\n%s\r\n" % text).encode("utf-8", "replace")
            if not data:
                continue
        if not _k32.WriteFile(h_con_out, data, len(data), byref(written), None):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    stop_evt.set()


def _pump_input(h_con_in, h_in, stop_evt, write_lock):
    """Copy raw keyboard bytes from this console to the broker's input pipe.

    In raw VT mode ReadFile returns keys as soon as they are typed, already
    encoded as VT bytes, so they are forwarded unchanged.
    """
    buf = (c_char * 256)()  # keystrokes arrive in small bursts
    n = DWORD(0)
    while not stop_evt.is_set():
        ok = _k32.ReadFile(h_con_in, buf, 256, byref(n), None)
        if not ok or n.value == 0:
            break
        written = DWORD(0)
        with write_lock:
            sent = _k32.WriteFile(h_in, buf, n.value, byref(written), None)
        if not sent:
            break
    stop_evt.set()


def _watch_resize(h_con_out, h_ctl, stop_evt, initial=None):
    """Send the console size once, then again whenever it changes.

    A 250 ms poll. Windows reports console size changes only as
    WINDOW_BUFFER_SIZE_EVENT records through ReadConsoleInput; the raw
    ReadFile loop in _pump_input never sees them, so there is no event here
    to wait on instead (rule 10).

    initial (cols, rows): the size of the Sublime tab this session was moved from. It is sent instead
    of this window's own size, so the program keeps drawing at the tab's width (a different width would
    leave output in the session's replay that the tab cannot lay out when it comes back); the window's
    size is only followed once it changes later.
    """
    last = _console_size(h_con_out)
    if initial is not None:
        _send_resize(h_ctl, initial[0], initial[1])
    elif last is not None:
        _send_resize(h_ctl, last[0], last[1])
    while not stop_evt.wait(0.25):
        size = _console_size(h_con_out)
        if size is not None and size != last:
            last = size
            _send_resize(h_ctl, size[0], size[1])


def pick_pipe_name(explicit):
    if explicit:
        return explicit
    sessions = find_sessions()
    if not sessions:
        sys.exit("attach_console: no running agent_broker.py sessions found")
    if len(sessions) == 1:
        pipe_name, cwd, child = sessions[0]
        print(f"[attach] found one session: pipe={pipe_name!r} cwd={cwd} child={child}",
              file=sys.stderr)
        return pipe_name
    print("[attach] multiple sessions found:", file=sys.stderr)
    for i, (pipe_name, cwd, child) in enumerate(sessions):
        print(f"  [{i}] pipe={pipe_name!r} cwd={cwd} child={child}", file=sys.stderr)
    idx = input("Attach to which index? ")
    return sessions[int(idx)][0]


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--pipe-name", default=None, help="skip auto-detection")
    p.add_argument("--list", action="store_true", help="list sessions and exit")
    p.add_argument("--registry-dir", default=None,
                   help="folder with the session records (default: the old machine-wide one)")
    p.add_argument("--size", default=None, metavar="COLS,ROWS",
                   help="size to give the session at first (the Sublime tab's), instead of this window's")
    args = p.parse_args()
    global _registry_dir_override
    _registry_dir_override = args.registry_dir
    initial_size = None
    if args.size:
        try:
            cols_text, rows_text = args.size.split(",")
            initial_size = (int(cols_text), int(rows_text))
        except ValueError:
            p.error("--size must be COLS,ROWS")

    if args.list:
        for pipe_name, cwd, child in find_sessions():
            print(f"pipe={pipe_name!r} cwd={cwd} child={child}")
        return

    pipe_name = pick_pipe_name(args.pipe_name)

    h_out = connect_output(pipe_name)
    h_in = connect("\\\\.\\pipe\\" + pipe_name + "-in", _GENERIC_WRITE)
    try:
        h_ctl = connect(
            "\\\\.\\pipe\\" + pipe_name + "-ctl", _GENERIC_WRITE, timeout_s=2.0
        )
    except OSError:
        h_ctl = None
    print("[attach] connected to \\\\.\\pipe\\%s (+ -in) -- close this window "
          "to detach (session keeps running)" % pipe_name, file=sys.stderr)

    # This console's own keyboard and screen handles.
    h_con_in = _k32.GetStdHandle(_STD_INPUT_HANDLE)
    h_con_out = _k32.GetStdHandle(_STD_OUTPUT_HANDLE)
    saved = None
    try:
        saved = _enable_raw_vt(h_con_in, h_con_out)
    except OSError as exc:
        print("[attach] could not enter raw VT mode: %s" % exc, file=sys.stderr)

    print("[attach] this window is %s (cols, rows); the session is kept at %s"
          % (_console_size(h_con_out), initial_size or "this window's size"), file=sys.stderr)
    stop_evt = threading.Event()
    write_lock = threading.Lock()   # the keyboard pump and the TSP replies both write to the input pipe
    relay = _make_tsp_relay(h_in, write_lock, h_con_out)
    print("[attach] TSP text view: %s" % ("ON" if relay is not None else "OFF (raw bytes)"), file=sys.stderr)
    threading.Thread(
        target=_pump_output, args=(h_out, h_con_out, stop_evt, relay), daemon=True
    ).start()
    threading.Thread(
        target=_pump_input, args=(h_con_in, h_in, stop_evt, write_lock), daemon=True
    ).start()
    if h_ctl is not None:
        threading.Thread(
            target=_watch_resize, args=(h_con_out, h_ctl, stop_evt, initial_size), daemon=True
        ).start()

    try:
        while not stop_evt.wait(0.25):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        if saved is not None:
            # Give the window back its normal (cooked) console modes.
            _k32.SetConsoleMode(h_con_in, saved[0])
            _k32.SetConsoleMode(h_con_out, saved[1])
        print("\n[attach] detached (session keeps running)", file=sys.stderr)
        os._exit(0)


if __name__ == "__main__":
    main()
