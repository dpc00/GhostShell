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
import asyncio
import collections
import ctypes
import json
import os
import re
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

try:
    import conpty_api       # run as a script: tools/ is on sys.path
except ImportError:
    from . import conpty_api    # imported as part of the tools package

if os.name != "nt":
    sys.exit("agent_broker.py is Windows-only (ConPTY + named pipes).")

import _winapi  # noqa: E402  (Windows only)
from asyncio import windows_utils  # noqa: E402

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


class _PtyOutput(asyncio.Protocol):
    """Receives the pseudoconsole's output from the event loop (see _Pty.attach)."""

    def __init__(self, pty):
        self._pty = pty

    def connection_made(self, transport):
        self._pty._read_transport = transport

    def data_received(self, data):
        self._pty._on_output(data)

    def eof_received(self):
        return False

    def connection_lost(self, exc):
        self._pty._on_eof()


# How long, after the child has exited, the broker keeps waiting for the pseudoconsole to deliver the child's last
# output (the end of the stream) before it stops waiting.
_FINAL_OUTPUT_WAIT_S = 2.0


class _Pty:
    """A child process attached to a Windows pseudoconsole.

    All I/O runs on the broker's one event loop, with no threads: the two pipe ends on this side are overlapped
    (asyncio.windows_utils.pipe) while the ends the pseudoconsole uses are ordinary ones. Output arrives through a
    read transport, input goes out through the loop's overlapped WriteFile, and the child's exit is a wait on its
    process handle registered with the loop."""

    def __init__(self, argv, cwd, cols, rows, env):
        self.argv = list(argv)
        self.pid = 0
        self._hPC = None
        self._out_pipe = None      # our read end of the pseudoconsole's output (PipeHandle, overlapped)
        self._in_pipe = None       # our write end of the pseudoconsole's input (PipeHandle, overlapped)
        self._hProcess = None
        self._hThread = None
        self._attr_list = None
        self._heap_buf = None
        self._alive = True
        self._exit_code = None
        self._cmdline = subprocess.list2cmdline(self.argv)
        self._cwd = cwd or None
        self._env = env
        # Which ConPTY to use (see conpty_api.py). A profile names a folder in its
        # spawn_env; the value is for this broker, not the child, so it is removed
        # from the environment the child receives.
        self._conpty_folder = ""
        self._conpty = None
        if isinstance(env, dict) and conpty_api.CONPTY_FOLDER_ENV_VAR in env:
            self._env = dict(env)
            self._conpty_folder = self._env.pop(conpty_api.CONPTY_FOLDER_ENV_VAR) or ""
        self._cols = cols
        self._rows = rows
        # Event-loop state, created by attach().
        self._read_transport = None
        self._on_data = None
        self._eof = None           # set when the output pipe has reached its end
        self.exited = None         # set when the child is gone
        self.finished = None       # set when it is gone AND its last output has been delivered (or the wait ran out)
        self._write_lock = None
        self._watch_task = None

    def start(self):
        """Create the ConPTY (two pipe pairs + CreatePseudoConsole) and hand
        the pty-side ends to _start_child to spawn the real process. Every
        raise path here closes whatever HANDLEs it already opened, since a
        rejected spawn otherwise leaks them for the life of the broker."""
        # Our ends overlapped (the event loop waits on them), the pseudoconsole's ends plain.
        out_r, out_w = windows_utils.pipe(overlapped=(True, False), bufsize=65536)
        in_r, in_w = windows_utils.pipe(overlapped=(False, True), bufsize=65536)
        self._out_pipe = windows_utils.PipeHandle(out_r)
        self._in_pipe = windows_utils.PipeHandle(in_w)

        hPC = HANDLE()
        try:
            self._conpty = conpty_api.select_api(_k32, self._conpty_folder)
            print("[agent_broker] ConPTY: %s" % self._conpty.source)
            hr = self._conpty.create(_COORD(self._cols, self._rows),
                                     HANDLE(in_r), HANDLE(out_w), 0, byref(hPC))
        except OSError:
            _winapi.CloseHandle(in_r)
            _winapi.CloseHandle(out_w)
            self._close_pipes()
            raise
        _winapi.CloseHandle(in_r)       # the pseudoconsole holds its own copies now
        _winapi.CloseHandle(out_w)
        if hr & 0x80000000:
            self._close_pipes()
            raise OSError(f"CreatePseudoConsole failed: HRESULT 0x{hr & 0xffffffff:08X}")
        self._hPC = hPC.value

        try:
            self._start_child()
        except BaseException:
            self._close_pc()
            self._close_pipes()
            self._release_attr_list()
            self._alive = False
            raise

    def _start_child(self):
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
                f"CreateProcessW failed (GetLastError {err}) while launching child process"
            )
        self._hProcess = pi.hProcess
        self._hThread = pi.hThread
        self.pid = pi.dwProcessId

    async def attach(self, on_data):
        """Start delivering the pseudoconsole's output to on_data(bytes) and watching for the child's exit. Must run
        on the event loop that will serve the pipes."""
        loop = asyncio.get_running_loop()
        self._on_data = on_data
        self._eof = asyncio.Event()
        self.exited = asyncio.Event()
        self.finished = asyncio.Event()
        self._write_lock = asyncio.Lock()
        await loop.connect_read_pipe(lambda: _PtyOutput(self), self._out_pipe)
        self._watch_task = loop.create_task(self._watch_process_exit())

    def _on_output(self, data):
        if self._on_data is not None:
            self._on_data(data)

    def _on_eof(self):
        """The output pipe reached its end (conhost closes it once the pseudoconsole is closed)."""
        self._alive = False
        self._eof.set()
        self._check_finished()

    def _check_finished(self):
        if self.exited.is_set() and self._eof.is_set():
            self.finished.set()

    def _mark_exited(self):
        if self.exited is None or self.exited.is_set():
            return
        self.exited.set()
        # The last output normally arrives within moments of the exit; do not wait forever for it.
        asyncio.get_running_loop().call_later(_FINAL_OUTPUT_WAIT_S, self.finished.set)
        self._check_finished()

    def pause_reading(self):
        """Stop reading the pseudoconsole's output (a slow client is behind); the child then blocks on its own writes."""
        transport = self._read_transport
        if transport is not None and not transport.is_closing():
            transport.pause_reading()

    def resume_reading(self):
        transport = self._read_transport
        if transport is not None and not transport.is_closing():
            transport.resume_reading()

    async def _watch_process_exit(self):
        """Wait (on the loop, no thread) for the child to exit on its own
        (ReadFile on the output pipe does not return EOF just because every process
        attached to the console exited -- conhost only flushes the final
        frame and closes the pipe once ClosePseudoConsole is called), then
        record its exit code via GetExitCodeProcess, close the pseudoconsole
        (which ends the output pipe) and wake everything waiting on `exited`."""
        h = self._hProcess
        if h is None:
            return
        try:
            await asyncio.get_running_loop()._proactor.wait_for_handle(h)
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
        self._close_pc()
        self._mark_exited()

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
        """Close the pseudoconsole HANDLE (idempotent)."""
        if self._hPC is not None:
            self._conpty.close(self._hPC)
            self._hPC = None

    async def write(self, data):
        """Write data to the pseudoconsole's input pipe on the loop, looping
        until every byte is accepted (a single write is not guaranteed to
        consume the whole buffer); writers are serialized so input keeps its order."""
        if not self._alive or self._in_pipe is None:
            return
        proactor = asyncio.get_running_loop()._proactor
        async with self._write_lock:
            while data:
                written = await proactor.send(self._in_pipe, data)
                if not written:
                    raise OSError(
                        "WriteFile accepted 0 of %d bytes of terminal input" % len(data)
                    )
                data = data[written:]

    def resize(self, cols, rows):
        """Actually resize the ConPTY (unlike _ControlServer's RESIZE line
        handling, which just calls this) -- returns True iff ConPTY accepted
        the new size, so a caller can tell a rejected resize apart from an
        applied one."""
        if not self._alive or self._hPC is None:
            return False
        try:
            hr = self._conpty.resize(self._hPC, _COORD(cols, rows))
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
        the exit wait completed."""
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
        if self._watch_task is not None:
            self._watch_task.cancel()
        self._close_pc()
        if self._hProcess is not None:
            _k32.TerminateProcess(self._hProcess, 0)
        self._close_handles()
        self._mark_exited()

    def _close_pipes(self):
        """Close our two pipe ends (idempotent); the read end belongs to the read transport once attached."""
        for pipe in (self._out_pipe, self._in_pipe):
            if pipe is not None:
                pipe.close()

    def _close_handles(self):
        """Close every pipe/process/thread HANDLE this instance owns
        (the pseudoconsole itself is closed separately via _close_pc)."""
        if self._read_transport is not None:
            self._read_transport.close()
        self._close_pipes()
        for h in (self._hThread, self._hProcess):
            if h is not None:
                _k32.CloseHandle(h)
        self._hThread = self._hProcess = None
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


# Output backlog (bytes queued for the attached client) at which the pseudoconsole is no longer read, and the level at
# which reading resumes. A client that stops reading therefore stalls the child instead of growing the broker's memory.
_OUT_HIGH_WATER = 1024 * 1024
_OUT_LOW_WATER = 256 * 1024
_FLAG_OVERLAPPED = 0x40000000


def _create_pipe_instance(name, access, instances, out_buffer, in_buffer):
    """One overlapped instance of the named pipe `name`, as an asyncio PipeHandle (closing it closes the handle).
    The broker's end is overlapped so the event loop can wait on it; a client may open its own end either way."""
    handle = _winapi.CreateNamedPipe(
        _pipe_path(name),
        access | _FLAG_OVERLAPPED,
        _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
        instances, out_buffer, in_buffer, 0, _winapi.NULL,
    )
    return windows_utils.PipeHandle(handle)


async def _accept_or_stop(pipe, stop):
    """Wait for a client to connect to `pipe`. Returns True when one did, False when the `stop` event was set first
    (the child is gone); the pending accept is then cancelled."""
    loop = asyncio.get_running_loop()
    accept = loop._proactor.accept_pipe(pipe)
    stopper = loop.create_task(stop.wait())
    try:
        await asyncio.wait({accept, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if accept.done() and not accept.cancelled():
            accept.result()
            return True
        return False
    finally:
        stopper.cancel()
        if not accept.done():
            accept.cancel()


def _disconnect_and_close(pipe):
    """DisconnectNamedPipe + close: the instance is then discarded (the next client gets a fresh instance)."""
    try:
        _k32.DisconnectNamedPipe(pipe.fileno())
    except (OSError, ValueError):
        pass
    pipe.close()


class _Client:
    """One attached output client: its pipe, its queue of unsent chunks, and the task that sends them in order."""

    def __init__(self, pipe):
        self.pipe = pipe
        self.chunks = collections.deque()
        self.pending = 0
        self.wake = asyncio.Event()
        self.gone = asyncio.Event()     # a write failed, or the matching input client left
        self.closing = False            # send what is queued, then stop
        self.sender = None

    def push(self, data):
        if data:
            self.chunks.append(data)
            self.pending += len(data)
            self.wake.set()


class _OutputServer:
    """Broker -> client output, one client at a time, on its OWN pipe
    (<name>). Deliberately write-only from the broker's side (never reads this
    handle): client keystrokes go over the separate _InputServer pipe. (An earlier
    duplex, thread-per-direction version had a WriteFile that could simply never
    complete while a ReadFile was pending on the same handle from another thread.)

    Everything runs on the broker's event loop. feed() is called with each chunk the pseudoconsole produces; it
    always records the chunk in the scrollback and, when a client is attached, queues it for that client's sender
    task. Attaching publishes the scrollback snapshot, the replay marker and the live feed with no await in
    between, so no output can fall between the snapshot and the live data."""

    def __init__(self, name, pty, scrollback):
        self._name = name
        self._pty = pty
        self._scrollback = scrollback
        self._client = None
        self._ever_connected = False

    def feed(self, data):
        try:
            self._scrollback.append(data)
        except OSError:
            print("[agent_broker] scrollback write failed:\n%s" % traceback.format_exc())
        client = self._client
        if client is None or client.gone.is_set():
            return
        client.push(data)
        if client.pending > _OUT_HIGH_WATER:
            self._pty.pause_reading()

    def disconnect_client(self):
        """Release the output side when the matching input client leaves.

        An outbound-only named pipe cannot notice a quiet client disappearing
        until the next write. The input server *can* notice immediately,
        so it uses this hook to prevent a silent session from remaining pinned
        forever to a dead client.
        """
        client = self._client
        if client is not None:
            client.gone.set()

    @property
    def ever_connected(self):
        return self._ever_connected

    async def _send_loop(self, client):
        proactor = asyncio.get_running_loop()._proactor
        try:
            while True:
                while not client.chunks:
                    if client.closing:
                        return
                    client.wake.clear()
                    await client.wake.wait()
                data = client.chunks.popleft()
                client.pending -= len(data)
                while data:
                    written = await proactor.send(client.pipe, data)
                    if not written:
                        raise OSError("write accepted 0 bytes to pipe client")
                    data = data[written:]
                if client.pending < _OUT_LOW_WATER:
                    self._pty.resume_reading()
        except OSError:
            client.gone.set()
            self._pty.resume_reading()

    async def run_forever(self):
        """Accept-loop, one client at a time, until the child is gone: create
        one overlapped named-pipe instance, wait for a client (a dead child cancels that
        wait), queue the scrollback snapshot + _REPLAY_END marker so a reattaching client can
        tell replay from live output apart, then wait until the client is gone or the child
        has finished, let the queue drain, and recycle the pipe instance for the next client."""
        loop = asyncio.get_running_loop()
        while self._pty.is_alive():
            try:
                pipe = _create_pipe_instance(self._name, _PIPE_ACCESS_OUTBOUND, 1, 65536, 0)
            except OSError as e:
                print("[agent_broker] CreateNamedPipe(out) failed: %s" % e)
                await asyncio.sleep(1)
                continue
            try:
                connected = await _accept_or_stop(pipe, self._pty.exited)
            except OSError as e:
                print("[agent_broker] ConnectNamedPipe(out) failed: %s" % e)
                pipe.close()
                continue
            if not connected:
                # The child died while nobody was attached -- not an error, the loop's own is_alive() check ends it.
                pipe.close()
                continue

            print("[agent_broker] client attached to pipe %r" % self._name)
            client = _Client(pipe)
            client.push(self._scrollback.snapshot())
            client.push(_REPLAY_END)
            self._client = client
            self._ever_connected = True
            client.sender = loop.create_task(self._send_loop(client))

            waits = {loop.create_task(client.gone.wait()), loop.create_task(self._pty.finished.wait())}
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for w in waits:
                w.cancel()

            if self._client is client:
                self._client = None
            if client.gone.is_set():
                client.sender.cancel()
            else:
                # The child has finished: let the client receive what is still queued (its last output).
                client.closing = True
                client.wake.set()
                try:
                    await asyncio.wait_for(client.sender, 2.0)
                except (asyncio.TimeoutError, OSError):
                    client.sender.cancel()
            _disconnect_and_close(pipe)
            self._pty.resume_reading()
            print("[agent_broker] client detached from pipe %r" % self._name)


class _InputServer:
    """Client -> broker input, one client at a time, on its own pipe
    (<name>-in). Read-only from the broker's side -- nothing else ever
    writes to this handle."""

    def __init__(self, name, pty, on_disconnect=None):
        self._name = name + "-in"
        self._pty = pty
        self._on_disconnect = on_disconnect
        self._serving = None        # the task serving the currently connected client, for force_disconnect()
        self._kicked = False        # set by force_disconnect, so its cancellation is told apart from the broker's own

    async def run_forever(self):
        """Accept-loop, one client at a time, until the child is gone: create
        one overlapped named-pipe instance, wait for a client, then serve it until it
        disconnects (naturally, or via force_disconnect), disconnect/close and recycle
        the pipe instance for the next connection -- same shape as _OutputServer.run_forever."""
        loop = asyncio.get_running_loop()
        while self._pty.is_alive():
            try:
                pipe = _create_pipe_instance(self._name, _PIPE_ACCESS_INBOUND, 1, 0, 65536)
            except OSError as e:
                print("[agent_broker] CreateNamedPipe(in) failed: %s" % e)
                await asyncio.sleep(1)
                continue
            try:
                connected = await _accept_or_stop(pipe, self._pty.exited)
            except OSError:
                pipe.close()
                continue
            if not connected:
                pipe.close()
                continue
            self._serving = loop.create_task(self._serve_client(pipe))
            try:
                await self._serving
            except asyncio.CancelledError:
                # force_disconnect() cancelled the serving task: carry on with the next client. Any other
                # cancellation (the broker is stopping) is passed on.
                if not self._kicked:
                    raise
            finally:
                self._kicked = False
                self._serving = None
                _disconnect_and_close(pipe)
                if self._on_disconnect is not None:
                    self._on_disconnect()

    def force_disconnect(self):
        """Kick the currently-connected client off, without touching the
        child process. Used to hand a session back from a Windows Terminal
        relay to Sublime without the user having to close that window first
        -- see _ControlServer's DISCONNECT command.

        Cancelling the serving task cancels its pending overlapped read; run_forever's own finally block then does
        the real DisconnectNamedPipe/close and fires on_disconnect, which releases the output side too -- no
        separate call needed there.
        """
        task = self._serving
        if task is not None:
            self._kicked = True
            task.cancel()

    async def _serve_client(self, pipe):
        """Read loop on one connected client's input pipe; every chunk read
        is written straight to the PTY (self._pty.write -- the real
        keystroke path from client to child)."""
        proactor = asyncio.get_running_loop()._proactor
        while self._pty.is_alive():
            try:
                data = await proactor.recv(pipe, 4096)
            except OSError:
                return              # the client left (broken pipe) or the read was aborted
            if not data:
                return
            try:
                await self._pty.write(data)
            except OSError as e:
                print("[agent_broker] write to PTY failed: %s" % e)
                return


class _ControlServer:
    """Serves out-of-band control commands (RESIZE, KILL, DISCONNECT) on a
    second pipe, <name>-ctl. Kept separate from the data pipe so a control
    command can never collide with literal bytes typed into the child
    (arrow keys, escape sequences, pasted text, etc)."""

    def __init__(self, name, pty, input_server=None):
        self._name = name + "-ctl"
        self._pty = pty
        self._input_server = input_server

    async def run_forever(self):
        # Any number of ctl connections at once: a long-lived resize watcher can stay attached while a short-lived
        # DISCONNECT or KILL connection gets in. Each connection is served by its own task.
        loop = asyncio.get_running_loop()
        clients = set()
        try:
            while self._pty.is_alive():
                try:
                    pipe = _create_pipe_instance(self._name, _PIPE_ACCESS_DUPLEX,
                                                 _PIPE_UNLIMITED_INSTANCES, 4096, 4096)
                except OSError:
                    await asyncio.sleep(1)
                    continue
                try:
                    connected = await _accept_or_stop(pipe, self._pty.exited)
                except OSError:
                    pipe.close()
                    continue
                if not connected:
                    pipe.close()
                    continue
                task = loop.create_task(self._run_client(pipe))
                clients.add(task)
                task.add_done_callback(clients.discard)
        finally:
            for task in list(clients):
                task.cancel()

    async def _run_client(self, pipe):
        """Serve this one control client until it disconnects, then always
        DisconnectNamedPipe/close."""
        try:
            await self._serve_client(pipe)
        finally:
            _disconnect_and_close(pipe)

    async def _serve_client(self, pipe):
        """Read loop, splitting the byte stream on b"\\n" into whole lines and
        dispatching each to _handle_line (RESIZE/KILL/DISCONNECT) -- a line can
        arrive split across reads, so partial data is held in `pending` until
        a full line is seen."""
        proactor = asyncio.get_running_loop()._proactor
        pending = b""
        while self._pty.is_alive():
            try:
                data = await proactor.recv(pipe, 256)
            except OSError:
                return
            if not data:
                return
            pending += data
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
            # disconnecting, which must NOT kill anything). The serving loops
            # all stop on their own once the child is gone; main()'s
            # `finally: pty.kill()` is then a no-op.
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


async def _run(args, child_argv, launch_env):
    """Everything the broker does after startup, on one event loop and one thread: start the child, serve the
    output, input and control pipes, and tear down once the child is gone and its last output has been delivered."""
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
    ctl = _ControlServer(args.pipe_name, pty, input_server=in_server)

    loop = asyncio.get_running_loop()
    side_tasks = []
    child_exit_code = None
    exited_before_client = False
    early_exit_output = b""
    try:
        await pty.attach(out_server.feed)
        side_tasks = [loop.create_task(in_server.run_forever()), loop.create_task(ctl.run_forever())]
        out_task = loop.create_task(out_server.run_forever())

        _publish_registry(
            args.registry_file, args.pipe_name, args.profile_name, cwd, child_argv,
            child_pid=pty.pid,
        )

        print("[agent_broker] serving \\\\.\\pipe\\%s (+ -in, -ctl) -- Ctrl+C to stop and kill the child"
              % args.pipe_name)
        await out_task
    finally:
        child_exit_code = pty.exit_code()
        exited_before_client = (
            not out_server.ever_connected and child_exit_code is not None
        )
        if exited_before_client:
            early_exit_output = scrollback.snapshot()
        print("[%s] broker stopping normally; child_alive=%r child_exit_code=%r" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), pty.is_alive(), pty.exit_code(),
        ))
        pty.kill()
        scrollback.close()
        _remove_registry(args.registry_file)
        for task in side_tasks:
            task.cancel()
    if exited_before_client:
        child_output = early_exit_output[-8192:].decode("utf-8", errors="replace")
        child_output = re.sub(
            r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))",
            "",
            child_output,
        )
        child_output = "".join(
            char for char in child_output
            if char in "\n\t" or char.isprintable()
        ).strip()
        output_detail = (
            "\nCaptured child output (last 8192 bytes):\n" + child_output
            if child_output else "\nThe child produced no capturable terminal output."
        )
        raise RuntimeError(
            "child process exited before the broker pipe accepted a client "
            "(exit code %s)" % child_exit_code + output_detail
        )
    print("[agent_broker] child exited, broker stopping")


def _main():
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

    try:
        asyncio.run(_run(args, child_argv, launch_env))
    except KeyboardInterrupt:
        pass


def main():
    try:
        _main()
    except BaseException:
        startup_error_file = None
        argv = sys.argv[1:]
        if len(argv) == 2 and argv[0] == "--launch-file":
            startup_error_file = argv[1] + ".broker-error"
        if startup_error_file:
            try:
                with open(startup_error_file, "w", encoding="utf-8") as handle:
                    handle.write(
                        time.strftime("%Y-%m-%d %H:%M:%S")
                        + " broker failed:\n"
                        + traceback.format_exc()
                    )
            except OSError as error:
                print(
                    "[agent_broker] could not write startup diagnostic %s: %s"
                    % (startup_error_file, error),
                    file=sys.stderr,
                )
        raise


if __name__ == "__main__":
    main()
