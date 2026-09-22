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
import struct
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
_ERROR_OPERATION_ABORTED = 995

_PIPE_ACCESS_DUPLEX = 0x00000003
_PIPE_ACCESS_OUTBOUND = 0x00000002
_PIPE_ACCESS_INBOUND = 0x00000001
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_PIPE_UNLIMITED_INSTANCES = 255
_INVALID_HANDLE_VALUE = HANDLE(-1).value
# Private OSC marker between the broker's buffered snapshot and live output.
# Older GhostShell clients safely pass this unknown OSC to their VT parser;
# current clients strip it and use it as an exact bootstrap boundary.
_REPLAY_END = b"\x1b]777;GhostShellReplayEnd\x07"


# Win32 COORD (wincon.h): a column/row cell size, used here only for the
# ConPTY's (cols, rows) argument to CreatePseudoConsole/ResizePseudoConsole.
class _COORD(Structure):
    _fields_ = [("X", SHORT), ("Y", SHORT)]


# Win32 SECURITY_ATTRIBUTES (wtypesbase.h): passed to CreatePipe so the
# pipe's HANDLE is inheritable by the child process CreateProcessW spawns
# (bInheritHandle=True) -- without that, the child can't see its own
# stdin/stdout pipe ends.
class _SECURITY_ATTRIBUTES(Structure):
    _fields_ = [("nLength", DWORD),
                ("lpSecurityDescriptor", c_void_p),
                ("bInheritHandle", BOOL)]


# Win32 STARTUPINFOW (processthreadsapi.h): the legacy fixed-size half of
# STARTUPINFOEXW below. Only cb (struct size) and the hStd* handles are set
# by _Pty._start_child; the rest stay zeroed.
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


# Win32 STARTUPINFOEXW (processthreadsapi.h): STARTUPINFOW plus the extended
# attribute list that is the only way to attach a pseudoconsole to a child
# process -- see _Pty._start_child's InitializeProcThreadAttributeList/
# UpdateProcThreadAttribute calls for how lpAttributeList gets set.
class _STARTUPINFOEXW(Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", c_void_p)]


# Win32 PROCESS_INFORMATION (processthreadsapi.h): CreateProcessW's output --
# handles/ids for the new process and its initial thread. hThread is closed
# immediately after spawn; hProcess is kept for GetExitCodeProcess/
# TerminateProcess/WaitForSingleObject.
class _PROCESS_INFORMATION(Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
# CreatePipe: makes the anonymous pipe pair ConPTY reads/writes through (one
# pair for the child's stdin, one for its stdout).
_k32.CreatePipe.argtypes = [POINTER(HANDLE), POINTER(HANDLE),
                            POINTER(_SECURITY_ATTRIBUTES), DWORD]
_k32.CreatePipe.restype = BOOL
# ConPTY lifecycle proper: Create/Resize/ClosePseudoConsole (wincon.h) -- the
# actual pseudoconsole device, separate from the pipes above and from the
# child process CreateProcessW spawns below.
_k32.CreatePseudoConsole.argtypes = [_COORD, HANDLE, HANDLE, DWORD, POINTER(HANDLE)]
_k32.CreatePseudoConsole.restype = HRESULT
_k32.ResizePseudoConsole.argtypes = [HANDLE, _COORD]
_k32.ResizePseudoConsole.restype = HRESULT
_k32.ClosePseudoConsole.argtypes = [HANDLE]
_k32.ClosePseudoConsole.restype = None
# Proc-thread attribute list (processthreadsapi.h): attaches the
# pseudoconsole handle to CreateProcessW below -- Initialize (size the
# list), Update (set the PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE entry to the
# ConPTY handle), Delete (free it after the child is spawned).
_k32.InitializeProcThreadAttributeList.argtypes = [c_void_p, DWORD, DWORD, POINTER(c_ulong)]
_k32.InitializeProcThreadAttributeList.restype = BOOL
_k32.UpdateProcThreadAttribute.argtypes = [c_void_p, DWORD, DWORD,
                                           c_void_p, c_ulong,
                                           c_void_p, POINTER(c_ulong)]
_k32.UpdateProcThreadAttribute.restype = BOOL
_k32.DeleteProcThreadAttributeList.argtypes = [c_void_p]
_k32.DeleteProcThreadAttributeList.restype = None
# Spawns the actual child (the shell/agent CLI), with the pseudoconsole
# attribute from above attached via lpAttributeList.
_k32.CreateProcessW.argtypes = [LPCWSTR, ctypes.c_wchar_p, c_void_p, c_void_p, BOOL,
                                DWORD, c_void_p, LPCWSTR,
                                POINTER(_STARTUPINFOEXW), POINTER(_PROCESS_INFORMATION)]
_k32.CreateProcessW.restype = BOOL
# Buffer arg must match the read buffer type -- see _Pty.read's own comment
# in ai_terminal.py for why POINTER(c_char), not LPBYTE.
_k32.ReadFile.argtypes = [HANDLE, POINTER(c_char), DWORD, POINTER(DWORD), c_void_p]
_k32.ReadFile.restype = BOOL
_k32.WriteFile.argtypes = [HANDLE, ctypes.c_char_p, DWORD, POINTER(DWORD), c_void_p]
_k32.WriteFile.restype = BOOL
# Process lifecycle group: exit status, killing, waiting, and closing any
# HANDLE this file opens (pipes, process, thread, pseudoconsole).
_k32.GetExitCodeProcess.argtypes = [HANDLE, POINTER(DWORD)]
_k32.GetExitCodeProcess.restype = BOOL
_k32.TerminateProcess.argtypes = [HANDLE, DWORD]
_k32.TerminateProcess.restype = BOOL
_k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
_k32.WaitForSingleObject.restype = DWORD
_k32.CloseHandle.argtypes = [HANDLE]
_k32.CloseHandle.restype = BOOL
# Process heap group (heapapi.h): backs InitializeProcThreadAttributeList
# above -- that call needs a caller-allocated buffer of a size only knowable
# after a first sizing call, which this GetProcessHeap/HeapAlloc/HeapFree
# trio provides.
_k32.GetProcessHeap.restype = ctypes.c_void_p
_k32.HeapAlloc.argtypes = [ctypes.c_void_p, DWORD, c_ulong]
_k32.HeapAlloc.restype = c_void_p
_k32.HeapFree.argtypes = [ctypes.c_void_p, DWORD, c_void_p]
_k32.HeapFree.restype = BOOL
# Named-pipe SERVER group (namedpipeapi.h) -- the broker side of the
# connection ai_terminal.py's _BrokerPty (client side, CreateFileW) connects
# to. CreateNamedPipeW makes one pipe instance; ConnectNamedPipe blocks until
# a client connects to it; DisconnectNamedPipe drops the current client so a
# new one can connect (this is how "detach" then "reattach" work -- the pipe
# instance itself persists); FlushFileBuffers ensures a write has actually
# reached the client before this side proceeds (used before a deliberate
# disconnect, so the client sees every byte).
_k32.CreateNamedPipeW.argtypes = [LPCWSTR, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, c_void_p]
_k32.CreateNamedPipeW.restype = HANDLE
_k32.ConnectNamedPipe.argtypes = [HANDLE, c_void_p]
_k32.ConnectNamedPipe.restype = BOOL
_k32.DisconnectNamedPipe.argtypes = [HANDLE]
_k32.DisconnectNamedPipe.restype = BOOL
_k32.FlushFileBuffers.argtypes = [HANDLE]
_k32.FlushFileBuffers.restype = BOOL
# Unblocks a thread's pending ReadFile/ConnectNamedPipe on a HANDLE from a
# different thread -- same purpose as ai_terminal.py's _BrokerPty.kill use of
# it: CloseHandle can hang otherwise if a blocking call on the same HANDLE is
# still pending elsewhere.
_k32.CancelIoEx.argtypes = [HANDLE, c_void_p]
_k32.CancelIoEx.restype = BOOL
# Used only by _current_process_is_in_job() below, to detect whether this
# broker process itself is inside a Windows job object (Sublime's own
# process is; the broker is deliberately spawned with
# DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB in ai_terminal.py so it is
# not, and can outlive a Sublime restart -- see docs/DETACHABLE_SESSIONS.md).
_k32.GetCurrentProcess.restype = HANDLE
_k32.IsProcessInJob.argtypes = [HANDLE, HANDLE, POINTER(BOOL)]
_k32.IsProcessInJob.restype = BOOL


LIFECYCLE_LOG_SWITCH = "GHOSTSHELL_BROKER_LOG"


def _configure_lifecycle_log(path):
    """Persist broker stdout/stderr, only when the developer switch is on.

    This is a debugging aid for the owner's own installation. It is OFF by default:
    the broker creates no folder, opens no file and holds nothing open unless the
    environment variable GHOSTSHELL_BROKER_LOG is set to "1" (AGENTS.md rule 14).
    With no log stream, print() output is discarded, which is also what happens under
    pythonw.exe, so nothing else in the broker depends on it.
    """
    if not path or os.environ.get(LIFECYCLE_LOG_SWITCH) != "1":
        return
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    stream = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream


def _current_process_is_in_job():
    """True/False if IsProcessInJob succeeds, else a string describing the
    GetLastError failure -- logged once at startup (see main()) so a broker
    that unexpectedly ended up inside a job object (and so cannot survive a
    Sublime restart, defeating the whole point of this process) is visible
    in the lifecycle log rather than silently failing later."""
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
        self._exit_code = None
        self._pc_lock = threading.Lock()
        self._exit_watcher = None
        self._exit_callbacks = []
        self._exit_callbacks_lock = threading.Lock()
        self._cmdline = subprocess.list2cmdline(self.argv)
        self._cwd = cwd or None
        self._env = env
        self._cols = cols
        self._rows = rows

    def start(self):
        """Create the ConPTY (two pipe pairs + CreatePseudoConsole) and hand
        the pty-side ends to _start_child to spawn the real process. Every
        raise path here closes whatever HANDLEs it already opened, since a
        rejected spawn otherwise leaks them for the life of the broker."""
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
        """Spawn the real child process (argv/cwd/env) attached to the
        pseudoconsole self._hPC via a proc-thread attribute list (the
        InitializeProcThreadAttributeList/HeapAlloc pair below is the
        standard double-call pattern: NULL -> get required size -> allocate
        -> call again for real), the only mechanism CreateProcessW exposes
        for attaching a ConPTY."""
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

    def on_exit(self, callback):
        """Register a callback fired once, from the exit-watcher thread,
        the moment the child process is confirmed dead. Used to unstick a
        server thread parked in a blocking Windows call (e.g.
        ConnectNamedPipe waiting for the *next* client) that would
        otherwise never re-check is_alive() and never notice the child
        died -- see _OutputServer._cancel_pending_accept.

        Fires immediately (still via the caller's thread, not the exit
        watcher) if the child has already exited by the time this is
        called -- e.g. a child that crashes on startup, before the
        constructor registering this callback even runs -- so a slow
        registration can't miss the one-shot exit event and leak the same
        way the un-cancellable ConnectNamedPipe wait used to."""
        with self._exit_callbacks_lock:
            if not self.is_alive():
                already_fired = True
            else:
                self._exit_callbacks.append(callback)
                already_fired = False
        if already_fired:
            callback()

    def _fire_exit_callbacks(self):
        with self._exit_callbacks_lock:
            callbacks, self._exit_callbacks = self._exit_callbacks, []
        for callback in callbacks:
            try:
                callback()
            except OSError:
                print("[agent_broker] exit callback failed:\n%s" % traceback.format_exc())

    def _watch_process_exit(self):
        """Block on WaitForSingleObject until the child exits on its own
        (ReadFile on hOutRead does not return EOF just because every process
        attached to the console exited -- conhost only flushes the final
        frame and closes the pipe once ClosePseudoConsole is called), then
        record its exit code via GetExitCodeProcess, fire on_exit callbacks
        (see _OutputServer._cancel_pending_accept, which registers one so a
        pending ConnectNamedPipe wakes up once the child is gone), and close
        the pseudoconsole."""
        h = self._hProcess
        if h is None:
            return
        try:
            _k32.WaitForSingleObject(h, _INFINITE)
            code = DWORD(0)
            if _k32.GetExitCodeProcess(h, byref(code)):
                self._exit_code = code.value
                print(
                    "[%s] child process exited pid=%d exit_code=%d (0x%08X)"
                    % (
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        self.pid,
                        self._exit_code,
                        self._exit_code,
                    )
                )
            else:
                print(
                    "[%s] child process exited pid=%d exit_code=unknown "
                    "(GetLastError %d)"
                    % (
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        self.pid,
                        ctypes.get_last_error(),
                    )
                )
        except OSError:
            print("[agent_broker] exit watcher failed:\n%s" % traceback.format_exc())
        self._alive = False
        self._fire_exit_callbacks()
        self._close_pc()

    def exit_code(self):
        """Return the child's cached/current Windows process exit code, if known."""
        if self._exit_code is not None:
            return self._exit_code
        if self._hProcess is None:
            return None
        code = DWORD(0)
        if not _k32.GetExitCodeProcess(self._hProcess, byref(code)):
            return None
        if code.value == _STILL_ACTIVE:
            return None
        self._exit_code = code.value
        return self._exit_code

    def _close_pc(self):
        """Close the pseudoconsole HANDLE (idempotent, lock-guarded so the
        exit watcher thread and an explicit kill() can't double-close)."""
        with self._pc_lock:
            if self._hPC is not None:
                _k32.ClosePseudoConsole(self._hPC)
                self._hPC = None

    def read(self, on_data):
        """Blocking reader loop; calls on_data(bytes) until EOF. Run on a
        daemon thread -- see main()'s _Pty.read thread. Unlike
        ai_terminal.py's _BrokerPty.read (the client side), there is no
        replay-marker bookkeeping here: this is the raw output the broker
        feeds straight to _OutputServer.feed. _REPLAY_END itself is written
        separately, by _OutputServer.run_forever right after a client
        connects (snapshot, then the marker, then live feed() data)."""
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
        """Blocking WriteFile to the pseudoconsole's input pipe, looping
        until every byte of data is accepted (a single WriteFile call is not
        guaranteed to consume the whole buffer)."""
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
        """Actually resize the ConPTY (unlike _ControlServer's RESIZE line
        handling, which just calls this) -- returns True iff ConPTY accepted
        the new size, so a caller can tell a rejected resize apart from an
        applied one."""
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
        """GetExitCodeProcess-backed liveness check, not just the cached
        self._alive flag -- catches a child that exited on its own before
        _watch_process_exit's WaitForSingleObject got scheduled."""
        if not self._alive or self._hProcess is None:
            return False
        code = DWORD(0)
        if _k32.GetExitCodeProcess(self._hProcess, byref(code)):
            if code.value != _STILL_ACTIVE:
                self._alive = False
                return False
        return self._alive

    def kill(self):
        """End the child for real -- unlike ai_terminal.py's client-side
        _BrokerPty.kill (which only disconnects a client, leaving the broker
        and child running), this IS the broker, so this actually terminates
        the process: close the pseudoconsole, TerminateProcess, release
        every HANDLE. Called from _ControlServer's KILL line handler."""
        if not self._alive:
            return
        self._alive = False
        self._close_pc()
        if self._hProcess is not None:
            _k32.TerminateProcess(self._hProcess, 0)
        self._close_handles()

    def _close_handles(self):
        """CloseHandle every pipe/process/thread HANDLE this instance owns
        (the pseudoconsole itself is closed separately via _close_pc)."""
        for h in (self._hInWrite, self._hOutRead, self._hThread, self._hProcess):
            if h is not None:
                _k32.CloseHandle(h)
        self._hInWrite = self._hOutRead = self._hThread = self._hProcess = None
        self._release_attr_list()

    def _release_attr_list(self):
        """Free the proc-thread attribute list and its backing heap
        allocation from _start_child's InitializeProcThreadAttributeList /
        HeapAlloc pair -- the two are not the same allocation and both must
        be released, in this order (list first, then the heap it lives in)."""
        if self._attr_list is not None:
            _k32.DeleteProcThreadAttributeList(self._attr_list)
            self._attr_list = None
        if self._heap_buf is not None:
            _k32.HeapFree(self._heap_buf[0], 0, self._heap_buf[1])
            self._heap_buf = None


_GSB_MAGIC = b"GSB1"
_GSB_HEADER = struct.Struct("<4sIII")
_GSB_HEADER_SIZE = _GSB_HEADER.size
_GSB_FSYNC_BYTES = 256 * 1024
_GSB_FSYNC_S = 1.0

class _Scrollback:
    def __init__(self, max_bytes, path=None):
        self._max = max(0, int(max_bytes))
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._path = path
        self._file = None
        self._start = 0
        self._length = 0
        self._unsynced = 0
        self._last_fsync = time.monotonic()
        if path:
            self._open_file(path)

    def _open_file(self, path):
        folder = os.path.dirname(os.path.abspath(path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        handle = open(path, "w+b")
        handle.write(_GSB_HEADER.pack(_GSB_MAGIC, self._max, 0, 0))
        if self._max:
            handle.truncate(_GSB_HEADER_SIZE + self._max)
        handle.flush()
        self._file = handle

    def append(self, data):
        if not data:
            return
        with self._lock:
            self._buf.extend(data)
            if len(self._buf) > self._max:
                del self._buf[: len(self._buf) - self._max]
            self._append_file(data)
            self._maybe_fsync_locked()

    def _append_file(self, data):
        if self._file is None or not self._max:
            return
        cap = self._max
        if len(data) >= cap:
            data = data[-cap:]
            self._write_ring(0, data)
            self._start = 0
            self._length = cap
            self._write_header()
            self._unsynced += len(data)
            return
        pos = (self._start + self._length) % cap
        self._write_ring(pos, data)
        new_total = self._length + len(data)
        if new_total > cap:
            self._start = (self._start + new_total - cap) % cap
            self._length = cap
        else:
            self._length = new_total
        self._write_header()
        self._unsynced += len(data)

    def _write_ring(self, pos, data):
        cap = self._max
        first = cap - pos
        self._file.seek(_GSB_HEADER_SIZE + pos)
        if len(data) <= first:
            self._file.write(data)
            return
        self._file.write(data[:first])
        self._file.seek(_GSB_HEADER_SIZE)
        self._file.write(data[first:])

    def _write_header(self):
        self._file.seek(0)
        self._file.write(_GSB_HEADER.pack(
            _GSB_MAGIC, self._max, self._start, self._length
        ))

    def _fsync_locked(self):
        if self._file is None:
            return
        self._file.flush()
        os.fsync(self._file.fileno())
        self._unsynced = 0
        self._last_fsync = time.monotonic()

    def _maybe_fsync_locked(self):
        if self._file is None:
            return
        if (self._unsynced >= _GSB_FSYNC_BYTES
                or (time.monotonic() - self._last_fsync) >= _GSB_FSYNC_S):
            self._fsync_locked()

    def snapshot(self):
        with self._lock:
            self._fsync_locked()
            return bytes(self._buf)

    def close(self):
        path = None
        with self._lock:
            if self._file is None and not self._path:
                return
            if self._file is not None:
                try:
                    self._fsync_locked()
                finally:
                    self._file.close()
                    self._file = None
            path = self._path
            self._path = None
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def read_scrollback_file(path):
    """Reconstruct snapshot bytes from a GSB1 file. Tests/debug only."""
    with open(path, "rb") as handle:
        header = handle.read(_GSB_HEADER_SIZE)
        if len(header) != _GSB_HEADER_SIZE:
            return b""
        magic, cap, start, length = _GSB_HEADER.unpack(header)
        if magic != _GSB_MAGIC or cap == 0 or length == 0:
            return b""
        length = min(length, cap)
        start %= cap
        payload = handle.read(cap)
        if len(payload) < cap:
            payload += b"\x00" * (cap - len(payload))
        end = start + length
        if end <= cap:
            return payload[start:end]
        return payload[start:] + payload[:end - cap]


def _scrollback_path_for_registry(registry_path):
    if not registry_path:
        return None
    root, _ext = os.path.splitext(registry_path)
    return root + ".scrollback"


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
        # The listen handle currently blocked in ConnectNamedPipe, waiting
        # for the *next* client -- distinct from _client_handle above,
        # which is only set once a client has actually connected. Without
        # this, a child that dies while nobody is attached leaves
        # run_forever's ConnectNamedPipe call parked with no timeout: it
        # never re-checks pty.is_alive(), so run_forever() never returns,
        # main()'s `finally: _remove_registry()` never runs, and the
        # broker leaks forever as an unkillable phantom entry in "list
        # sessions" (confirmed live 2026-09-15: dead child, live broker
        # process, orphaned registry file on disk for days). pty.on_exit
        # below cancels this pending accept the moment the child dies, so
        # the loop falls through to its own is_alive() check and exits
        # cleanly.
        self._pending_lock = threading.Lock()
        self._pending_handle = None
        pty.on_exit(self._cancel_pending_accept)

    def _cancel_pending_accept(self):
        """pty.on_exit callback: CancelIoEx the ConnectNamedPipe call
        run_forever is currently blocked in, so a dead child unblocks the
        accept loop immediately instead of leaving it parked forever (see
        the class docstring's pty.on_exit paragraph)."""
        with self._pending_lock:
            handle = self._pending_handle
        if handle is not None:
            _k32.CancelIoEx(handle, None)

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
        """Blocking WriteFile loop to a connected client HANDLE, looping
        until every byte of data is accepted."""
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
        """Accept-loop, one client at a time, until the child exits: create
        one named-pipe instance (CreateNamedPipeW), block in ConnectNamedPipe
        for a client, send the scrollback snapshot + _REPLAY_END marker so a
        reattaching client can tell replay from live output apart, then wait
        (polling _client_handle, not blocking on any I/O -- this pipe is
        write-only, see the class docstring) until feed() notices the client
        is gone, and recycle the pipe instance for the next connection."""
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

            with self._pending_lock:
                self._pending_handle = handle
            ok = _k32.ConnectNamedPipe(handle, None)
            with self._pending_lock:
                self._pending_handle = None
            if not ok:
                err = ctypes.get_last_error()
                if err == _ERROR_OPERATION_ABORTED:
                    # Cancelled by _cancel_pending_accept because the child
                    # died while nobody was connected -- not an error, just
                    # the signal to fall through to the loop's own
                    # is_alive() check and exit run_forever().
                    _k32.CloseHandle(handle)
                    continue
                if err != _ERROR_PIPE_CONNECTED:
                    print("[agent_broker] ConnectNamedPipe(out) failed (GetLastError %d)"
                          % err)
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
        # Tracks the currently-connected client's handle so force_disconnect()
        # (called from the control server's thread) can kick it off. Guarded
        # by a lock since it's written from run_forever's thread and read
        # from whichever thread calls force_disconnect().
        self._handle_lock = threading.Lock()
        self._current_handle = None

    def run_forever(self):
        """Accept-loop, one client at a time, until the child exits: create
        one named-pipe instance, block in ConnectNamedPipe, then hand the
        connected client to _serve_client until it disconnects (naturally,
        or via force_disconnect's CancelIoEx), disconnect/close and recycle
        the pipe instance for the next connection -- same shape as
        _OutputServer.run_forever, but inbound and with a real read loop
        instead of a poll."""
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
            with self._handle_lock:
                self._current_handle = handle
            try:
                self._serve_client(handle)
            finally:
                with self._handle_lock:
                    self._current_handle = None
                _k32.DisconnectNamedPipe(handle)
                _k32.CloseHandle(handle)
                if self._on_disconnect is not None:
                    self._on_disconnect()

    def force_disconnect(self):
        """Kick the currently-connected client off, without touching the
        child process. Used to hand a session back from a Windows Terminal
        relay to Sublime without the user having to close that window first
        -- see _ControlServer's DISCONNECT command.

        CancelIoEx on another thread unblocks _serve_client's blocking
        ReadFile (same technique ai_terminal.py's _BrokerPty.kill() uses on
        its own reader thread); run_forever's own finally block then does
        the real DisconnectNamedPipe/CloseHandle and fires on_disconnect,
        which releases the output side too -- no separate call needed there.
        """
        with self._handle_lock:
            handle = self._current_handle
        if handle is not None:
            _k32.CancelIoEx(handle, None)

    def _serve_client(self, handle):
        """Blocking ReadFile loop on one connected client's input pipe;
        every chunk read is written straight to the PTY (self._pty.write --
        the real keystroke path from client to child)."""
        buf = (c_char * 4096)()
        n = DWORD(0)
        while self._pty.is_alive():
            ok = _k32.ReadFile(handle, buf, 4096, byref(n), None)
            if not ok:
                err = ctypes.get_last_error()
                if err in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA,
                           _ERROR_PIPE_NOT_CONNECTED, _ERROR_HANDLE_EOF,
                           _ERROR_OPERATION_ABORTED):
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

    def __init__(self, name, pty, input_server=None):
        self._name = name + "-ctl"
        self._pty = pty
        self._input_server = input_server

    def run_forever(self):
        # nMaxInstances used to be 1, served synchronously in this same loop
        # iteration -- fine while the only ctl client was a long-lived
        # resize-watcher (ai_terminal.py's _BrokerPty, or recover_console.py's
        # _watch_resize thread) holding the pipe open for the session's whole
        # lifetime. DISCONNECT changed that: a second, short-lived ctl
        # connection now needs to get in *while* that long-lived one is still
        # connected (to kick it off in the first place) -- impossible with a
        # single instance served one-at-a-time. PIPE_UNLIMITED_INSTANCES plus
        # dispatching each connection to its own thread lets this loop go
        # straight back to accepting the next connection instead of blocking
        # in _serve_client for as long as the current client stays attached.
        while self._pty.is_alive():
            handle = _k32.CreateNamedPipeW(
                _pipe_path(self._name),
                _PIPE_ACCESS_DUPLEX,
                _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
                _PIPE_UNLIMITED_INSTANCES,
                4096, 4096, 0, None,
            )
            if handle == _INVALID_HANDLE_VALUE:
                time.sleep(1)
                continue
            ok = _k32.ConnectNamedPipe(handle, None)
            if not ok and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                _k32.CloseHandle(handle)
                continue
            threading.Thread(
                target=self._run_client, args=(handle,), daemon=True
            ).start()

    def _run_client(self, handle):
        """Per-connection thread body: serve this one control client until
        it disconnects, then always DisconnectNamedPipe/CloseHandle -- run in
        its own thread (see run_forever's comment) so a long-lived ctl
        client does not block the accept loop from seeing the next one."""
        try:
            self._serve_client(handle)
        finally:
            _k32.DisconnectNamedPipe(handle)
            _k32.CloseHandle(handle)

    def _serve_client(self, handle):
        """Blocking ReadFile loop, splitting the byte stream on b"\\n" into
        whole lines and dispatching each to _handle_line (RESIZE/KILL/
        DISCONNECT) -- a line can arrive split across ReadFile calls, so
        partial data is held in `pending` until a full line is seen."""
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
                print("[agent_broker] RESIZE line parse failed for %r:\n%s"
                      % (line, traceback.format_exc()))
                return
            self._pty.resize(cols, rows)
        elif parts[0] == "KILL":
            # Explicit end-of-session request (as opposed to a client just
            # disconnecting, which must NOT kill anything). run_forever()'s
            # `while self._pty.is_alive()` loops both stop on their own once
            # this returns; main()'s `finally: pty.kill()` is then a no-op.
            self._pty.kill()
        elif parts[0] == "DISCONNECT":
            # Hand the session back from whatever currently holds it (e.g. a
            # Windows Terminal relay) without touching the child process --
            # see _InputServer.force_disconnect. A caller with nothing
            # attached to disconnect (input_server not wired, or no client
            # currently connected) is a silent no-op, not an error: the
            # requester (ai_terminal.py's reattach command) treats "pipe
            # already free" the same as "just freed it."
            if self._input_server is not None:
                self._input_server.force_disconnect()


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
            print("[agent_broker] could not remove launch file %s:\n%s"
                  % (path, traceback.format_exc()))
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


def _publish_registry(path, pipe_name, profile_name, cwd, child_argv, child_pid=None):
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
        "child_pid": child_pid,
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
    for target in (path, _scrollback_path_for_registry(path)):
        try:
            os.unlink(target)
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

    scrollback = _Scrollback(
        args.scrollback_bytes,
        path=_scrollback_path_for_registry(args.registry_file),
    )
    out_server = _OutputServer(args.pipe_name, pty, scrollback)
    in_server = _InputServer(
        args.pipe_name, pty, on_disconnect=out_server.disconnect_client
    )

    reader = threading.Thread(target=pty.read, args=(out_server.feed,), daemon=True)
    reader.start()

    threading.Thread(target=in_server.run_forever, daemon=True).start()

    ctl = _ControlServer(args.pipe_name, pty, input_server=in_server)
    threading.Thread(target=ctl.run_forever, daemon=True).start()

    _publish_registry(
        args.registry_file, args.pipe_name, args.profile_name, cwd, child_argv,
        child_pid=pty.pid,
    )

    print("[agent_broker] serving \\\\.\\pipe\\%s (+ -in, -ctl) -- Ctrl+C to stop and kill the child"
          % args.pipe_name)
    try:
        out_server.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("[%s] broker stopping normally; child_alive=%r child_exit_code=%r" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), pty.is_alive(), pty.exit_code(),
        ))
        pty.kill()
        scrollback.close()
        _remove_registry(args.registry_file)
    print("[agent_broker] child exited, broker stopping")


if __name__ == "__main__":
    main()
