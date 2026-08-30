"""agent_broker.py -- run any CLI (agent or plain shell) in a Windows ConPTY,
reachable over a named pipe, so a client can disconnect and reconnect to the
SAME running process later.

Usage:
    python tools/agent_broker.py --pipe-name test1 -- cmd.exe

Then, from another window/process, connect a client (see
agent_broker_client.py) to \\\\.\\pipe\\test1. Close the client, the broker
and its child keep running. Connect a new client to the same pipe name and
you're back in the same live process -- not a fresh one.
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from ctypes import (
    Structure,
    POINTER,
    byref,
    c_void_p,
    c_char,
    c_ulong,
    sizeof,
)
from ctypes.wintypes import HANDLE, DWORD, WORD, BOOL, LPCWSTR, LPBYTE, SHORT

if os.name != "nt":
    sys.exit("agent_broker.py is Windows-only (ConPTY + named pipes).")

HRESULT = ctypes.c_long

_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_STARTF_USESTDHANDLES = 0x00000100
_STILL_ACTIVE = 259
_INFINITE = 0xFFFFFFFF
_ERROR_HANDLE_EOF = 38
_ERROR_BROKEN_PIPE = 109
_ERROR_PIPE_CONNECTED = 535
_ERROR_NO_DATA = 232
_ERROR_PIPE_NOT_CONNECTED = 233

_PIPE_ACCESS_DUPLEX = 0x00000003
_PIPE_ACCESS_OUTBOUND = 0x00000002
_PIPE_ACCESS_INBOUND = 0x00000001
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_INVALID_HANDLE_VALUE = HANDLE(-1).value
# Private OSC marker between the broker's buffered snapshot and live output.
# Older GhostShell clients safely pass this unknown OSC to their VT parser;
# current clients strip it and use it as an exact bootstrap boundary.
_REPLAY_END = b"\x1b]777;GhostShellReplayEnd\x07"


class _COORD(Structure):
    _fields_ = [("X", SHORT), ("Y", SHORT)]


class _SECURITY_ATTRIBUTES(Structure):
    _fields_ = [("nLength", DWORD),
                ("lpSecurityDescriptor", c_void_p),
                ("bInheritHandle", BOOL)]


class _STARTUPINFOW(Structure):
    _fields_ = [("cb", DWORD), ("lpReserved", c_void_p),
                ("lpDesktop", c_void_p), ("lpTitle", c_void_p),
                ("dwX", DWORD), ("dwY", DWORD),
                ("dwXSize", DWORD), ("dwYSize", DWORD),
                ("dwXCountChars", DWORD), ("dwYCountChars", DWORD),
                ("dwFillAttribute", DWORD), ("dwFlags", DWORD),
                ("wShowWindow", WORD), ("cbReserved2", WORD),
                ("lpReserved2", LPBYTE),
                ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE)]


class _STARTUPINFOEXW(Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", c_void_p)]


class _PROCESS_INFORMATION(Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreatePipe.argtypes = [POINTER(HANDLE), POINTER(HANDLE),
                            POINTER(_SECURITY_ATTRIBUTES), DWORD]
_k32.CreatePipe.restype = BOOL
_k32.CreatePseudoConsole.argtypes = [_COORD, HANDLE, HANDLE, DWORD, POINTER(HANDLE)]
_k32.CreatePseudoConsole.restype = HRESULT
_k32.ResizePseudoConsole.argtypes = [HANDLE, _COORD]
_k32.ResizePseudoConsole.restype = HRESULT
_k32.ClosePseudoConsole.argtypes = [HANDLE]
_k32.ClosePseudoConsole.restype = None
_k32.InitializeProcThreadAttributeList.argtypes = [c_void_p, DWORD, DWORD, POINTER(c_ulong)]
_k32.InitializeProcThreadAttributeList.restype = BOOL
_k32.UpdateProcThreadAttribute.argtypes = [c_void_p, DWORD, DWORD,
                                           c_void_p, c_ulong,
                                           c_void_p, POINTER(c_ulong)]
_k32.UpdateProcThreadAttribute.restype = BOOL
_k32.DeleteProcThreadAttributeList.argtypes = [c_void_p]
_k32.DeleteProcThreadAttributeList.restype = None
_k32.CreateProcessW.argtypes = [LPCWSTR, ctypes.c_wchar_p, c_void_p, c_void_p, BOOL,
                                DWORD, c_void_p, LPCWSTR,
                                POINTER(_STARTUPINFOEXW), POINTER(_PROCESS_INFORMATION)]
_k32.CreateProcessW.restype = BOOL
_k32.ReadFile.argtypes = [HANDLE, POINTER(c_char), DWORD, POINTER(DWORD), c_void_p]
_k32.ReadFile.restype = BOOL
_k32.WriteFile.argtypes = [HANDLE, ctypes.c_char_p, DWORD, POINTER(DWORD), c_void_p]
_k32.WriteFile.restype = BOOL
_k32.GetExitCodeProcess.argtypes = [HANDLE, POINTER(DWORD)]
_k32.GetExitCodeProcess.restype = BOOL
_k32.TerminateProcess.argtypes = [HANDLE, DWORD]
_k32.TerminateProcess.restype = BOOL
_k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
_k32.WaitForSingleObject.restype = DWORD
_k32.CloseHandle.argtypes = [HANDLE]
_k32.CloseHandle.restype = BOOL
_k32.GetProcessHeap.restype = ctypes.c_void_p
_k32.HeapAlloc.argtypes = [ctypes.c_void_p, DWORD, c_ulong]
_k32.HeapAlloc.restype = c_void_p
_k32.HeapFree.argtypes = [ctypes.c_void_p, DWORD, c_void_p]
_k32.HeapFree.restype = BOOL
_k32.CreateNamedPipeW.argtypes = [LPCWSTR, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, c_void_p]
_k32.CreateNamedPipeW.restype = HANDLE
_k32.ConnectNamedPipe.argtypes = [HANDLE, c_void_p]
_k32.ConnectNamedPipe.restype = BOOL
_k32.DisconnectNamedPipe.argtypes = [HANDLE]
_k32.DisconnectNamedPipe.restype = BOOL
_k32.FlushFileBuffers.argtypes = [HANDLE]
_k32.FlushFileBuffers.restype = BOOL
_k32.GetCurrentProcess.restype = HANDLE
_k32.IsProcessInJob.argtypes = [HANDLE, HANDLE, POINTER(BOOL)]
_k32.IsProcessInJob.restype = BOOL


def _configure_lifecycle_log(path):
    """Persist broker stdout/stderr so an abrupt parent-exit kill is visible."""
    if not path:
        return
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    stream = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream


def _current_process_is_in_job():
    result = BOOL(False)
    ok = _k32.IsProcessInJob(_k32.GetCurrentProcess(), None, byref(result))
    return bool(result.value) if ok else "unknown(error=%d)" % ctypes.get_last_error()


class _Pty:
    """A child process attached to a Windows pseudoconsole."""

    def __init__(self, argv, cwd, cols, rows, env):
        self.argv = list(argv)
        self.pid = 0
        self._hPC = None
        self._hInWrite = None
        self._hOutRead = None
        self._hProcess = None
        self._hThread = None
        self._attr_list = None
        self._heap_buf = None
        self._alive = True
        self._pc_lock = threading.Lock()
        self._exit_watcher = None
        self._cmdline = subprocess.list2cmdline(self.argv)
        self._cwd = cwd or None
        self._env = env
        self._cols = cols
        self._rows = rows

    def start(self):
        hPipePtyIn = HANDLE()
        hInWrite = HANDLE()
        hOutRead = HANDLE()
        hPipePtyOut = HANDLE()
        if not _k32.CreatePipe(byref(hPipePtyIn), byref(hInWrite), None, 0):
            raise OSError("CreatePipe(input) failed")
        if not _k32.CreatePipe(byref(hOutRead), byref(hPipePtyOut), None, 0):
            _k32.CloseHandle(hPipePtyIn)
            _k32.CloseHandle(hInWrite)
            raise OSError("CreatePipe(output) failed")

        hPC = HANDLE()
        try:
            hr = _k32.CreatePseudoConsole(_COORD(self._cols, self._rows),
                                          hPipePtyIn, hPipePtyOut, 0, byref(hPC))
        except OSError:
            _k32.CloseHandle(hPipePtyIn)
            _k32.CloseHandle(hPipePtyOut)
            _k32.CloseHandle(hInWrite)
            _k32.CloseHandle(hOutRead)
            raise
        _k32.CloseHandle(hPipePtyIn)
        _k32.CloseHandle(hPipePtyOut)
        if hr & 0x80000000:
            _k32.CloseHandle(hInWrite)
            _k32.CloseHandle(hOutRead)
            raise OSError(f"CreatePseudoConsole failed: HRESULT 0x{hr & 0xffffffff:08X}")
        self._hPC = hPC.value

        try:
            self._start_child(hInWrite, hOutRead)
        except BaseException:
            self._close_pc()
            _k32.CloseHandle(hInWrite)
            _k32.CloseHandle(hOutRead)
            self._release_attr_list()
            self._alive = False
            raise

    def _start_child(self, hInWrite, hOutRead):
        size = c_ulong(0)
        _k32.InitializeProcThreadAttributeList(None, 1, 0, byref(size))
        heap = _k32.GetProcessHeap()
        buf = _k32.HeapAlloc(heap, 0, size.value)
        if not buf:
            raise OSError("HeapAlloc attribute list failed")
        attr = c_void_p(buf)
        self._heap_buf = (heap, buf)
        if not _k32.InitializeProcThreadAttributeList(attr, 1, 0, byref(size)):
            raise OSError(
                "InitializeProcThreadAttributeList failed (GetLastError %d)"
                % ctypes.get_last_error()
            )
        self._attr_list = attr
        if not _k32.UpdateProcThreadAttribute(attr, 0, _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                                              self._hPC, sizeof(HANDLE), None, None):
            raise OSError(
                "UpdateProcThreadAttribute failed (GetLastError %d)"
                % ctypes.get_last_error()
            )

        si = _STARTUPINFOEXW()
        si.StartupInfo.cb = sizeof(_STARTUPINFOEXW)
        si.StartupInfo.dwFlags |= _STARTF_USESTDHANDLES
        si.lpAttributeList = attr.value
        pi = _PROCESS_INFORMATION()
        cmd = ctypes.create_unicode_buffer(self._cmdline)
        cwd = ctypes.c_wchar_p(self._cwd) if self._cwd else None
        envblock = "".join(f"{k}={v}\x00" for k, v in self._env.items()) + "\x00"
        envbuf = ctypes.create_unicode_buffer(envblock)
        flags = _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT
        ok = _k32.CreateProcessW(None, cmd, None, None, False, flags,
                                 envbuf, cwd, byref(si), byref(pi))
        if not ok:
            err = ctypes.get_last_error()
            raise OSError(
                f"CreateProcessW failed (GetLastError {err}) for cmdline: {self._cmdline!r}"
            )
        self._hProcess = pi.hProcess
        self._hThread = pi.hThread
        self.pid = pi.dwProcessId
        self._hInWrite = hInWrite
        self._hOutRead = hOutRead
        self._exit_watcher = threading.Thread(target=self._watch_process_exit, daemon=True)
        self._exit_watcher.start()

    def _watch_process_exit(self):
        h = self._hProcess
        if h is None:
            return
        try:
            _k32.WaitForSingleObject(h, _INFINITE)
        except Exception:
            print("[agent_broker] exit watcher failed:\n%s" % traceback.format_exc())
        self._close_pc()

    def _close_pc(self):
        with self._pc_lock:
            if self._hPC is not None:
                _k32.ClosePseudoConsole(self._hPC)
                self._hPC = None

    def read(self, on_data):
        buf = (c_char * 8192)()
        n = DWORD(0)
        while self._alive:
            ok = _k32.ReadFile(self._hOutRead, buf, 8192, byref(n), None)
            if not ok:
                err = ctypes.get_last_error()
                self._alive = False
                if err in (0, _ERROR_HANDLE_EOF, _ERROR_BROKEN_PIPE):
                    return
                raise OSError(
                    "ReadFile on the pseudoconsole output failed (GetLastError %d)" % err
                )
            if n.value == 0:
                break
            on_data(bytes(buf[: n.value]))
        self._alive = False

    def write(self, data):
        if not self._alive or self._hInWrite is None:
            return
        written = DWORD(0)
        while data:
            if not _k32.WriteFile(self._hInWrite, data, len(data), byref(written), None):
                raise OSError(
                    "WriteFile to the pseudoconsole input failed (GetLastError %d)"
                    % ctypes.get_last_error()
                )
            if not written.value:
                raise OSError(
                    "WriteFile accepted 0 of %d bytes of terminal input" % len(data)
                )
            data = data[written.value:]

    def resize(self, cols, rows):
        if not self._alive or self._hPC is None:
            return False
        try:
            hr = _k32.ResizePseudoConsole(self._hPC, _COORD(cols, rows))
        except OSError as e:
            print(f"[agent_broker] ResizePseudoConsole({cols}, {rows}) failed: {e}")
            return False
        if hr & 0x80000000:
            print(
                "[agent_broker] ResizePseudoConsole(%d, %d) failed: HRESULT 0x%08X"
                % (cols, rows, hr & 0xFFFFFFFF)
            )
            return False
        self._cols, self._rows = cols, rows
        return True

    def is_alive(self):
        if not self._alive or self._hProcess is None:
            return False
        code = DWORD(0)
        if _k32.GetExitCodeProcess(self._hProcess, byref(code)):
            if code.value != _STILL_ACTIVE:
                self._alive = False
                return False
        return self._alive

    def kill(self):
        if not self._alive:
            return
        self._alive = False
        self._close_pc()
        if self._hProcess is not None:
            _k32.TerminateProcess(self._hProcess, 0)
        self._close_handles()

    def _close_handles(self):
        for h in (self._hInWrite, self._hOutRead, self._hThread, self._hProcess):
            if h is not None:
                _k32.CloseHandle(h)
        self._hInWrite = self._hOutRead = self._hThread = self._hProcess = None
        self._release_attr_list()

    def _release_attr_list(self):
        if self._attr_list is not None:
            _k32.DeleteProcThreadAttributeList(self._attr_list)
            self._attr_list = None
        if self._heap_buf is not None:
            _k32.HeapFree(self._heap_buf[0], 0, self._heap_buf[1])
            self._heap_buf = None


class _Scrollback:
    def __init__(self, max_bytes):
        self._max = max_bytes
        self._buf = bytearray()
        self._lock = threading.Lock()

    def append(self, data):
        with self._lock:
            self._buf.extend(data)
            if len(self._buf) > self._max:
                del self._buf[: len(self._buf) - self._max]

    def snapshot(self):
        with self._lock:
            return bytes(self._buf)


def _pipe_path(name):
    return "\\\\.\\pipe\\" + name


class _OutputServer:
    """Broker -> client output, one client at a time, on its OWN pipe
    (<name>). Deliberately write-only from the broker's side (never issues
    ReadFile on this handle) -- an earlier duplex version had the broker's
    reader thread WriteFile()ing here while a second thread ReadFile()'d the
    SAME handle for client keystrokes, and that concurrent read+write on one
    synchronous named-pipe handle was empirically unreliable: confirmed live
    that a WriteFile can simply never complete (no error, just never
    returns) when a ReadFile is pending on the same handle from another
    thread, silently killing the session after just the first few bytes.
    Client keystrokes now go over the separate _InputServer pipe instead."""

    def __init__(self, name, pty, scrollback):
        self._name = name
        self._pty = pty
        self._scrollback = scrollback
        self._client_handle = None
        self._client_lock = threading.Lock()

    def feed(self, data):
        with self._client_lock:
            self._scrollback.append(data)
            h = self._client_handle
            if h is None:
                return
            try:
                self._write(h, data)
            except OSError:
                if self._client_handle is h:
                    self._client_handle = None

    def _write(self, handle, data):
        written = DWORD(0)
        while data:
            if not _k32.WriteFile(handle, data, len(data), byref(written), None):
                raise OSError("WriteFile to pipe client failed (GetLastError %d)"
                              % ctypes.get_last_error())
            if not written.value:
                raise OSError("WriteFile accepted 0 bytes to pipe client")
            data = data[written.value:]

    def disconnect_client(self):
        """Release the output side when the matching input client leaves.

        An outbound-only named pipe cannot notice a quiet client disappearing
        until the next WriteFile.  The input server *can* notice immediately,
        so it uses this hook to prevent a silent session from remaining pinned
        forever to a dead client.
        """
        with self._client_lock:
            self._client_handle = None

    def run_forever(self):
        while self._pty.is_alive():
            handle = _k32.CreateNamedPipeW(
                _pipe_path(self._name),
                _PIPE_ACCESS_OUTBOUND,
                _PIPE_TYPE_BYTE | _PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
                1,
                65536, 0, 0, None,
            )
            if handle == _INVALID_HANDLE_VALUE:
                print("[agent_broker] CreateNamedPipeW(out) failed (GetLastError %d)"
                      % ctypes.get_last_error())
                time.sleep(1)
                continue

            ok = _k32.ConnectNamedPipe(handle, None)
            if not ok and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                print("[agent_broker] ConnectNamedPipe(out) failed (GetLastError %d)"
                      % ctypes.get_last_error())
                _k32.CloseHandle(handle)
                continue

            print("[agent_broker] client attached to pipe %r" % self._name)
            try:
                # Serialize snapshot + marker + live publication against
                # feed(). This closes the old gap where output appended after
                # snapshot() but before _client_handle was published was sent
                # neither in the snapshot nor as live data.
                with self._client_lock:
                    self._write(handle, self._scrollback.snapshot())
                    self._write(handle, _REPLAY_END)
                    self._client_handle = handle
            except OSError:
                _k32.CloseHandle(handle)
                continue
            # No read side to block on here -- just wait until feed() (on the
            # reader thread) notices a write failure and clears the handle,
            # i.e. the client disconnected. Bounded poll, not a busy spin.
            while self._pty.is_alive():
                with self._client_lock:
                    if self._client_handle is not handle:
                        break
                time.sleep(0.2)

            _k32.FlushFileBuffers(handle)
            _k32.DisconnectNamedPipe(handle)
            _k32.CloseHandle(handle)
            print("[agent_broker] client detached from pipe %r" % self._name)


class _InputServer:
    """Client -> broker input, one client at a time, on its own pipe
    (<name>-in). Read-only from the broker's side -- nothing else ever
    writes to this handle, so no concurrent-read/write risk here either."""

    def __init__(self, name, pty, on_disconnect=None):
        self._name = name + "-in"
        self._pty = pty
        self._on_disconnect = on_disconnect

    def run_forever(self):
        while self._pty.is_alive():
            handle = _k32.CreateNamedPipeW(
                _pipe_path(self._name),
                _PIPE_ACCESS_INBOUND,
                _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
                1,
                0, 65536, 0, None,
            )
            if handle == _INVALID_HANDLE_VALUE:
                print("[agent_broker] CreateNamedPipeW(in) failed (GetLastError %d)"
                      % ctypes.get_last_error())
                time.sleep(1)
                continue
            ok = _k32.ConnectNamedPipe(handle, None)
            if not ok and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                _k32.CloseHandle(handle)
                continue
            try:
                self._serve_client(handle)
            finally:
                _k32.DisconnectNamedPipe(handle)
                _k32.CloseHandle(handle)
                if self._on_disconnect is not None:
                    self._on_disconnect()

    def _serve_client(self, handle):
        buf = (c_char * 4096)()
        n = DWORD(0)
        while self._pty.is_alive():
            ok = _k32.ReadFile(handle, buf, 4096, byref(n), None)
            if not ok:
                err = ctypes.get_last_error()
                if err in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA,
                           _ERROR_PIPE_NOT_CONNECTED, _ERROR_HANDLE_EOF):
                    return
                print("[agent_broker] ReadFile on input pipe failed (GetLastError %d)" % err)
                return
            if n.value == 0:
                return
            try:
                self._pty.write(bytes(buf[: n.value]))
            except OSError as e:
                print("[agent_broker] write to PTY failed: %s" % e)
                return


class _ControlServer:
    """Serves out-of-band control commands (currently just RESIZE) on a
    second pipe, <name>-ctl. Kept separate from the data pipe so a control
    command can never collide with literal bytes typed into the child
    (arrow keys, escape sequences, pasted text, etc)."""

    def __init__(self, name, pty):
        self._name = name + "-ctl"
        self._pty = pty

    def run_forever(self):
        while self._pty.is_alive():
            handle = _k32.CreateNamedPipeW(
                _pipe_path(self._name),
                _PIPE_ACCESS_DUPLEX,
                _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
                1,
                4096, 4096, 0, None,
            )
            if handle == _INVALID_HANDLE_VALUE:
                time.sleep(1)
                continue
            ok = _k32.ConnectNamedPipe(handle, None)
            if not ok and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                _k32.CloseHandle(handle)
                continue
            try:
                self._serve_client(handle)
            finally:
                _k32.DisconnectNamedPipe(handle)
                _k32.CloseHandle(handle)

    def _serve_client(self, handle):
        buf = (c_char * 256)()
        n = DWORD(0)
        pending = b""
        while self._pty.is_alive():
            ok = _k32.ReadFile(handle, buf, 256, byref(n), None)
            if not ok or n.value == 0:
                return
            pending += bytes(buf[: n.value])
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                self._handle_line(line.strip())

    def _handle_line(self, line):
        parts = line.decode("utf-8", "replace").split()
        if not parts:
            return
        if len(parts) == 3 and parts[0] == "RESIZE":
            try:
                cols, rows = int(parts[1]), int(parts[2])
            except ValueError:
                return
            self._pty.resize(cols, rows)
        elif parts[0] == "KILL":
            # Explicit end-of-session request (as opposed to a client just
            # disconnecting, which must NOT kill anything). run_forever()'s
            # `while self._pty.is_alive()` loops both stop on their own once
            # this returns; main()'s `finally: pty.kill()` is then a no-op.
            self._pty.kill()


def _parse_env_overrides(pairs):
    env = os.environ.copy()
    for p in pairs or []:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        env[k] = v
    return env


def _load_launch_file(argv):
    """Expand a one-use launch file before normal argument parsing."""
    if len(argv) != 2 or argv[0] != "--launch-file":
        own_argv, child_argv = _split_argv(argv)
        return own_argv, child_argv, None
    path = argv[1]
    try:
        with open(path, "r", encoding="utf-8") as handle:
            launch = json.load(handle)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    env = launch.get("environment") if isinstance(launch, dict) else None
    own_argv = launch.get("broker_argv") if isinstance(launch, dict) else None
    child_argv = launch.get("child_argv") if isinstance(launch, dict) else None
    if not isinstance(own_argv, list) or not all(isinstance(v, str) for v in own_argv):
        raise ValueError("invalid broker argv in launch file")
    if not isinstance(child_argv, list) or not all(isinstance(v, str) for v in child_argv):
        raise ValueError("invalid child argv in launch file")
    if not isinstance(env, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in env.items()):
        raise ValueError("invalid environment in launch file")
    return own_argv, child_argv, env


def _split_argv(argv):
    if "--" not in argv:
        return argv, []
    i = argv.index("--")
    return argv[:i], argv[i + 1:]


def _publish_registry(path, pipe_name, profile_name, cwd, child_argv):
    if not path:
        return
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    temporary = path + ".tmp-%d" % os.getpid()
    record = {
        "pipe_name": pipe_name,
        "profile_name": profile_name,
        "cwd": cwd,
        "child_argv": child_argv,
        "broker_pid": os.getpid(),
        "created_at": time.time(),
    }
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _remove_registry(path):
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def main():
    own_argv, child_argv, launch_env = _load_launch_file(sys.argv[1:])

    p = argparse.ArgumentParser(
        description="Run any CLI in a ConPTY, reachable over a named pipe."
    )
    p.add_argument("--pipe-name", required=True,
                    help="Named pipe identifier (served at \\\\.\\pipe\\<name>).")
    p.add_argument("--cwd", default=None, help="Working directory for the child.")
    p.add_argument("--cols", type=int, default=120)
    p.add_argument("--rows", type=int, default=40)
    p.add_argument("--scrollback-bytes", type=int, default=2 * 1024 * 1024,
                    help="Replayed to each newly (re)connected client.")
    p.add_argument("--registry-file", default=None,
                    help="Atomic live-session record removed when the broker exits.")
    p.add_argument("--log-file", default=None,
                    help="Append-only broker lifecycle diagnostic log.")
    p.add_argument("--profile-name", default=None)
    p.add_argument("--env", action="append", default=[],
                    help="KEY=VALUE, repeatable, merged onto the broker's own environment.")
    args = p.parse_args(own_argv)

    _configure_lifecycle_log(args.log_file)
    print("[%s] broker starting pid=%d parent_pid=%d in_job=%r pipe=%s" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), os.getppid(),
        _current_process_is_in_job(), args.pipe_name,
    ))

    if not child_argv:
        p.error("no child command given -- pass it after `--`, e.g. ... -- cmd.exe")

    env = launch_env if launch_env is not None else os.environ.copy()
    for pair in args.env or []:
        if "=" in pair:
            key, value = pair.split("=", 1)
            env[key] = value
    cwd = os.path.realpath(args.cwd) if args.cwd else os.getcwd()

    pty = _Pty(child_argv, cwd, args.cols, args.rows, env)
    pty.start()
    print("[agent_broker] spawned pid=%d cwd=%s argv=%s" % (pty.pid, cwd, child_argv))

    scrollback = _Scrollback(args.scrollback_bytes)
    out_server = _OutputServer(args.pipe_name, pty, scrollback)
    in_server = _InputServer(
        args.pipe_name, pty, on_disconnect=out_server.disconnect_client
    )

    reader = threading.Thread(target=pty.read, args=(out_server.feed,), daemon=True)
    reader.start()

    threading.Thread(target=in_server.run_forever, daemon=True).start()

    ctl = _ControlServer(args.pipe_name, pty)
    threading.Thread(target=ctl.run_forever, daemon=True).start()

    _publish_registry(
        args.registry_file, args.pipe_name, args.profile_name, cwd, child_argv
    )

    print("[agent_broker] serving \\\\.\\pipe\\%s (+ -in, -ctl) -- Ctrl+C to stop and kill the child"
          % args.pipe_name)
    try:
        out_server.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("[%s] broker stopping normally; child_alive=%r" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), pty.is_alive(),
        ))
        pty.kill()
        _remove_registry(args.registry_file)
    print("[agent_broker] child exited, broker stopping")


if __name__ == "__main__":
    main()
