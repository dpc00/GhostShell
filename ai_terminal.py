
"""ai_terminal.py -- bare-bones owned terminal for the Claude CLI.

Replaces the Terminus dependency for AI launch. No third-party packages: pure
ctypes against the Windows ConPTY (Pseudoconsole) API, plus a small cursor-aware
ANSI renderer tailored to the subset Claude's ratatui TUI emits. Because all of
the rendering/state code is ours, every bug is fixable here.

Architecture:
  terminal/  -- pure core (Screen, Parser, colours, keys, render) — unit-testable
  _Pty          -- ConPTY wrapper (ctypes). Spawns the child, gives us a byte stream.
  _Terminal     -- owns a _Pty + Screen + Parser; registry keyed by view id.
  renderer      -- debounced, walks Screen -> view text on the main thread.
  listener      -- forwards keystrokes from the view to the PTY; kills PTY on close.

Commands (ST names):
  ai_terminal_open_here / ai_terminal_open_in_editor
  ai_terminal_send_string / ai_terminal_keypress / ai_terminal_render
  ai_terminal_nuke / ai_terminal_noop / ai_terminal_dump_screen

Note on input: ST does not fire on_text_command for unbound printable keys, so
Default.sublime-keymap binds every printable/special key to ai_terminal_keypress
(gated by setting.ai_terminal_view); ai_terminal_keypress translates the key to
terminal bytes and writes them to the PTY. The on_text_command listener is kept
as a fallback for any key-bound commands that still dispatch as insert/move.
"""

import codecs
import collections
import ctypes
import errno
import gc
import json
import math
import os
import tempfile
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from functools import lru_cache

import sublime
import sublime_plugin

# ─── ctypes ConPTY binding (guarded: a failure must not crash the plugin load) ─────

_PTY_OK = False
_k32 = None

# A closed pseudoconsole surfaces as one of these on ReadFile rather than as a
# clean zero-byte read, so they mean "child gone", not "pipe broke".
_ERROR_HANDLE_EOF = 38
_ERROR_BROKEN_PIPE = 109

if os.name == "nt":
    try:
        import ctypes
        from ctypes import (
            Structure,
            POINTER,
            byref,
            c_void_p,
            c_char,
            c_ulong,
            sizeof,
            windll,
        )
        from ctypes.wintypes import HANDLE, DWORD, WORD, BOOL, LPCWSTR, LPBYTE, SHORT, FILETIME

        # wintypes does not export HRESULT; it is a signed LONG.
        HRESULT = ctypes.c_long

        _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
        _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
        _CREATE_UNICODE_ENVIRONMENT = 0x00000400
        _STARTF_USESTDHANDLES = 0x00000100

        # Win32 COORD (wincon.h): a column/row cell size, used here only for
        # the ConPTY's (cols, rows) argument to CreatePseudoConsole/
        # ResizePseudoConsole.
        class _COORD(Structure):
            _fields_ = [("X", SHORT), ("Y", SHORT)]

        # Win32 SECURITY_ATTRIBUTES (wtypesbase.h): passed to CreatePipe so
        # the pipe's HANDLE is inheritable by the child process CreateProcessW
        # spawns (bInheritHandle=True) -- without that, the child can't see
        # its own stdin/stdout pipe ends.
        class _SECURITY_ATTRIBUTES(Structure):
            _fields_ = [("nLength", DWORD),
                        ("lpSecurityDescriptor", c_void_p),
                        ("bInheritHandle", BOOL)]

        # Win32 STARTUPINFOW (processthreadsapi.h): the legacy fixed-size half
        # of STARTUPINFOEXW below. Only cb (struct size) and the hStd* handles
        # are set here; the rest stay zeroed (this process does not redirect a
        # console window position/size or icon).
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

        # Win32 STARTUPINFOEXW (processthreadsapi.h): STARTUPINFOW plus the
        # extended attribute list that is the only way to attach a
        # pseudoconsole to a child process -- CreateProcessW takes this
        # (not plain STARTUPINFOW) when _EXTENDED_STARTUPINFO_PRESENT is set,
        # with lpAttributeList carrying the PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE
        # entry (see _spawn's InitializeProcThreadAttributeList/
        # UpdateProcThreadAttribute calls).
        class _STARTUPINFOEXW(Structure):
            _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", c_void_p)]

        # Win32 PROCESS_INFORMATION (processthreadsapi.h): CreateProcessW's
        # output -- handles/ids for the new process and its initial thread.
        # hThread is closed immediately after spawn (never waited on); hProcess
        # is kept for GetExitCodeProcess/TerminateProcess/WaitForSingleObject.
        class _PROCESS_INFORMATION(Structure):
            _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                        ("dwProcessId", DWORD), ("dwThreadId", DWORD)]

        # use_last_error=True so ctypes.get_last_error() works after CreateProcessW.
        # windll.kernel32 does not preserve last-error (reports 0 on failure).
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Set argtypes/restype on EVERY function -- without these ctypes truncates
        # 64-bit HANDLEs to c_int and ConPTY silently corrupts.
        # CreatePipe: makes the anonymous pipe pair ConPTY reads/writes through
        # (one pair for the child's stdin, one for its stdout).
        _k32.CreatePipe.argtypes = [POINTER(HANDLE), POINTER(HANDLE),
                                    POINTER(_SECURITY_ATTRIBUTES), DWORD]
        _k32.CreatePipe.restype = BOOL
        # ConPTY lifecycle proper: Create/Resize/ClosePseudoConsole
        # (wincon.h) -- the actual pseudoconsole device, separate from the
        # pipes above and from the child process CreateProcessW spawns below.
        _k32.CreatePseudoConsole.argtypes = [_COORD, HANDLE, HANDLE, DWORD, POINTER(HANDLE)]
        _k32.CreatePseudoConsole.restype = HRESULT
        _k32.ResizePseudoConsole.argtypes = [HANDLE, _COORD]
        _k32.ResizePseudoConsole.restype = HRESULT
        _k32.ClosePseudoConsole.argtypes = [HANDLE]
        _k32.ClosePseudoConsole.restype = None
        # Proc-thread attribute list (processthreadsapi.h): the mechanism that
        # attaches the pseudoconsole handle to CreateProcessW below --
        # Initialize (size the list), Update (set the
        # PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE entry to the ConPTY handle),
        # Delete (free it after the child is spawned; the list itself is not
        # needed once CreateProcessW returns).
        _k32.InitializeProcThreadAttributeList.argtypes = [c_void_p, DWORD, DWORD, POINTER(c_ulong)]
        _k32.InitializeProcThreadAttributeList.restype = BOOL
        _k32.UpdateProcThreadAttribute.argtypes = [c_void_p, DWORD, DWORD,
                                                   c_void_p, c_ulong,
                                                   c_void_p, POINTER(c_ulong)]
        _k32.UpdateProcThreadAttribute.restype = BOOL
        _k32.DeleteProcThreadAttributeList.argtypes = [c_void_p]
        _k32.DeleteProcThreadAttributeList.restype = None
        # Spawns the actual child (the shell/agent CLI), with the
        # pseudoconsole attribute from above attached via lpAttributeList.
        _k32.CreateProcessW.argtypes = [LPCWSTR, ctypes.c_wchar_p, c_void_p, c_void_p, BOOL,
                                        DWORD, c_void_p, LPCWSTR,
                                        POINTER(_STARTUPINFOEXW), POINTER(_PROCESS_INFORMATION)]
        _k32.CreateProcessW.restype = BOOL
        # Buffer arg must match the read buffer type. The reader uses a
        # (c_char * N) array, so the param is POINTER(c_char) -- LPBYTE
        # (POINTER(c_ubyte)) raises "expected LP_c_byte instance instead of
        # c_char_Array_N" on the first ReadFile and kills the reader thread.
        _k32.ReadFile.argtypes = [HANDLE, POINTER(c_char), DWORD, POINTER(DWORD), c_void_p]
        _k32.ReadFile.restype = BOOL
        # write() passes a `bytes` object; c_char_p accepts bytes directly.
        _k32.WriteFile.argtypes = [HANDLE, ctypes.c_char_p, DWORD, POINTER(DWORD), c_void_p]
        _k32.WriteFile.restype = BOOL
        # Process lifecycle/query group: exit status, opening a HANDLE from a
        # bare PID (used by _broker_process_matches to re-attach to a broker
        # process across a Sublime restart), killing, waiting, and closing
        # any HANDLE this file opens (pipes, process, thread, pseudoconsole).
        _k32.GetExitCodeProcess.argtypes = [HANDLE, POINTER(DWORD)]
        _k32.GetExitCodeProcess.restype = BOOL
        _k32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
        _k32.OpenProcess.restype = HANDLE
        _k32.TerminateProcess.argtypes = [HANDLE, DWORD]
        _k32.TerminateProcess.restype = BOOL
        _k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
        _k32.WaitForSingleObject.restype = DWORD
        _k32.CloseHandle.argtypes = [HANDLE]
        _k32.CloseHandle.restype = BOOL
        # Process heap group (heapapi.h): backs InitializeProcThreadAttributeList
        # above -- that call needs a caller-allocated buffer of a size only
        # knowable after a first sizing call, which is what this
        # GetProcessHeap/HeapAlloc/HeapFree trio provides (Python's own
        # allocator is not used here since the buffer must outlive the
        # ctypes call that sizes it and be freed with the matching Win32 API).
        _k32.GetProcessHeap.restype = ctypes.c_void_p
        _k32.HeapAlloc.argtypes = [ctypes.c_void_p, DWORD, c_ulong]
        _k32.HeapAlloc.restype = c_void_p
        _k32.HeapFree.argtypes = [ctypes.c_void_p, DWORD, c_void_p]
        _k32.HeapFree.restype = BOOL
        # Named-pipe client calls, used by _BrokerPty to connect to a
        # detachable session's agent_broker.py instead of owning a ConPTY
        # directly -- see _BrokerPty below.
        _k32.CreateFileW.argtypes = [LPCWSTR, DWORD, DWORD, c_void_p, DWORD, DWORD, HANDLE]
        _k32.CreateFileW.restype = HANDLE
        _k32.WaitNamedPipeW.argtypes = [LPCWSTR, DWORD]
        _k32.WaitNamedPipeW.restype = BOOL
        _k32.CancelIoEx.argtypes = [HANDLE, c_void_p]
        _k32.CancelIoEx.restype = BOOL
        # Used by _broker_process_matches() to tell a genuine broker process
        # apart from an unrelated process that later reused the same PID
        # (Windows recycles PIDs; a registry record for a broker that never
        # cleaned up after itself can otherwise look "alive" for years).
        _k32.QueryFullProcessImageNameW.argtypes = [
            HANDLE, DWORD, LPCWSTR, POINTER(DWORD),
        ]
        _k32.QueryFullProcessImageNameW.restype = BOOL
        _k32.GetProcessTimes.argtypes = [
            HANDLE, POINTER(FILETIME), POINTER(FILETIME),
            POINTER(FILETIME), POINTER(FILETIME),
        ]
        _k32.GetProcessTimes.restype = BOOL
        _INVALID_HANDLE_VALUE = HANDLE(-1).value

        _STILL_ACTIVE = 259
        _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        _INFINITE = 0xFFFFFFFF
        _PTY_OK = True
    except Exception as _e:  # pragma: no cover
        print(f"[ai_terminal] ctypes ConPTY binding failed: {_e}")
        _PTY_OK = False
else:
    # POSIX: the stdlib pty module backs _PosixPty, so no binding is needed.
    _PTY_OK = True


# ─── _Pty: ConPTY child process ───────────────────────────────────────────────


class _Pty:
    """A child process attached to a Windows pseudoconsole."""

    def __init__(self, argv, cwd, cols, rows, env):
        self.argv = list(argv)
        self.pid = 0
        self._hPC = None
        self._hInWrite = None      # we write input here
        self._hOutRead = None     # we read output here
        self._hProcess = None
        self._hThread = None
        self._attr_list = None
        self._heap_buf = None
        self._alive = True
        self._pc_lock = threading.Lock()
        self._exit_watcher = None
        # list2cmdline quotes paths with spaces; plain " ".join does not and
        # also cannot launch npm .cmd shims without prior _resolve_launch_argv.
        self._cmdline = subprocess.list2cmdline(self.argv)
        self._cwd = cwd or None
        self._env = env
        self._cols = cols
        self._rows = rows

    def start(self):
        """Create the ConPTY (two pipe pairs + CreatePseudoConsole) and hand
        the pty-side ends to _start_child to spawn the real process.
        Every raise path here closes whatever HANDLEs it already opened --
        see the comments at each step -- since a rejected spawn otherwise
        leaks them for the life of the Sublime process."""
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
        # restype=HRESULT: ctypes raises OSError itself on a failing result, so
        # the hr check below never ran and the raise leaked both pipe ends.
        try:
            hr = _k32.CreatePseudoConsole(_COORD(self._cols, self._rows),
                                          hPipePtyIn, hPipePtyOut, 0, byref(hPC))
        except OSError:
            _k32.CloseHandle(hPipePtyIn)
            _k32.CloseHandle(hPipePtyOut)
            _k32.CloseHandle(hInWrite)
            _k32.CloseHandle(hOutRead)
            raise
        # The pseudoconsole now holds its own copies of the pty-side pipe ends.
        _k32.CloseHandle(hPipePtyIn)
        _k32.CloseHandle(hPipePtyOut)
        if hr & 0x80000000:
            _k32.CloseHandle(hInWrite)
            _k32.CloseHandle(hOutRead)
            raise OSError(f"CreatePseudoConsole failed: HRESULT 0x{hr & 0xffffffff:08X}")
        self._hPC = hPC.value

        # Every failure past this point must undo the pseudoconsole and the pipe
        # ends we own, or a rejected spawn leaks them for the life of the host.
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
        pseudoconsole self._hPC via a proc-thread attribute list, the only
        mechanism CreateProcessW exposes for attaching a ConPTY."""
        # Build the proc-thread attribute list (double call: NULL -> size -> alloc -> call).
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
        # Force the child to take the pseudoconsole as its console rather than
        # inheriting our (redirected / console-less) std handles. Without this,
        # when the host process has no console (ST's plugin host, a piped parent),
        # the child inherits those null/redirected handles and isatty() is False
        # for every stream -- so claude falls back to --print and ollama refuses
        # the interactive picker. The PSEUDOCONSOLE attribute then overrides the
        # (null) hStd* handles with the pty console.
        si.StartupInfo.dwFlags |= _STARTF_USESTDHANDLES
        si.lpAttributeList = attr.value
        pi = _PROCESS_INFORMATION()
        cmd = ctypes.create_unicode_buffer(self._cmdline)
        cwd = ctypes.c_wchar_p(self._cwd) if self._cwd else None
        # Environment block (unicode, NUL-separated, double-NUL terminated).
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
        """Unblock the reader thread the moment the child exits on its own.

        ReadFile on hOutRead does not return EOF just because every process
        attached to the console exited -- conhost only flushes the final
        frame and closes the pipe once ClosePseudoConsole is called (kill()
        already relied on this, see its comment). kill() only calls it on an
        explicit tab-close, so a natural exit (typing `exit`/`/exit`, a
        crash) left the reader parked in ReadFile forever and the
        close-on-exit timer in _Terminal never fired. Waiting on the process
        handle here closes the pseudoconsole as soon as it exits, for either
        exit path.
        """
        h = self._hProcess
        if h is None:
            return
        try:
            _k32.WaitForSingleObject(h, _INFINITE)
        except OSError:
            # Losing the wait would park the reader in ReadFile forever, so still
            # close the pseudoconsole and say why the watcher gave up early.
            print("[ai_terminal] exit watcher failed:\n%s" % traceback.format_exc())
        self._close_pc()

    def _close_pc(self):
        """Close the pseudoconsole HANDLE (idempotent, lock-guarded so the
        exit watcher thread and an explicit kill() can't double-close)."""
        with self._pc_lock:
            if self._hPC is not None:
                _k32.ClosePseudoConsole(self._hPC)
                self._hPC = None

    def read(self, on_data):
        """Blocking reader loop; calls on_data(bytes) until EOF. Run on a daemon thread."""
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
        """Returns True iff ConPTY actually accepted the new size -- see
        _Terminal.resize, which must not treat a rejected resize as applied
        (a caller that did would silently skip a later identical-looking
        resize request forever, since its own last-known-size bookkeeping
        would already claim to be at the size ConPTY never actually took)."""
        if not self._alive or self._hPC is None:
            return False
        # restype=HRESULT makes ctypes raise on a failing result, so a bad
        # resize used to escape into the caller's layout watcher. Non-fatal:
        # the child keeps its old winsize, so full-screen TUIs draw at the
        # wrong width until a later resize succeeds -- report, don't propagate.
        try:
            hr = _k32.ResizePseudoConsole(self._hPC, _COORD(cols, rows))
        except OSError as e:
            print(f"[ai_terminal] ResizePseudoConsole({cols}, {rows}) failed: {e}")
            return False
        if hr & 0x80000000:
            print(
                "[ai_terminal] ResizePseudoConsole(%d, %d) failed: HRESULT 0x%08X"
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
        """Explicit tab-close teardown: close the pseudoconsole (drains the
        reader via EOF), force-terminate the child if it's still running,
        then release every HANDLE this instance owns."""
        if not self._alive:
            return
        self._alive = False
        # ClosePseudoConsole emits a final frame to hOutRead; the reader drains it
        # then sees EOF. Order matters -- see plan's ConPTY pitfalls. Routed
        # through _close_pc() so this can't race the exit watcher double-closing.
        self._close_pc()
        if self._hProcess is not None:
            _k32.TerminateProcess(self._hProcess, 0)
        self._close_handles()

    def _close_handles(self):
        """CloseHandle every pipe/process/thread HANDLE this instance owns
        (pseudoconsole itself is closed separately via _close_pc)."""
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


# ─── _BrokerPty: client of a standalone tools/agent_broker.py session ────────
#
# Same interface as _Pty (start/read/write/resize/is_alive/kill, .pid) so it's
# a drop-in replacement wherever _Pty is constructed. Instead of owning a
# ConPTY directly, it connects to a named pipe served by a separate
# agent_broker.py process. Sublime itself runs in a non-breakaway Windows job,
# so a direct CreateProcess child remains in that job even when passed
# CREATE_BREAKAWAY_FROM_JOB. The broker is therefore created by the Windows
# Task Scheduler service via a short-lived, immediately unregistered task in
# tools/spawn_outside_job.ps1. kill()
# here only disconnects this client -- the
# whole point of a detachable session is that closing the tab must NOT kill
# the agent; ending it for real goes through explicit_kill().

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_ERROR_PIPE_BUSY = 231
_ERROR_NO_DATA = 232
_ERROR_PIPE_NOT_CONNECTED = 233
_ERROR_FILE_NOT_FOUND = 2
_ERROR_OPERATION_ABORTED = 995
_DETACHED_PROCESS = 0x00000008
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_BROKER_REPLAY_END = b"\x1b]777;GhostShellReplayEnd\x07"
_BROKER_REPLAY_IDLE_S = 0.15


def _broker_registry_dir():
    """Durable discovery data owned by brokers, not Sublime's save cycle."""
    override = _settings_obj().get("broker_registry_dir")
    if override:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(override)))
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "GhostShell", "broker_sessions")


def _broker_registry_file(pipe_name):
    return os.path.join(_broker_registry_dir(), pipe_name + ".json")


def _read_broker_registry_record(pipe_name):
    """The registry JSON for a live pipe (child_argv, broker_pid,
    created_at, ...), or None if it's missing/unreadable -- e.g. the broker
    already exited and removed its own record."""
    try:
        with open(_broker_registry_file(pipe_name), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        print("[ai_terminal] read broker registry record failed:\n%s" % traceback.format_exc())
        return None


def _pid_is_alive(pid):
    """Whether a bare PID (no HANDLE of our own, e.g. one read back from the
    broker registry JSON after a restart) is still a live process. On
    Windows, OpenProcess by PID + GetExitCodeProcess (same pattern as
    _Pty.is_alive, but opening the HANDLE fresh instead of holding one);
    elsewhere, the POSIX os.kill(pid, 0) probe."""
    if os.name == "nt" and _k32 is not None:
        try:
            handle = _k32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid),
            )
        except (TypeError, ValueError):
            print("[ai_terminal] pid liveness check: OpenProcess failed:\n%s"
                  % traceback.format_exc())
            return False
        if not handle:
            return False
        try:
            code = DWORD(0)
            return bool(_k32.GetExitCodeProcess(handle, byref(code))) and (
                code.value == _STILL_ACTIVE
            )
        finally:
            _k32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        print("[ai_terminal] pid liveness check: os.kill probe failed:\n%s"
              % traceback.format_exc())
        return False


_EPOCH_AS_FILETIME = 116444736000000000  # 1601-01-01 -> 1970-01-01, in 100ns units


def _filetime_to_unix(ft):
    """Convert a Win32 FILETIME (ctypes.wintypes; 100ns ticks since
    1601-01-01, split across two 32-bit words) -- as returned by
    GetProcessTimes -- into a Unix epoch float, for comparing a broker's
    real process creation time against the registry's recorded created_at
    (see _broker_process_matches, the reason GetProcessTimes is called)."""
    value = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return (value - _EPOCH_AS_FILETIME) / 10000000.0


def _broker_process_matches(pid, created_at):
    """True if `pid` is not just alive, but plausibly *the* broker recorded
    at `created_at` -- not an unrelated process that later reused the PID.

    Windows recycles PIDs. A broker that crashed or was killed without
    reaching its own registry cleanup leaves a record behind forever; days
    later some completely different process can land on that same PID and
    _pid_is_alive() alone would call it live. Brokers always run as
    python.exe/pythonw.exe (see _resolve_broker_python / spawn path below),
    and a genuine broker's process start time sits within seconds of when it
    published its registry record -- so cross-check both before trusting the
    PID.
    """
    if not _pid_is_alive(pid):
        return False
    if os.name != "nt" or _k32 is None:
        return True
    try:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    except (TypeError, ValueError):
        print("[ai_terminal] broker process match: OpenProcess failed:\n%s"
              % traceback.format_exc())
        return False
    if not handle:
        return False
    try:
        size = DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if not _k32.QueryFullProcessImageNameW(handle, 0, buf, byref(size)):
            return False
        exe_name = os.path.basename(buf.value).lower()
        if exe_name not in ("python.exe", "pythonw.exe"):
            return False
        if created_at is None:
            return True
        creation = FILETIME()
        exit_t = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not _k32.GetProcessTimes(
            handle, byref(creation), byref(exit_t), byref(kernel), byref(user)
        ):
            # Image name already matched; treat as a pass rather than fail
            # recovery over a query we can't complete.
            return True
        started = _filetime_to_unix(creation)
        # Generous either-direction skew: clock differences, slow machines,
        # and the gap between process start and the registry write itself.
        return abs(started - float(created_at)) < 300
    finally:
        _k32.CloseHandle(handle)


# OpenProcess failure codes (winerror.h) that _broker_confirmed_dead tells
# apart: "no such process" is proof the broker is gone; "access denied" means
# a live process we may not inspect, so it is never treated as dead.
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87


def _broker_confirmed_dead(pid, created_at):
    """True only when the broker in a registry record is certainly gone.

    Stricter than `not _broker_process_matches(...)`, which also answers
    False for a live broker it merely failed to inspect. Used to delete a
    dead broker's leftover files, so any doubt means "not dead":
      - no such process (OpenProcess -> ERROR_INVALID_PARAMETER), or it has
        exited (GetExitCodeProcess != STILL_ACTIVE);
      - the PID now belongs to another program (not python.exe/pythonw.exe);
      - the PID was reused: that process started 5+ minutes away from the
        record's created_at (the same window _broker_process_matches uses).
    Access denied, a failed query, a missing PID, or a non-Windows host all
    answer False.
    """
    if os.name != "nt" or _k32 is None or pid is None:
        return False
    try:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    except (TypeError, ValueError):
        return False
    if not handle:
        return ctypes.get_last_error() == _ERROR_INVALID_PARAMETER
    try:
        code = DWORD(0)
        if not _k32.GetExitCodeProcess(handle, byref(code)):
            return False
        if code.value != _STILL_ACTIVE:
            return True
        size = DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if not _k32.QueryFullProcessImageNameW(handle, 0, buf, byref(size)):
            return False
        if os.path.basename(buf.value).lower() not in ("python.exe", "pythonw.exe"):
            return True
        if created_at is None:
            return False
        creation, exit_t, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not _k32.GetProcessTimes(
            handle, byref(creation), byref(exit_t), byref(kernel), byref(user)
        ):
            return False
        return abs(_filetime_to_unix(creation) - float(created_at)) >= 300
    finally:
        _k32.CloseHandle(handle)


def _remove_dead_broker_files(registry_path):
    """Delete a dead broker's registry record and its saved screen.

    A broker removes these itself in its own `finally` (tools/agent_broker.py
    _remove_registry). One ended from outside -- Task Manager, a crash --
    never gets there, and until 2026-09-22 nothing else removed them, so
    each such session left a record plus a 2 MB .scrollback file forever
    (found live: three from 09-18 and 09-21, two ended via Task Manager).
    Only called after _broker_confirmed_dead.
    """
    root, _ext = os.path.splitext(registry_path)
    for target in (registry_path, root + ".scrollback"):
        try:
            os.unlink(target)
            print("[ai_terminal] removed dead broker file %s" % target)
        except FileNotFoundError:
            pass
        except OSError:
            print("[ai_terminal] could not remove dead broker file %s:\n%s"
                  % (target, traceback.format_exc()))


def _registered_brokers(profile_name=None, cwd=None):
    """Return newest-first broker records matching a restored terminal."""
    folder = _broker_registry_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        print("[ai_terminal] registered brokers: listdir failed:\n%s" % traceback.format_exc())
        return []
    wanted_cwd = os.path.normcase(os.path.realpath(cwd)) if cwd else None
    found = []
    for name in names:
        if not name.startswith("ghostshell_") or not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
            pipe_name = record.get("pipe_name")
            if not pipe_name or pipe_name != name[:-5]:
                continue
            if not _broker_process_matches(
                record.get("broker_pid"), record.get("created_at")
            ):
                if _broker_confirmed_dead(
                    record.get("broker_pid"), record.get("created_at")
                ):
                    _remove_dead_broker_files(path)
                continue
            if profile_name and record.get("profile_name") != profile_name:
                continue
            record_cwd = record.get("cwd")
            if wanted_cwd and (not record_cwd or
                    os.path.normcase(os.path.realpath(record_cwd)) != wanted_cwd):
                continue
            found.append((os.path.getmtime(path), record))
        except (OSError, TypeError, ValueError):
            print("[ai_terminal] registered brokers: record %s unreadable:\n%s"
                  % (name, traceback.format_exc()))
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [record for _mtime, record in found]


def _same_view(a, b):
    """True if `a` and `b` are the same Sublime view, compared by view id
    rather than Python object identity. sublime.View is a lightweight
    proxy: consecutive calls like window.active_view() return a fresh
    Python object wrapping the same underlying view, so `a is b` is False
    even when they ARE the same tab (confirmed live 2026-09-15: window
    .active_view() called twice in a row returned two distinct objects
    with `is` False but `==`/id() True) -- `is` comparisons of views are
    always wrong and silently mislabel the active tab as some other one.
    """
    if a is None or b is None:
        return False
    return a.id() == b.id()


def _broker_pipe_path(name):
    return "\\\\.\\pipe\\" + name


def _pipe_instance_free(pipe_name, timeout_ms=150):
    """True if the broker's output pipe has no client attached right now.

    A broker being alive (per the on-disk registry) does not mean its pipe
    is available -- another client (e.g. a Windows Terminal hand-off) may
    already hold the one connection slot. WaitNamedPipeW succeeds
    immediately when an instance is free to accept; ERROR_FILE_NOT_FOUND
    means the pipe doesn't exist yet (broker mid-restart), a transient
    state treated as "try anyway" like _try_connect above does. Any other
    outcome means busy. Caller must not run this on the main thread --
    it can block up to timeout_ms.
    """
    if not _PTY_OK or os.name != "nt":
        return True
    if _k32.WaitNamedPipeW(_broker_pipe_path(pipe_name), timeout_ms):
        return True
    return ctypes.get_last_error() == _ERROR_FILE_NOT_FOUND


def _send_broker_ctl_line(pipe_name, line, timeout_s=2.0):
    """Send one line to a broker's control pipe by name alone, with no live
    _BrokerPty object required.

    Used by the reattach-from-Windows-Terminal flow: the session may be
    orphaned (no ST view/Terminal at all right now) or held by a WT relay,
    neither of which has a _BrokerPty instance to call explicit_kill()'s
    connect-and-write pattern on -- this is that same pattern, standalone.
    Returns True only if the full line was actually written.
    """
    if not _PTY_OK or os.name != "nt":
        return False
    path = _broker_pipe_path(pipe_name + "-ctl")
    deadline = time.time() + timeout_s
    while True:
        h = _k32.CreateFileW(path, _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None)
        if h != _INVALID_HANDLE_VALUE:
            break
        err = ctypes.get_last_error()
        if time.time() > deadline:
            return False
        if err == _ERROR_PIPE_BUSY:
            _k32.WaitNamedPipeW(path, 500)
        elif err == _ERROR_FILE_NOT_FOUND:
            time.sleep(0.1)
        else:
            return False
    try:
        data = line if isinstance(line, bytes) else line.encode("utf-8")
        written = DWORD(0)
        return bool(
            _k32.WriteFile(h, data, len(data), byref(written), None)
            and written.value == len(data)
        )
    finally:
        _k32.CloseHandle(h)


def _broker_script_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "agent_broker.py")


def _recover_console_script_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "recover_console.py")


def _windows_terminal_exe():
    """Locate wt.exe. Override with the `windows_terminal_exe` setting if
    auto-detect picks the wrong install (e.g. Store vs. non-Store wt)."""
    override = _settings_obj().get("windows_terminal_exe")
    if override:
        return override
    return shutil.which("wt.exe") or shutil.which("wt")


def _broker_python_exe():
    """Locate a real Windows Python to run tools/agent_broker.py -- sys.executable
    inside the ST plugin host resolves to sublime_text.exe, not a usable
    interpreter. Override with the `broker_python` setting if auto-detect
    picks the wrong one (e.g. multiple Pythons on PATH)."""
    override = _settings_obj().get("broker_python")
    if override:
        return override
    for name in ("python.exe", "python3.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


class _BrokerPty:
    """Client of a tools/agent_broker.py session over a named pipe.

    start() first tries to connect to an already-running broker on
    --pipe-name; only if that fails -- and only if allow_spawn is true --
    does it spawn a new detached broker and retry. allow_spawn=False makes
    a failed connect raise instead of silently starting an unrelated new
    session under an old tab's identity; that's what the ST-restart reattach
    path (_reattach_broker_view) wants, since the old broker being gone is a
    real failure to report, not a cue to spawn a same-named replacement.
    """

    def __init__(self, pipe_name, argv, cwd, cols, rows, env, allow_spawn=True,
                 scrollback_bytes=2 * 1024 * 1024, profile_name=None):
        self.pipe_name = pipe_name
        self.argv = list(argv)
        self.pid = 0
        self._cwd = cwd or None
        self._env = env
        self._cols = cols
        self._rows = rows
        self._allow_spawn = allow_spawn
        self._scrollback_bytes = scrollback_bytes
        self._profile_name = profile_name
        self._h_out = None
        self._h_in = None
        self._h_ctl = None
        self._alive = False
        self._io_lock = threading.Lock()

    def _try_connect(self, path, access, timeout_s):
        """Retry CreateFileW against a named pipe until it opens or
        timeout_s elapses, distinguishing "pipe exists but busy" (wait on
        it via WaitNamedPipeW) from "pipe doesn't exist yet" (poll instead --
        WaitNamedPipeW does not wait for first creation, per MSDN)."""
        deadline = time.time() + timeout_s
        while True:
            h = _k32.CreateFileW(path, access, 0, None, _OPEN_EXISTING, 0, None)
            if h != _INVALID_HANDLE_VALUE:
                return h
            err = ctypes.get_last_error()
            if time.time() > deadline:
                return None
            if err == _ERROR_PIPE_BUSY:
                # An instance exists but is taken -- WaitNamedPipeW actually
                # blocks until one frees up (or this timeout).
                _k32.WaitNamedPipeW(path, 500)
            elif err == _ERROR_FILE_NOT_FOUND:
                # The pipe doesn't exist yet (broker still starting up).
                # WaitNamedPipeW returns immediately in this case per MSDN --
                # it does not wait for first creation -- so poll instead.
                time.sleep(0.1)
            else:
                return None

    def start(self):
        # Two separate unidirectional pipes (<name> broker-writes/we-read,
        # <name>-in we-write/broker-reads) -- NOT one duplex pipe. A duplex
        # version had this client's reader thread blocked in ReadFile while
        # the broker's writer thread did WriteFile on the SAME handle;
        # confirmed live that the WriteFile can simply never complete (no
        # error, just never returns) under a fast burst, silently killing
        # the session after just its first few bytes. Splitting the
        # directions removes the shared handle entirely.
        out_path = _broker_pipe_path(self.pipe_name)
        in_path = _broker_pipe_path(self.pipe_name + "-in")

        # allow_spawn=False (reattach) has no fallback if this first attempt
        # gives up too soon, so it gets the full 10s instead of the fresh-open
        # path's fast 0.3s fail-through-to-spawn.
        initial_timeout = 0.3 if self._allow_spawn else 10.0
        h_out = self._try_connect(out_path, _GENERIC_READ, initial_timeout)
        if h_out is None:
            if not self._allow_spawn:
                raise OSError(
                    f"no broker answering on pipe {self.pipe_name!r} -- "
                    f"the detachable session is gone, not reattaching"
                )
            self._spawn_broker()
            h_out = self._try_connect(out_path, _GENERIC_READ, 10.0)
            if h_out is None:
                raise OSError(
                    f"could not connect to agent_broker.py on pipe {self.pipe_name!r} "
                    f"after spawning it"
                )
            print(f"[ai_terminal] spawned new detachable session on pipe {self.pipe_name!r}")
        else:
            print(f"[ai_terminal] reattached to existing detachable session on pipe {self.pipe_name!r}")
        self._h_out = h_out

        h_in = self._try_connect(in_path, _GENERIC_WRITE, 5.0)
        if h_in is None:
            _k32.CloseHandle(h_out)
            self._h_out = None
            raise OSError(f"could not connect to input pipe for {self.pipe_name!r}")
        self._h_in = h_in
        self._alive = True

        # Control pipe connect is best-effort -- a session that survived a
        # restart is already sized however it last was; a failed connect here
        # just means resize() silently no-ops until the next reattach.
        ctl = self._try_connect(_broker_pipe_path(self.pipe_name + "-ctl"), _GENERIC_WRITE, 2.0)
        self._h_ctl = ctl
        if ctl is not None:
            self.resize(self._cols, self._rows)

    def _spawn_broker(self):
        from .terminal.log_paths import log_root
        python_exe = _broker_python_exe()
        if not python_exe:
            raise OSError(
                "no Python interpreter found to run tools/agent_broker.py -- "
                "install Python and ensure it's on PATH, or set the "
                "`broker_python` setting to its full path"
            )
        # The broker has no interactive stdio (it redirects diagnostics to
        # its lifecycle log before printing) and owns a ConPTY for the child.
        # python.exe would create a visible console when Task Scheduler starts
        # it; use the windowless sibling when available.
        broker_exe = python_exe
        if os.path.basename(python_exe).lower() == "python.exe":
            candidate = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
            if os.path.isfile(candidate):
                broker_exe = candidate
        broker_argv = [
            "--pipe-name", self.pipe_name,
            "--cols", str(self._cols), "--rows", str(self._rows),
            "--scrollback-bytes", str(self._scrollback_bytes),
            "--registry-file", _broker_registry_file(self.pipe_name),
            "--log-file", os.path.join(log_root(), "agent_broker.log"),
        ]
        if self._profile_name:
            broker_argv += ["--profile-name", self._profile_name]
        if self._cwd:
            broker_argv += ["--cwd", self._cwd]
        launch_file = _broker_registry_file(self.pipe_name) + ".launch"
        os.makedirs(os.path.dirname(launch_file), exist_ok=True)
        fd = os.open(launch_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({
                    "broker_argv": broker_argv,
                    "child_argv": self.argv,
                    "environment": self._env,
                }, handle)
        except (OSError, TypeError, ValueError):
            try:
                os.unlink(launch_file)
            except OSError:
                print("[ai_terminal] broker launch: could not remove launch file %s:\n%s"
                      % (launch_file, traceback.format_exc()))
            raise
        # Keep every broker option, child argument, and environment value in
        # the one-use launch file; only its randomized path appears in process
        # listings or the temporary scheduled-task action.
        cmd = [broker_exe, _broker_script_path(), "--launch-file", launch_file]
        launcher = os.path.join(os.path.dirname(__file__), "tools", "spawn_outside_job.ps1")
        powershell = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
        )
        payload = json.dumps({
            "executable": cmd[0],
            "arguments": subprocess.list2cmdline(cmd[1:]),
            "cwd": self._cwd or os.getcwd(),
            "task_name": "GhostShell Broker " + self.pipe_name,
            "launch_file": launch_file,
        })
        helper = subprocess.Popen(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", launcher],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", creationflags=0x08000000,
        )
        try:
            output, error = helper.communicate(payload, timeout=15.0)
        except (subprocess.SubprocessError, OSError):
            try:
                os.unlink(launch_file)
            except OSError:
                print("[ai_terminal] broker launch: could not remove launch file %s:\n%s"
                      % (launch_file, traceback.format_exc()))
            raise
        if helper.returncode != 0:
            try:
                os.unlink(launch_file)
            except OSError:
                print("[ai_terminal] broker launch: could not remove launch file %s:\n%s"
                      % (launch_file, traceback.format_exc()))
            raise OSError(
                "could not launch detachable broker through Task Scheduler: %s"
                % ((error or output or "unknown WMI launcher error").strip(),)
            )
        try:
            result = json.loads(output)
            if int(result.get("return_value", -1)) != 0:
                raise ValueError("WMI return value %r" % result.get("return_value"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            try:
                os.unlink(launch_file)
            except OSError:
                print("[ai_terminal] broker launch: could not remove launch file %s:\n%s"
                      % (launch_file, traceback.format_exc()))
            raise OSError("invalid scheduled broker-launch response: %r (%s)" % (output, exc))

    def read(self, on_data, on_replay_complete=None):
        """Blocking ReadFile loop on the broker's output pipe (run on a
        daemon thread, like _Pty.read). The bulk of this method's own logic
        is not ctypes: it is scanning the byte stream for _BROKER_REPLAY_END
        so a reattach's buffered replay bytes can be told apart from live
        output and on_replay_complete fired exactly once at the boundary."""
        buf = (c_char * 8192)()
        n = DWORD(0)
        if on_replay_complete is None:
            on_replay_complete = lambda: None
        marker_tail = bytearray()
        replay_done = False
        stream_lock = threading.RLock()
        idle_timer = [None]

        def finish_replay(flush_tail=True):
            nonlocal replay_done
            with stream_lock:
                if replay_done:
                    return
                if flush_tail and marker_tail:
                    on_data(bytes(marker_tail))
                    marker_tail.clear()
                replay_done = True
                timer = idle_timer[0]
                idle_timer[0] = None
                if timer is not None and timer is not threading.current_thread():
                    timer.cancel()
                on_replay_complete()

        def arm_legacy_idle_boundary():
            if replay_done:
                return
            timer = threading.Timer(_BROKER_REPLAY_IDLE_S, finish_replay)
            timer.daemon = True
            with stream_lock:
                old = idle_timer[0]
                idle_timer[0] = timer
            if old is not None:
                old.cancel()
            timer.start()

        while self._alive:
            ok = _k32.ReadFile(self._h_out, buf, 8192, byref(n), None)
            if not ok:
                err = ctypes.get_last_error()
                self._alive = False
                if err in (0, _ERROR_HANDLE_EOF, _ERROR_BROKEN_PIPE, _ERROR_NO_DATA,
                           _ERROR_PIPE_NOT_CONNECTED, _ERROR_OPERATION_ABORTED):
                    break
                raise OSError("ReadFile on broker output pipe failed (GetLastError %d)" % err)
            if n.value == 0:
                break
            data = bytes(buf[: n.value])
            if replay_done:
                on_data(data)
                continue
            with stream_lock:
                marker_tail.extend(data)
                marker_at = marker_tail.find(_BROKER_REPLAY_END)
                if marker_at >= 0:
                    before = bytes(marker_tail[:marker_at])
                    after = bytes(marker_tail[marker_at + len(_BROKER_REPLAY_END):])
                    marker_tail.clear()
                    if before:
                        on_data(before)
                    finish_replay(flush_tail=False)
                    if after:
                        on_data(after)
                    continue
                safe = len(marker_tail) - len(_BROKER_REPLAY_END) + 1
                if safe > 0:
                    on_data(bytes(marker_tail[:safe]))
                    del marker_tail[:safe]
            arm_legacy_idle_boundary()
        finish_replay()
        self._alive = False

    def write(self, data):
        """Blocking WriteFile to the broker's input pipe, looping until every
        byte of data is accepted -- same pattern as _Pty.write."""
        if not self._alive or self._h_in is None:
            return
        written = DWORD(0)
        while data:
            if not _k32.WriteFile(self._h_in, data, len(data), byref(written), None):
                raise OSError("WriteFile to broker input pipe failed (GetLastError %d)"
                              % ctypes.get_last_error())
            if not written.value:
                raise OSError("WriteFile accepted 0 of %d bytes" % len(data))
            data = data[written.value:]

    def resize(self, cols, rows):
        """Send a text "RESIZE cols rows\\n" line over the separate control
        pipe (self._h_ctl) rather than a ConPTY call -- the broker, not this
        client, owns the real ConPTY; a client with no control-pipe
        connection (start()'s best-effort connect failed) silently no-ops."""
        self._cols, self._rows = cols, rows
        if self._h_ctl is None:
            return False
        line = ("RESIZE %d %d\n" % (cols, rows)).encode("utf-8")
        written = DWORD(0)
        with self._io_lock:
            ok = _k32.WriteFile(self._h_ctl, line, len(line), byref(written), None)
        return bool(ok)

    def is_alive(self):
        """No ctypes here -- unlike _Pty.is_alive, this client has no HANDLE
        to a process to query (the broker owns the child); _alive only
        reflects this pipe connection's own state, flipped False by read()
        on EOF/error or by kill()."""
        return self._alive

    def kill(self):
        """Disconnect this client only. The broker and its agent process keep
        running -- that's the point of a detachable session. See
        explicit_kill() to actually end the session."""
        # Do not return early when _alive is already false.  A natural child
        # exit makes read() clear that flag before the view's on_close path
        # calls kill(); the pipe handles still belong to this client and must
        # be closed.  Leaving them open kept the broker side connected after
        # Claude's /exit and made the clean EOF look like a pipe failure.
        self._alive = False
        handles = (self._h_out, self._h_in, self._h_ctl)
        self._h_out = self._h_in = self._h_ctl = None
        for h in handles:
            if h is not None:
                # CancelIoEx unblocks the reader thread's pending ReadFile on
                # this handle from here (a different thread). Without this,
                # CloseHandle can hang the calling thread indefinitely if a
                # blocking ReadFile on the same handle is still pending on
                # another thread -- reproduced live: froze Sublime's main
                # thread solid when called there directly.
                _k32.CancelIoEx(h, None)
                _k32.CloseHandle(h)

    def explicit_kill(self):
        """End the underlying agent/shell for real (not just disconnect)."""
        line = b"KILL\n"
        with self._io_lock:
            h = self._h_ctl
            sent = False
            if h is not None:
                written = DWORD(0)
                sent = bool(
                    _k32.WriteFile(h, line, len(line), byref(written), None)
                    and written.value == len(line)
                )
                if not sent:
                    # A broker can replace its control-pipe instance after a
                    # disconnect.  Do not silently turn a requested session
                    # end into a detach merely because our cached handle went
                    # stale; reconnect once and deliver KILL to the live pipe.
                    _k32.CloseHandle(h)
                    self._h_ctl = None
            if not sent:
                h = self._try_connect(
                    _broker_pipe_path(self.pipe_name + "-ctl"),
                    _GENERIC_WRITE,
                    1.0,
                )
                if h is not None:
                    written = DWORD(0)
                    sent = bool(
                        _k32.WriteFile(h, line, len(line), byref(written), None)
                        and written.value == len(line)
                    )
                    _k32.CloseHandle(h)
        self.kill()


def _is_broker_pty(pty):
    """Recognize detachable PTYs across Sublime plugin hot reloads.

    Live _Terminal objects are upgraded to the newly loaded class generation,
    but their existing _BrokerPty instance keeps its old class identity.
    An isinstance-only check therefore silently skips tab-close KILL for every
    broker tab that was open while this module reloaded.
    """
    return isinstance(pty, _BrokerPty) or (
        getattr(getattr(pty, "__class__", None), "__name__", None) == "_BrokerPty"
        and bool(getattr(pty, "pipe_name", None))
        and callable(getattr(pty, "explicit_kill", None))
    )


def _tune_profile_panel_html(term):
    """Minihtml body for the expanded in-tab profile-settings panel (the
    "Settings" toolbar link's on-state). Each row is a clickable checkbox
    glyph -- minihtml has no real <input>, so a checked/unchecked glyph
    doubles as the toggle link, same trick the toolbar itself uses for
    edit_mode. Clicking writes through immediately via
    AiTerminalTuneProfileSetCommand (href "tp_set:<key>") -- no separate
    Save/Cancel, per explicit instruction: this panel auto-saves on click.
    """
    profile_name = term.profile_name
    current = _profile_settings(profile_name) or {}

    def _row(key, desc, default, respawns):
        value = bool(current.get(key, default))
        glyph = "☑" if value else "☐"  # ☑ / ☐
        suffix = (
            ' <span style="font-style:italic;">(respawns tab)</span>'
            if respawns else ""
        )
        return (
            '<a href="tp_set:%s">%s %s</a>%s<br>'
            '<span style="padding-left:1.4em;">%s</span>'
            % (key, glyph, key, suffix, desc)
        )

    rows = []
    for key, desc, default in _LIVE_TUNABLE_PROFILE_KEYS:
        rows.append(_row(key, desc, default, respawns=False))
    for key, desc, default in _REATTACH_TUNABLE_PROFILE_KEYS:
        rows.append(_row(key, desc, default, respawns=True))

    return (
        "<br>&mdash; %s profile settings &mdash;<br>" % profile_name
        + "<br>".join(rows)
    )


def _close_menu_panel_html():
    """Minihtml body for the "Tab Close" toolbar link's expanded state --
    the three differently-destructive session-ending actions, revealed
    behind one extra click rather than sitting bare in the always-visible
    toolbar row (see the reasoning above _add_close_toolbar's `items`
    list). Captions match the existing Tab Context menu / Command Palette
    entries for these same commands exactly, so there is only one
    vocabulary to learn regardless of which path a user finds first.
    """
    rows = [
        ("close_keep_alive", "Close Tab (Keep Session Alive)",
         "detaches -- keeps running in the background, recoverable via "
         "'Ai: Recover Session...'"),
        ("kill_session", "Kill Session (Keep Tab Open)",
         "ends the process now; the tab and its transcript stay open to read"),
        ("end_session", "End Session (Kill + Close)",
         "ends the process now and closes the tab"),
    ]
    return (
        "<br>&mdash; end or detach this session &mdash;<br>"
        + "<br>".join(
            '<a href="%s">%s</a><br>'
            '<span style="padding-left:1.4em; font-style:italic;">%s</span>'
            % (href, caption, desc)
            for href, caption, desc in rows
        )
    )


def _add_close_toolbar(term):
    """(Re-)anchor the persistent Close Tab/Relaunch/Windows-Terminal toolbar
    to term's view, at the buffer's current end. See
    _CLOSE_TOOLBAR_PHANTOM_KEY. No-op for non-broker sessions (nothing to
    detach or relaunch) or an already-invalid view.

    Called from _do_render after every applied frame (outside term._lock),
    not just once at spawn/reattach/migrate. A short-lived test (2026-09-09)
    made it look like a phantom's zero-width anchor tracks the buffer's
    growing end through every full-buffer view.replace() on its own; a real,
    longer-running session disproved that -- it went stale mid-buffer once
    enough new content had been appended after it. Erasing + re-adding here
    every frame is cheap next to the render it follows and keeps it honest.
    """
    view = term.view
    if not view or not view.is_valid() or not _is_broker_pty(term.pty):
        return
    view.erase_phantoms(_CLOSE_TOOLBAR_PHANTOM_KEY)

    def _on_navigate(href):
        v = term.view
        if not v or not v.is_valid():
            return
        w = v.window()
        if w is None:
            return
        if href == "wt":
            v.run_command("ai_terminal_open_in_windows_terminal")
            return
        if href == "edit_mode":
            v.run_command("ai_terminal_toggle_copy_mode")
            t = _Terminal.from_id(v.id())
            if t is not None:
                _add_close_toolbar(t)
            return
        if href == "tune_profile":
            v.run_command("ai_terminal_toggle_tune_profile_panel")
            return
        if href.startswith("tp_set:"):
            v.run_command(
                "ai_terminal_tune_profile_set", {"key": href[len("tp_set:"):]}
            )
            return
        if href == "close_menu":
            t = _Terminal.from_id(v.id())
            if t is not None:
                t.close_menu_open = not getattr(t, "close_menu_open", False)
                _add_close_toolbar(t)
            return
        group, index = w.get_view_index(v)
        if href == "relaunch":
            w.run_command("ai_terminal_relaunch", {"group": group, "index": index})
        elif href == "info":
            w.run_command("ai_terminal_session_info", {"group": group, "index": index})
        elif href in ("close_keep_alive", "kill_session", "end_session"):
            # Collapse the reveal before acting -- the action is already
            # underway (or the tab/session is already gone) by the time it
            # would matter, so there is nothing left to keep showing a
            # confirm-style sub-menu for.
            t = _Terminal.from_id(v.id())
            if t is not None:
                t.close_menu_open = False
            w.run_command(
                {
                    "close_keep_alive": "ai_terminal_close_keep_alive",
                    "kill_session": "ai_terminal_kill_session",
                    "end_session": "ai_terminal_end_session",
                }[href],
                {"group": group, "index": index},
            )

    # Plain text, no leading symbol: every dingbat/emoji tried (⏹ ⏏ ↗ ↻ 🔄)
    # had some glyph-support quirk in minihtml -- a tofu box, dim/inconsistent
    # weight, or a swallowed space before the following word. Confirmed live
    # (2026-09-09).
    #
    # Hard-wrapped ourselves at the view's known column count, the same way
    # the PTY apps this hosts wrap their own output -- their wrapping is real
    # newline characters written for a known terminal width, not Sublime
    # soft-wrap (word_wrap is off on this view, required so the PTY grid
    # itself doesn't reflow). A phantom is not buffer text, so it gets none
    # of that hard-wrapping for free; confirmed live (2026-09-09) that a
    # single unwrapped row just overflows sideways instead, with later items
    # clipped off-screen and reachable only by horizontal scroll.
    # No standalone Kill Session or Close Tab buttons: a real kill is already
    # reachable via '/exit' inside the agent itself, and closing the tab
    # (which detaches, not kills, by default -- see
    # AiTerminalViewListener.on_close) is already exactly what the native
    # tab 'X' does -- confirmed live (2026-09-09) they're identical, so a
    # toolbar shortcut for it is redundant. Kill Session/End Session/Close
    # Tab (Keep Session Alive) still exist as explicit commands (Tab Context
    # menu, Command Palette) for discoverability, and deliberately stay
    # menu-only: they sit right next to each other with similarly-worded,
    # differently-destructive captions, and the menu's extra travel
    # distance (right-click -> hover -> click) is real accident protection,
    # not just slowness -- a toolbar shortcut would trade a rare
    # inconvenience for a real, recurring mis-click risk (2026-09-10).
    # Session Info is the safe, read-only, frequent exception: it answers
    # "is this still doing something" with zero risk, so it gets the
    # one-click toolbar treatment those three intentionally don't.
    #
    # Text Edit Mode toggle: a persistent MODE, not a held modifier key, and
    # deliberately so. Shift is already claimed by ST's own keyboard-driven
    # selection-extend (shift+arrow) everywhere, including in this view's
    # own keymap context gate below -- overloading it as a mouse escape
    # hatch back to normal ST selection would collide with that meaning,
    # not add a new one. A toggle is the only strategy that doesn't fight
    # existing ST semantics. This is the existing `term.copy_mode` /
    # ai_terminal_toggle_copy_mode command (ctrl+alt+c) -- previously
    # keyboard-only with no visible affordance, surfaced here as a toolbar
    # item because the user needs to see/reach it without memorizing a
    # chord. "Text Edit Mode" is the user-facing name; the internal
    # `copy_mode` attribute/command name is unchanged (rename is a larger,
    # separate refactor if ever wanted).
    edit_mode_label = (
        "Text Edit Mode: On" if getattr(term, "copy_mode", False) else "Text Edit Mode: Off"
    )

    # (href, display text, html text) -- kept separate in case a future label
    # needs HTML entities again; none of these currently do.
    items = [
        ("edit_mode", edit_mode_label, edit_mode_label),
        ("relaunch", "Relaunch Agent", "Relaunch Agent"),
        ("wt", "Move to Windows Terminal", "Move to Windows Terminal"),
        ("info", "Session Info", "Session Info"),
    ]
    if term.profile_name:
        settings_label = (
            "Settings ▲" if getattr(term, "tune_profile_open", False)
            else "Settings ▼"
        )
        items.append(("tune_profile", settings_label, settings_label))
    # Tab Close reveal: since 2026-09-15 the native window-close commands
    # (Ctrl+W, File > Close File, Ctrl+F4, right-click Close Tab) are
    # blocked outright for ai_terminal tabs -- see
    # AiTerminalTabCloseInterceptor -- so this is the one place left to
    # actually end or detach a session without going to Tab Context menu /
    # Command Palette. Kill Session/End Session/Close Tab (Keep Session
    # Alive) still don't get one-click toolbar buttons of their own
    # (2026-09-10's mis-click reasoning above still holds for three
    # differently-destructive actions sitting bare in a row) -- gated
    # behind this one extra reveal click instead, which is the toolbar
    # equivalent of the menu's right-click travel distance, just visible
    # and discoverable instead of hidden.
    close_menu_label = (
        "Tab Close ▲" if getattr(term, "close_menu_open", False) else "Tab Close ▼"
    )
    items.append(("close_menu", close_menu_label, close_menu_label))
    sep = "   |   "
    try:
        cols, _rows = _measure(view, profile_name=term.profile_name)
    except Exception:
        cols = 80
    # Small safety margin: the phantom's own padding, and any rounding
    # difference between minihtml's monospace metrics and the terminal
    # font's, both eat into the raw column count.
    # The toolbar's monospace text is narrower than the terminal font (measured live
    # 2026-09-21: about 5.6 px against 7.03 px per character), so the toolbar fits more
    # characters per row than the terminal has columns.
    cols = max(20, int((cols - 4) * _setting_number(
        "toolbar_width_factor", 1.25, cast=float, profile_name=term.profile_name
    )))
    lines, cur, cur_len = [], [], 0
    for href, display, markup in items:
        add_len = len(display) + (len(sep) if cur else 0)
        if cur and cur_len + add_len > cols:
            lines.append(cur)
            cur, cur_len, add_len = [], 0, len(display)
        cur.append((href, markup))
        cur_len += add_len
    if cur:
        lines.append(cur)
    row_html = [
        sep.join('<a href="%s">%s</a>' % (href, markup) for href, markup in line)
        for line in lines
    ]
    panel_html = ""
    if term.profile_name and getattr(term, "tune_profile_open", False):
        panel_html = _tune_profile_panel_html(term)
    if getattr(term, "close_menu_open", False):
        panel_html += _close_menu_panel_html()
    # font-family: monospace so a character actually is the width _measure's
    # cols count assumed -- minihtml's default UI font isn't monospace, and
    # the cols-based line-wrap above is only valid if characters here are the
    # same width as the terminal grid's.
    html = (
        '<body style="padding:2px 8px; font-family:monospace;">'
        '<style>a{text-decoration:none;}</style>'
        + "<br>".join(row_html)
        + panel_html
        + "</body>"
    )
    size = view.size()
    view.add_phantom(
        _CLOSE_TOOLBAR_PHANTOM_KEY, sublime.Region(size, size), html,
        sublime.LAYOUT_BLOCK, _on_navigate,
    )


# ─── _PosixPty: forkpty child process (Linux/WSL/macOS) ──────────────────────


class _PosixPty:
    """A child process attached to a Unix pseudoterminal.

    Mirrors the _Pty interface (start/read/write/resize/is_alive/kill +
    argv/pid) using the stdlib pty, fcntl and termios modules, so the
    Sublime adapter above needs no platform branches beyond backend
    selection.
    """

    def __init__(self, argv, cwd, cols, rows, env):
        self.argv = list(argv)
        self.pid = 0
        self._cwd = cwd or None
        self._env = env
        self._cols = cols
        self._rows = rows
        self._fd = -1
        self._alive = True

    def start(self):
        import fcntl
        import pty
        import struct
        import termios

        pid, fd = pty.fork()
        if pid == 0:
            try:
                if self._cwd:
                    os.chdir(self._cwd)
                env = {str(k): str(v) for k, v in (self._env or os.environ).items()}
                os.execvpe(self.argv[0], self.argv, env)
            except BaseException as e:
                # The parent cannot see an exception raised in the forked child,
                # so write the reason onto the pty: otherwise a bad cwd or a
                # missing executable shows up as a blank tab that exits at once.
                try:
                    os.write(2, ("ai_terminal: could not exec %s: %s\r\n"
                                 % (self.argv[0], e)).encode("utf-8", "replace"))
                except OSError:
                    pass
                os._exit(127)
        self.pid = pid
        self._fd = fd
        try:
            fcntl.ioctl(
                self._fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", self._rows, self._cols, 0, 0),
            )
        except OSError as e:
            # Non-fatal: the child starts at the pty default size, so full-screen
            # TUIs draw at the wrong width until the next resize.
            print(f"[ai_terminal] posix pty initial resize failed: {e}")

    def read(self, on_data):
        """Blocking reader loop; calls on_data(bytes) until EOF. Run on a daemon thread."""
        while self._alive and self._fd >= 0:
            try:
                data = os.read(self._fd, 8192)
            except OSError as e:
                self._alive = False
                # EIO on the master side is how Linux reports the child closing
                # the slave: that is a normal exit, anything else is a failure
                # the caller must not mistake for one.
                if e.errno == errno.EIO:
                    return
                raise
            if not data:
                break
            on_data(data)
        self._alive = False

    def write(self, data):
        if not self._alive or self._fd < 0:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        while data:
            n = os.write(self._fd, data)
            data = data[n:]

    def resize(self, cols, rows):
        """Returns True iff TIOCSWINSZ actually succeeded -- see
        _Pty.resize's docstring and _Terminal.resize for why this must not
        be treated as applied on failure."""
        if not self._alive or self._fd < 0:
            return False
        try:
            import fcntl
            import struct
            import termios

            fcntl.ioctl(
                self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
            )
        except OSError as e:
            print(f"[ai_terminal] posix pty resize({cols}, {rows}) failed: {e}")
            return False
        self._cols, self._rows = cols, rows
        return True

    def is_alive(self):
        if not self._alive or not self.pid:
            return False
        try:
            wpid, _ = os.waitpid(self.pid, os.WNOHANG)
            if wpid == self.pid:
                self._alive = False
        except OSError:
            self._alive = False
        return self._alive

    def kill(self):
        if not self._alive:
            return
        self._alive = False
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGHUP)
            except OSError:
                print("[ai_terminal] posix pty kill: SIGHUP failed:\n%s" % traceback.format_exc())
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                print("[ai_terminal] posix pty kill: waitpid failed:\n%s" % traceback.format_exc())
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                print("[ai_terminal] posix pty kill: close fd failed:\n%s" % traceback.format_exc())
            self._fd = -1


# ─── pure terminal core (testable without Sublime) ───────────────────────────
# Screen, Parser, colours, keys, and text layout live in terminal/*.
# This file is the Sublime adapter: ConPTY, view I/O, commands, color-scheme.
# Session recordings (cast, text transcript, debug logs) live in terminal/.

try:
    from .terminal.colors import (
        quantize256 as _quantize256,
        pack_attr as _attr,
        xterm256_rgb as _xterm256_rgb,
        XTERM256_RGB as _XTERM256_RGB,
        FG_SHIFT as _FG_SHIFT,
        BG_SHIFT as _BG_SHIFT,
        ATTR_FG_MASK as _ATTR_FG_MASK,
        ATTR_BG_MASK as _ATTR_BG_MASK,
        BOLD as _BOLD,
        REVERSE as _REVERSE,
        FAINT as _FAINT,
        BG_LUMA_THRESHOLD as _BG_LUMA_THRESHOLD,
        ANSI16_HEX as _ANSI16_HEX,
        xterm_hex as _xterm_hex,
        HEX as _HEX,
        scope_name_for as _scope_name_for,
        font_style_for as _font_style_for,
        rstrip_cells as _rstrip_cells,
        scheme_colors_for as _scheme_colors_for,
        ensure_contrast as _ensure_contrast,
        DEFAULT_FG_HEX as _DEFAULT_FG_HEX,
        BG_NEAR_BLACK as _BG_NEAR_BLACK,
        _ANSI16_RGB,
    )
    from .terminal.screen import Screen as _Screen, BLANK as _BLANK
    from .terminal.ghostty_engine import GhosttyParser as _GhosttyParser
    from .terminal.keys import (
        KEY_MAP as _KEY_MAP,
        APP_MODE_KEY_MAP as _APP_MODE_KEY_MAP,
        CTRL_KEY_MAP as _CTRL_KEY_MAP,
        ALT_KEY_MAP as _ALT_KEY_MAP,
        SHIFT_KEY_MAP as _SHIFT_KEY_MAP,
        get_key_code as _get_key_code,
        get_ctrl_key_code as _get_ctrl_key_code,
        get_alt_key_code as _get_alt_key_code,
        get_shift_key_code as _get_shift_key_code,
        translate_key as _translate_key,
        encode_win32_key as _encode_win32_key,
    )
    from .terminal.pty_env import sanitize_pty_env as _sanitize_pty_env
    from .terminal.profile_availability import (
        command_exists as _command_exists,
        menu_caption as _menu_caption_pure,
        profile_is_available as _profile_is_available_pure,
    )
    from .terminal.profile_schema import validate_profiles as _validate_profiles
    from .terminal.layout import accepted_cols as _accepted_cols, accepted_rows as _accepted_rows, gutter_digit_delta as _gutter_digit_delta

    from .terminal.render import (
        HOST_CURSOR_SCOPE as _HOST_CURSOR_SCOPE,
        build_text_and_regions as _build_text_and_regions_pure,
        cursor_text_offset as _cursor_text_offset,
        paint_host_cursor as _paint_host_cursor,
        punch_host_cursor_region as _punch_host_cursor_region,
        trim_display_rows as _trim_display_rows,
    )
    from .terminal.caret import (
        adjust_display_caret as _adjust_display_caret,
        pad_row_for_caret as _pad_row_for_caret,
        find_prompt_row as _find_prompt_row,
        input_start_col as _input_start_col,
        field_right_limit as _field_right_limit,
    )
    from .terminal.mouse import (
        BTN_RELEASE_X10 as _BTN_RELEASE_X10,
        BTN_WHEEL_DOWN as _BTN_WHEEL_DOWN,
        BTN_WHEEL_UP as _BTN_WHEEL_UP,
        encode_mouse as _encode_mouse,
        st_button_to_proto as _st_button_to_proto,
        view_point_to_cell as _view_point_to_cell,
    )
    from .terminal.log_paths import DEBUG as _DEBUG
    from .terminal.color_scheme_log import color_scheme_log as _color_scheme_log
    from .terminal.settings_debug_log import settings_debug_log as _settings_debug_log
    from .terminal.raw_debug_log import debug_log as _debug_log
    from .terminal.cast_recorder import CastRecorder
    from .terminal.session_text_log import SessionTextLog
except ImportError as _term_imp_err:
    # Unit tests / scripts outside Packages/User use top-level `ai.*`.
    # Do NOT hide a real missing-name error behind "No module named 'ai'".
    try:
        from terminal.colors import (
            quantize256 as _quantize256,
            pack_attr as _attr,
            xterm256_rgb as _xterm256_rgb,
            XTERM256_RGB as _XTERM256_RGB,
            FG_SHIFT as _FG_SHIFT,
            BG_SHIFT as _BG_SHIFT,
            ATTR_FG_MASK as _ATTR_FG_MASK,
            ATTR_BG_MASK as _ATTR_BG_MASK,
            BOLD as _BOLD,
            REVERSE as _REVERSE,
            FAINT as _FAINT,
            BG_LUMA_THRESHOLD as _BG_LUMA_THRESHOLD,
            ANSI16_HEX as _ANSI16_HEX,
            xterm_hex as _xterm_hex,
            HEX as _HEX,
            scope_name_for as _scope_name_for,
            font_style_for as _font_style_for,
            rstrip_cells as _rstrip_cells,
            scheme_colors_for as _scheme_colors_for,
            ensure_contrast as _ensure_contrast,
            DEFAULT_FG_HEX as _DEFAULT_FG_HEX,
            BG_NEAR_BLACK as _BG_NEAR_BLACK,
            _ANSI16_RGB,
        )
        from terminal.screen import Screen as _Screen, BLANK as _BLANK
        from terminal.ghostty_engine import GhosttyParser as _GhosttyParser
        from terminal.keys import (
            KEY_MAP as _KEY_MAP,
            APP_MODE_KEY_MAP as _APP_MODE_KEY_MAP,
            CTRL_KEY_MAP as _CTRL_KEY_MAP,
            ALT_KEY_MAP as _ALT_KEY_MAP,
            SHIFT_KEY_MAP as _SHIFT_KEY_MAP,
            get_key_code as _get_key_code,
            get_ctrl_key_code as _get_ctrl_key_code,
            get_alt_key_code as _get_alt_key_code,
            get_shift_key_code as _get_shift_key_code,
            translate_key as _translate_key,
            encode_win32_key as _encode_win32_key,
        )
        from terminal.pty_env import sanitize_pty_env as _sanitize_pty_env
        from terminal.profile_availability import (
            command_exists as _command_exists,
            menu_caption as _menu_caption_pure,
            profile_is_available as _profile_is_available_pure,
        )
        from terminal.profile_schema import validate_profiles as _validate_profiles
        from terminal.layout import accepted_cols as _accepted_cols, accepted_rows as _accepted_rows, gutter_digit_delta as _gutter_digit_delta

        from terminal.render import (
            HOST_CURSOR_SCOPE as _HOST_CURSOR_SCOPE,
            build_text_and_regions as _build_text_and_regions_pure,
            cursor_text_offset as _cursor_text_offset,
            paint_host_cursor as _paint_host_cursor,
            punch_host_cursor_region as _punch_host_cursor_region,
            trim_display_rows as _trim_display_rows,
        )
        from terminal.caret import (
            adjust_display_caret as _adjust_display_caret,
            pad_row_for_caret as _pad_row_for_caret,
            find_prompt_row as _find_prompt_row,
            input_start_col as _input_start_col,
            field_right_limit as _field_right_limit,
        )
        from terminal.mouse import (
            BTN_RELEASE_X10 as _BTN_RELEASE_X10,
            BTN_WHEEL_DOWN as _BTN_WHEEL_DOWN,
            BTN_WHEEL_UP as _BTN_WHEEL_UP,
            encode_mouse as _encode_mouse,
            st_button_to_proto as _st_button_to_proto,
            view_point_to_cell as _view_point_to_cell,
        )
        from terminal.log_paths import DEBUG as _DEBUG
        from terminal.color_scheme_log import color_scheme_log as _color_scheme_log
        from terminal.settings_debug_log import settings_debug_log as _settings_debug_log
        from terminal.raw_debug_log import debug_log as _debug_log
        from terminal.cast_recorder import CastRecorder
        from terminal.session_text_log import SessionTextLog
    except ImportError:
        raise _term_imp_err


# ─── colour scheme registration (Sublime-specific) ───────────────────────────
_SCHEME_LOCK = threading.Lock()
_REGISTERED_SCOPES = set()
_SCHEME_PATH = None  # Safely initialized inside _init_dynamic_color_scheme using sublime.packages_path()
# ST caret colour, always visible: user must always be able to see and
# control the cursor like in any normal editor buffer, including in
# scrollback where there is no PTY-app cursor to double up with -- explicit
# user requirement, overriding the older "match background = invisible"
# design (see AiTerminalRenderCommand for the matching caret-control fix:
# the render loop no longer auto-repositions the caret once the user has
# moved it away from the PTY's own cursor position).
_HOST_CARET_HEX = "#FFCC00"
# Permanent high-contrast block for host-synthesized cursors (Grok --minimal,
# plain shells). Must not depend on dynamic ai.fb.* registration.
# Foreground must be LIGHT: the host cell is a full-block glyph (█) painted by
# add_regions. ST often applies only the text colour, not the region fill; a
# black foreground then makes █ black-on-black (invisible). White █ reads as a
# solid block even when fill fails.
_HOST_CURSOR_RULE = {
    "scope": "ai.terminal.host_cursor",
    "background": "#CCCCCC",
    "foreground": "#FFFFFF",
}
_BASE_SCHEME = {
    "name": "AI Terminal",
    "variables": {},
    "globals": {
        "background": "#000000",
        "foreground": "#FFFFFF",
        # Always-visible ST caret, by explicit user request: they need to
        # see and control the cursor like in any normal editor buffer,
        # including in scrollback where there is no PTY-app cursor to
        # double up with. Previously matched background (invisible) to
        # avoid looking doubled next to the PTY app's own reverse-video
        # cursor at the live typing position -- that tradeoff was rejected.
        "caret": _HOST_CARET_HEX,
        "selection": "#444444",
        "line_highlight": "#0a0a0a",
        "gutter": "#000000",
        "gutter_foreground": "#808080",
    },
    "rules": [dict(_HOST_CURSOR_RULE)],
}
_PENDING_RULES = []
_WRITE_PENDING = False


def _ensure_host_cursor_rule(scheme_data):
    """Guarantee the permanent host-cursor scope exists in scheme rules.

    Returns True if scheme_data was mutated.
    """
    if not isinstance(scheme_data, dict):
        return False
    rules = scheme_data.setdefault("rules", [])
    scope = _HOST_CURSOR_RULE["scope"]
    for r in rules:
        if r.get("scope") == scope:
            # Keep contrast high even if an older rule was muted.
            changed = False
            if r.get("background") != _HOST_CURSOR_RULE["background"]:
                r["background"] = _HOST_CURSOR_RULE["background"]
                changed = True
            if r.get("foreground") != _HOST_CURSOR_RULE["foreground"]:
                r["foreground"] = _HOST_CURSOR_RULE["foreground"]
                changed = True
            return changed
    rules.append(dict(_HOST_CURSOR_RULE))
    return True


def _make_fb_rule(fg, bg, style_id=0):
    """Build one ai.fb.* colour-scheme rule with readable contrast."""
    fh, bh = _scheme_colors_for(fg, bg)
    scope = f"ai.fb.{fg}.{bg}" if not style_id else f"ai.fb.{fg}.{bg}.s{style_id}"
    rule = {"scope": scope, "background": bh, "foreground": fh}
    font_style = _font_style_for(style_id) if style_id else ""
    if font_style:
        rule["font_style"] = font_style
    return rule


def _repair_scheme_rules(scheme_data):
    """Fix legacy ai.fb.* rules missing fg or with black-on-black contrast.

    Older dynamic registration wrote background-only rules for default-fg
    scopes (ai.fb.0.*). Sublime's region painter then swaps/drops fg so
    Grok input text on a tinted panel becomes invisible. Also lift any
    near-black-on-near-black pair that survived from ANSI black on dark bg.

    Returns number of rules mutated.
    """
    if not isinstance(scheme_data, dict):
        return 0
    fixed = 0
    rules = scheme_data.setdefault("rules", [])
    for r in rules:
        sc = r.get("scope") or ""
        if not sc.startswith("ai.fb."):
            continue
        parts = sc.split(".")
        if len(parts) != 4:
            continue
        try:
            fg_id, bg_id = int(parts[2]), int(parts[3])
        except ValueError:
            print("[ai_terminal] color scheme rule scope parse failed for %r:\n%s"
                  % (sc, traceback.format_exc()))
            continue
        want_fg, want_bg = _scheme_colors_for(fg_id, bg_id)
        changed = False
        if r.get("background") != want_bg:
            r["background"] = want_bg
            changed = True
        # Always ensure a foreground; repair low-contrast / missing.
        cur_fg = r.get("foreground")
        if not cur_fg:
            r["foreground"] = want_fg
            changed = True
        else:
            fixed_fg = _ensure_contrast(cur_fg, r.get("background") or want_bg)
            if fixed_fg != cur_fg:
                r["foreground"] = fixed_fg
                changed = True
            # Prefer canonical palette fg when we had to invent one from missing.
        if changed:
            fixed += 1
    return fixed


def _init_dynamic_color_scheme():
    global _SCHEME_PATH, _REGISTERED_SCOPES
    try:
        _SCHEME_PATH = os.path.join(sublime.packages_path(), "GhostShell", "ai_terminal.sublime-color-scheme")
        if os.path.exists(_SCHEME_PATH):
            size = os.path.getsize(_SCHEME_PATH)
            # If the file size is very large (e.g. the old precompiled 8.9MB static matrix), shrink it to the base scheme.
            # 15MB is a safe threshold to distinguish a dynamic scheme from the old static matrix.
            if size > 15000000:
                msg = f"[init] Existing color scheme is very large ({size} bytes). Overwriting with clean base scheme."
                print(f"[ai_terminal] {msg}")
                _color_scheme_log(msg)
                _save_color_scheme(_BASE_SCHEME)
            else:
                with open(_SCHEME_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    rules = data.get("rules", [])
                    for r in rules:
                        if "scope" in r:
                            _REGISTERED_SCOPES.add(r["scope"])
                dirty = False
                # Repair a stale invisible caret from schemes written before
                # this was made always-visible (caret used to be forced to
                # match background).
                g = data.setdefault("globals", {})
                bg = g.get("background", "#000000")
                if g.get("caret") in (None, bg):
                    g["caret"] = _HOST_CARET_HEX
                    dirty = True
                    _color_scheme_log(f"[init] Repaired invisible host caret -> {_HOST_CARET_HEX}.")
                if _ensure_host_cursor_rule(data):
                    dirty = True
                    _color_scheme_log("[init] Ensured ai.terminal.host_cursor rule.")
                n_fix = _repair_scheme_rules(data)
                if n_fix:
                    dirty = True
                    _color_scheme_log(
                        f"[init] Repaired {n_fix} ai.fb.* rules "
                        f"(missing fg / low contrast)."
                    )
                if dirty:
                    _save_color_scheme(data)
                _REGISTERED_SCOPES.add(_HOST_CURSOR_RULE["scope"])
            msg = f"[init] Initialized. Loaded {len(_REGISTERED_SCOPES)} registered scope rules from disk ({size} bytes)."
            print(f"[ai_terminal] {msg}")
            _color_scheme_log(msg)
        else:
            _save_color_scheme(_BASE_SCHEME)
            _REGISTERED_SCOPES.add(_HOST_CURSOR_RULE["scope"])
            msg = "[init] Created fresh dynamic color scheme file."
            print(f"[ai_terminal] {msg}")
            _color_scheme_log(msg)
    except Exception as e:
        msg = f"[init] ERROR: Failed to initialize dynamic color scheme: {e}"
        print(f"[ai_terminal] {msg}")
        _color_scheme_log(msg)


def _scheme_disk_paths():
    """All on-disk scheme paths we may read/write (never rely only on _SCHEME_PATH).

    Hot-reload resets module globals so _SCHEME_PATH can be None while the
    GhostShell scheme file still exists. Flush used to treat that as 'no file' and
    rewrite BASE+pending only — wiping thousands of rules (peak was 5275).

    Single source of truth: Packages/GhostShell (junction-linked repo). No
    dual-write to Packages/User — that was leaking stale/duplicate copies
    into the SText backup repo with no benefit.
    """
    paths = []
    if _SCHEME_PATH:
        paths.append(_SCHEME_PATH)
    try:
        gs_path = os.path.join(
            sublime.packages_path(), "GhostShell", "ai_terminal.sublime-color-scheme"
        )
        if gs_path not in paths:
            paths.append(gs_path)
    except (TypeError, AttributeError):
        print("[ai_terminal] scheme disk paths: packages_path lookup failed:\n%s"
              % traceback.format_exc())
    return paths


def _load_scheme_from_disk():
    """Load the largest valid scheme on disk (most rules wins)."""
    best = None
    best_n = -1
    best_path = None
    for p in _scheme_disk_paths():
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            n = len(data.get("rules") or [])
            if n > best_n:
                best, best_n, best_path = data, n, p
        except Exception as e:
            _color_scheme_log(f"[load] ERROR reading {p}: {e}")
    if best is not None:
        _color_scheme_log(f"[load] Using {best_path} with {best_n} rules")
    return best


def _durable_scheme_backup(scheme_data):
    """Keep a dated snapshot under the "log_root" setting's scheme_backups/."""
    try:
        from .terminal.log_paths import log_root
        n = len(scheme_data.get("rules") or [])
        if n < 100:
            return
        bdir = os.path.join(log_root(), "scheme_backups")
        os.makedirs(bdir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(bdir, f"ai_terminal_{n}rules_{ts}.sublime-color-scheme")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(scheme_data, f, indent=None, separators=(",", ":"))
        # Keep only the newest backup; each snapshot is a full ~400KB scheme
        # file and only the latest one is ever useful for recovery.
        bak = sorted(
            (os.path.join(bdir, x) for x in os.listdir(bdir)
             if x.endswith(".sublime-color-scheme")),
            key=os.path.getmtime,
            reverse=True,
        )
        for old in bak[1:]:
            try:
                os.remove(old)
            except OSError:
                _color_scheme_log(f"[backup] could not remove old backup {old}:\n{traceback.format_exc()}")
        _color_scheme_log(f"[backup] Wrote {path} ({n} rules)")
    except Exception as e:
        _color_scheme_log(f"[backup] ERROR: {e}")


def _save_color_scheme(scheme_data):
    # Never write a scheme that would shrink the on-disk rule set.
    # Absolute floor: never replace a file that has more rules than we're writing
    # when the existing file is "large" (the 5275→2 wipe class of bug).
    try:
        existing = _load_scheme_from_disk()
        if existing is not None:
            old_n = len(existing.get("rules") or [])
            new_n = len(scheme_data.get("rules") or [])
            if old_n > new_n and old_n >= 20:
                msg = (
                    f"[save] REFUSED wipe: disk has {old_n} rules, "
                    f"refusing to write {new_n}"
                )
                print(f"[ai_terminal] {msg}")
                _color_scheme_log(msg)
                return
    except Exception as e:
        _color_scheme_log(f"[save] guard error: {e}")

    paths = _scheme_disk_paths()
    if not paths:
        _color_scheme_log("[save] ERROR: no scheme paths available")
        return

    for p in paths:
        try:
            # Per-path guard: never shrink an individual file
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        prev = json.load(f)
                    prev_n = len(prev.get("rules") or [])
                    new_n = len(scheme_data.get("rules") or [])
                    if prev_n > new_n and prev_n >= 20:
                        _color_scheme_log(
                            f"[save] REFUSED shrink {p}: {prev_n} -> {new_n}"
                        )
                        continue
                except (OSError, ValueError, AttributeError):
                    print("[ai_terminal] scheme save: shrink-guard read failed for %s:\n%s"
                          % (p, traceback.format_exc()))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            temp_path = p + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(scheme_data, f, indent=None, separators=(",", ":"))
            os.replace(temp_path, p)
        except Exception as e:
            print(f"[ai_terminal] Error writing color scheme file to {p}: {e}")
            _color_scheme_log(f"[save] ERROR writing {p}: {e}")

    _durable_scheme_backup(scheme_data)


def _ensure_scopes_hydrated_from_disk():
    """If memory lost scopes after reload, re-read the largest on-disk scheme.

    Without this, register() thinks every scope is new and flush can race with
    a half-initialized path. Call under _SCHEME_LOCK or at init.
    """
    global _REGISTERED_SCOPES
    if len(_REGISTERED_SCOPES) >= 50:
        return
    data = _load_scheme_from_disk()
    if not data:
        return
    n0 = len(_REGISTERED_SCOPES)
    for r in data.get("rules") or []:
        sc = r.get("scope")
        if sc:
            _REGISTERED_SCOPES.add(sc)
    if len(_REGISTERED_SCOPES) > n0:
        _color_scheme_log(
            f"[hydrate] Loaded {len(_REGISTERED_SCOPES) - n0} scopes from disk "
            f"(now {len(_REGISTERED_SCOPES)} in memory)"
        )


def _register_scope_async(fg, bg, style_id=0):
    global _WRITE_PENDING
    scope = f"ai.fb.{fg}.{bg}" if not style_id else f"ai.fb.{fg}.{bg}.s{style_id}"

    with _SCHEME_LOCK:
        _ensure_scopes_hydrated_from_disk()
        if scope in _REGISTERED_SCOPES:
            return
        _REGISTERED_SCOPES.add(scope)
        _color_scheme_log(
            f"[register] Encountered new scope: {scope} "
            f"(Memory registered count: {len(_REGISTERED_SCOPES)})"
        )
        # Always set fg+bg with minimum contrast (ST bg-only rules hide text).
        _PENDING_RULES.append(_make_fb_rule(fg, bg, style_id))
        
        if _WRITE_PENDING:
            return
        _WRITE_PENDING = True
        
    # Throttled / debounced to avoid write storms and ST hot-reload crashes
    sublime.set_timeout_async(_flush_pending_rules, 15000)


def _flush_pending_rules():
    global _WRITE_PENDING, _PENDING_RULES, _SCHEME_PATH
    with _SCHEME_LOCK:
        _WRITE_PENDING = False
        if not _PENDING_RULES:
            return
        rules_to_add = list(_PENDING_RULES)
        _PENDING_RULES.clear()

    # Always re-resolve path (survives importlib.reload clearing globals).
    try:
        _SCHEME_PATH = os.path.join(
            sublime.packages_path(), "GhostShell", "ai_terminal.sublime-color-scheme"
        )
    except TypeError:
        print("[ai_terminal] flush pending rules: resolve scheme path failed:\n%s"
              % traceback.format_exc())

    scheme_data = _load_scheme_from_disk()

    if not scheme_data:
        # Only create empty base if no scheme file exists anywhere we know.
        any_exists = any(os.path.isfile(p) for p in _scheme_disk_paths())
        if any_exists:
            msg = (
                "[flush] CRITICAL SAFETY: scheme file(s) exist but unreadable; "
                "aborting write to avoid wipe."
            )
            print(f"[ai_terminal] {msg}")
            _color_scheme_log(msg)
            # Put pending rules back so a later flush can retry.
            with _SCHEME_LOCK:
                _PENDING_RULES = rules_to_add + _PENDING_RULES
            return
        scheme_data = dict(_BASE_SCHEME)
        scheme_data["rules"] = []

    # Merge by scope name (dedupe) then append new.
    by_scope = {}
    for r in scheme_data.get("rules") or []:
        sc = r.get("scope")
        if sc:
            by_scope[sc] = r
    for r in rules_to_add:
        sc = r.get("scope")
        if sc:
            by_scope[sc] = r
    scheme_data["rules"] = list(by_scope.values())
    # Always-visible caret (see _HOST_CARET_HEX).
    g = scheme_data.setdefault("globals", {})
    g["caret"] = _HOST_CARET_HEX
    _ensure_host_cursor_rule(scheme_data)
    n_fix = _repair_scheme_rules(scheme_data)
    if n_fix:
        _color_scheme_log(f"[flush] Repaired {n_fix} legacy ai.fb.* rules.")

    _save_color_scheme(scheme_data)
    msg = (
        f"[flush] SUCCESS: Flushed {len(rules_to_add)} dynamic rules to disk. "
        f"Total rules: {len(scheme_data.get('rules', []))}"
    )
    print(f"[ai_terminal] {msg}")
    _color_scheme_log(msg)


def _scope_for(attr):
    """Map a packed cell attr to a precompiled scope, or None for default.

    Reverse with default colours must not collapse to (0,0)/None — that made
    Claude's reverse-video block cursor invisible (see terminal.colors).
    """
    if attr == 0:
        return None
    # Prefer the pure helper (keeps reverse-default logic in one place).
    try:
        from .terminal.colors import scope_name_for as _pure_scope
    except ImportError:
        from terminal.colors import scope_name_for as _pure_scope
    scope = _pure_scope(attr)
    if scope is None:
        return None
    # Register dynamic scheme rule if needed (ai.fb.<fg>.<bg>[.s<style_id>]).
    try:
        # "ai.fb.1.16" -> fg=1, bg=16; "ai.fb.1.16.s3" -> style_id=3 (bold+italic)
        parts = scope.split(".")
        fg, bg = int(parts[2]), int(parts[3])
        style_id = int(parts[4][1:]) if len(parts) > 4 else 0
    except (IndexError, ValueError):
        print("[ai_terminal] scope name fg/bg parse failed for %r:\n%s"
              % (scope, traceback.format_exc()))
        return scope
    if scope not in _REGISTERED_SCOPES:
        _register_scope_async(fg, bg, style_id)
    return scope


# ─── plugin settings (ai_terminal.sublime-settings) ──────────────────────────
# User-tunable knobs read from a settings file so they can be changed without
# editing source: scrollback history size (the minimap-fill knob -- retune by
# eye against the minimap) and min/max terminal columns (floor/ceiling on the
# auto-sized cols). A settings-change callback swaps the live deques; the resize
# poller picks up new column bounds on its next tick (~750ms), so edits apply
# without a plugin reload (which would tear down the PTY).
_SETTINGS_NAME = "ai_terminal.sublime-settings"
_settings = None  # sublime.Settings; (re)bound in plugin_loaded


_profiles_cache = None  # (signature, merged catalog); see _all_profiles


def _deep_merge(base, override):
    """Merge two dicts the way VS Code does (configurationModels.mergeContents):
    where both sides hold a dict, merge them key by key, recursively; anything
    else in `override` (a string, a number, a list) replaces the value in
    `base` outright."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_profiles_layer(resource_path):
    """The "profiles" dict from ONE settings file, read on its own so Sublime's
    one-level merge cannot hide the other layer. {} if missing or malformed."""
    try:
        data = sublime.decode_value(sublime.load_resource(resource_path))
    except Exception:
        return {}
    profiles = data.get("profiles") if isinstance(data, dict) else None
    return profiles if isinstance(profiles, dict) else {}


def _file_mtime(path):
    """Modification time of `path` in nanoseconds, or None if it cannot be read."""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _all_profiles(s):
    """The profile catalog: the shipped ai_terminal.sublime-settings, then the
    user's Packages/User copy layered over it profile by profile. A profile
    the user sets to null is removed. The result is cached until either file
    changes on disk, because profile lookups run on every key and wheel event."""
    global _profiles_cache
    packages = sublime.packages_path()
    signature = (
        _file_mtime(os.path.join(packages, "User", _SETTINGS_NAME)),
        _file_mtime(os.path.join(packages, "GhostShell", _SETTINGS_NAME)),
    )
    if _profiles_cache is None or _profiles_cache[0] != signature:
        merged = _deep_merge(
            _read_profiles_layer("Packages/GhostShell/" + _SETTINGS_NAME),
            _read_profiles_layer("Packages/User/" + _SETTINGS_NAME),
        )
        catalog = {name: profile for name, profile in merged.items() if profile is not None}
        _profiles_cache = (signature, catalog)
    return dict(_profiles_cache[1])


def _settings_obj(settings=None):
    """The Settings object every knob reads: an explicit one, else the cached
    global bound in plugin_loaded, else a fresh load."""
    return settings or _settings or sublime.load_settings(_SETTINGS_NAME)


def _profile_settings(profile_name, settings=None):
    """The profile dict named `profile_name`, or None when there is no such
    (dict-shaped) profile."""
    if not profile_name:
        return None
    profiles = _all_profiles(_settings_obj(settings))
    if not isinstance(profiles, dict):
        return None
    profile = profiles.get(profile_name)
    return profile if isinstance(profile, dict) else None


def _report_profile_validation(settings=None):
    """Validate the merged profile catalog and make configuration drift visible."""
    errors, warnings = _validate_profiles(_all_profiles(_settings_obj(settings)))
    for message in errors:
        print("[ai_terminal] profile settings ERROR: %s" % message)
    for message in warnings:
        print("[ai_terminal] profile settings warning: %s" % message)
    if errors:
        sublime.status_message(
            "Ai terminal: %d invalid profile setting(s); see console" % len(errors)
        )
    return errors, warnings


def _profile_bool(profile_name, key, default, settings=None):
    """Per-profile boolean override for `key`, else `default`.

    The profile only wins when it actually names the key -- absence means
    "inherit", so a caller's default (a module kill switch, another flag) is
    never shadowed by a falsy missing value.
    """
    profile = _profile_settings(profile_name, settings)
    if profile is not None and key in profile:
        return bool(profile[key])
    return default


def _setting_bool(key, default, profile_name=None, settings=None):
    """Boolean knob resolved profile-override first, then the global settings
    key of the same name, then `default`."""
    s = _settings_obj(settings)
    profile = _profile_settings(profile_name, s)
    if profile is not None and key in profile:
        return bool(profile[key])
    return bool(s.get(key, default))


def _setting_string(key, default, profile_name=None, settings=None):
    """String knob resolved profile-override first, then the global settings
    key of the same name, then `default`.

    A falsy value (empty string, null, absent key) at either level means
    "inherit" rather than "set to nothing" -- used for font_face, where the
    fallback (`default=None`) means "leave ST's own current font alone."
    """
    s = _settings_obj(settings)
    profile = _profile_settings(profile_name, s)
    if profile is not None and profile.get(key):
        return profile[key]
    return s.get(key, default) or default


def _setting_number(key, default, cast=int, profile_name=None, settings=None):
    """Numeric knob resolved profile-override first, then the global settings
    key of the same name, then `default`.

    A hand-edited settings file is the only source here, so a bad value must
    never propagate as an exception into a render/resize tick.
    """
    s = _settings_obj(settings)
    profile = _profile_settings(profile_name, s)
    if profile is not None and key in profile:
        raw = profile[key]
    else:
        raw = s.get(key, default)
    if raw is None:
        # A settings key explicitly set to null (e.g. "no cap") resolves to
        # None here, same as a missing key -- that's the intended default,
        # not a cast failure, so don't run it through cast()/log a traceback.
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        print("[ai_terminal] numeric setting %r cast failed, using default:\n%s"
              % (key, traceback.format_exc()))
        return default


_DEFAULT_SCROLLBACK = 300
_DEFAULT_MIN_COLS = 20
_DEFAULT_MIN_ROWS = 1

# TODO(feature): no clickable-URL support -- URLs printed to the terminal
# (plain text or OSC 8 hyperlinks) are inert; clicking/ctrl-clicking one does
# nothing. Most terminal emulators either detect bare URL regex spans and
# open them on click, or honor OSC 8 (\x1b]8;;URL\x1b\\text\x1b]8;;\x1b\\) and
# make the wrapped text clickable. Neither is implemented here. Would need:
# a URL-span scan over rendered rows (or OSC 8 parsing in the ANSI parser),
# a click handler that checks the clicked cell against detected spans, and
# webbrowser.open() (or os.startfile on Windows) to launch it.

# Kill switch per user directive: mouse handling (DEC mouse-tracking click/
# drag forwarding to the PTY, and the always-swallow wheel-scroll routing)
# judged buggy and disabled outright. False = ST's native mouse/selection/
# scroll behavior applies everywhere; nothing mouse-related is ever forwarded
# to a PTY, regardless of whether the app requested DEC mouse tracking.
# Per-profile override: a profile can set "mouse_handling": true to opt back
# in (see _mouse_handling_enabled below) -- needed for apps like Vibe (a
# Textual TUI) that manage their own scroll region without ever emitting a
# real ANSI scroll, so Screen.history never populates and PageUp/PageDown
# reach nothing; mouse wheel is the only way such an app can scroll at all.
_MOUSE_HANDLING_ENABLED = False

# Kill switch per user directive (2026-08-18): the auto-scroll/follow/pin
# machinery -- _scroll_to_bottom, _pin_terminal_viewport, _pin_viewport_rest_dip_only,
# the render loop's do_follow write, and _clamp_vp_loop's several rest-pin
# branches -- had accumulated enough interacting special cases (footer-size
# assumptions, TUI-vs-shell branches, pan/latch state machines) that every
# targeted fix broke a different case live. Rather than keep patching that
# pile, every viewport write in this file now goes through the single
# _set_viewport choke point below, gated on this setting.
#
# RE-ENABLED 2026-08-27 (Terminus rewrite, stage 2, ai/TODO.md) after the
# actual redesign: the follow/drift decision now reads term._live_anchor_y
# (single writer per site, see its introduction above) instead of
# term._last_vp_y (which this same machinery also wrote with an
# incompatible meaning -- rest=0.0 -- corrupting the drift check). Verified
# in a genuinely isolated process (portable ST + git worktree) before this
# flip.
#
# Was a hardcoded module constant (_SCROLL_MANIPULATION_ENABLED) until
# 2026-09-21 -- rule 7 (everything editable through a setting) was not met;
# converted to scroll_manipulation_enabled in ai_terminal.sublime-settings,
# same pattern as the other bisection gates (caret_footer_pinning_enabled
# etc.) just above it in that file.
def _keep_screen_height_steady(term, rows, trimmed):
    """Never let the tab lose rows at the bottom once they have been shown.

    rows is the untrimmed frame (scrollback + every screen row); trimmed is
    the same frame with trailing blank rows dropped (trim_display_rows).
    Trimming alone makes the tab's height follow an app's footer: when
    Claude Code's status footer shrinks by a line, the tab shrinks by a line
    and the follow code scrolls the view up, then down again when the footer
    grows back -- the "jiggle" on status updates (logged live 2026-09-22:
    buffer 350 -> 348 -> 350 -> 351 rows, each change followed by a
    _scroll_to_bottom write). A real terminal's screen never changes height,
    so a shorter footer just leaves a blank row. This keeps the number of
    dropped rows from ever growing again: a new tab still starts compact and
    grows as content arrives, but a row, once shown, stays.

    Starts over when the screen height changes (resize) or the app switches
    between the normal and the alternate screen. Terminus trims the same
    way as trim_display_rows, so this is a deviation from it (see
    docs/DEVIATIONS_FROM_TERMINUS.md section 6).
    """
    screen = term.screen
    screen_rows = len(rows) - len(screen.history)
    key = (screen_rows, bool(getattr(screen, "alt_screen", False)))
    dropped = len(rows) - len(trimmed)
    if getattr(term, "_steady_rows_key", None) != key:
        term._steady_rows_key = key
        term._steady_rows_dropped = dropped
    else:
        term._steady_rows_dropped = min(term._steady_rows_dropped, dropped)
    return rows[:len(rows) - term._steady_rows_dropped]


def _scroll_manipulation_enabled():
    return _setting_bool("scroll_manipulation_enabled", True)


def _set_viewport(view, pos, animate=False):
    """Single choke point for every view.set_viewport_position() call in this
    file -- see _scroll_manipulation_enabled. No-op while disabled so the
    user's own scroll position (wheel, drag, keyboard) is never overwritten."""
    if not _scroll_manipulation_enabled():
        return
    view.set_viewport_position(pos, animate)


def _term_profile_name(term):
    return term.profile_name if term is not None else None


def _set_auto_follow(term, value):
    """Single choke point for every term._auto_follow assignment.

    Mirrors the flag onto term.screen.trim_paused (the inverse): while the
    user is scrolled back reading history (_auto_follow False), the engine
    holds off evicting old scrollback lines instead of silently trimming
    the buffer out from under the read position (2026-08-18 -- "if I am
    scrolled back into the buffer, the text should not trim off the top").
    The pause/resume + deferred-catchup logic itself lives in Screen
    (terminal/screen.py, pure/testable), not here -- this just keeps the
    two flags in lockstep from the one place ai_terminal.py has both.
    """
    value = bool(value)
    if term is None:
        return value
    term._auto_follow = value
    screen = getattr(term, "screen", None)
    if screen is not None and hasattr(screen, "set_trim_paused"):
        screen.set_trim_paused(not value)
    _update_debug_status(term)
    return value


def _mouse_handling_enabled(term):
    """Effective mouse-handling flag for one terminal: profile override, or
    the global kill switch above when the profile doesn't set one.

    HISTORY (2026-09-09, do not repeat this investigation without reading
    this first). A purpose-built test harness (tests/mock_agent_cli.py
    --mouse -- a bare Python script doing nothing but print()) sent raw
    xterm mouse-enable bytes (`ESC[?1000h` etc.) that visibly arrived (its
    subsequent screen content rendered fine) yet never set
    screen.private_modes, while feeding the identical bytes directly into
    the same parser did. That's real: ConPTY's mouse passthrough is keyed
    to the child calling the Win32 console API `SetConsoleMode(stdin,
    ENABLE_MOUSE_INPUT)` (microsoft/terminal#376, fixed by #9970), not to
    it writing an xterm-style escape to its own stdout -- conhost's VT
    engine swallows the latter internally. This was first over-generalized
    to "no cross-platform CLI can ever get mouse tracking through ConPTY
    on Windows" -- WRONG, disproven by a full live retest the same day.

    ACTUAL EMPIRICAL RESULT (2026-09-09, fresh spawn + direct
    screen.private_modes inspection, not asciicast-regex-guessing): 9 of
    11 real profiles tested get real DEC mouse tracking working today --
    GitHub Copilot, Cline (+ user-confirmed real click), Grok Build,
    jcode, Kilo Code, Mimo, OpenCode, Vibe, and the Pybackup Go TUI. Only
    2 showed no tracking (Junie; OpenCode routed through `ollama launch
    opencode`) -- both worth re-checking on their own terms, not evidence
    of a platform ceiling. "Grok Build --minimal" (a different rendering
    mode of the same app) never requests tracking at all, unrelated to any
    of this. Full results + methodology in the "agent_tui_catalog.sqlite3"
    `notes` column for each agent (~/data), dated 2026-09-09.

    Conclusion: the ConPTY/SetConsoleMode mechanism above is a real,
    correct explanation for *why a naive implementation could fail*, but
    is not a practical concern for real, maturely-built CLI tools -- their
    terminal-handling libraries evidently touch enough of the Windows
    console API (for ANSI/raw-mode setup) to satisfy ConPTY's passthrough
    trigger as a side effect. Do not re-assert "mouse tracking can't work
    on Windows through this file" -- it demonstrably does, for nearly
    everything real. If a *specific* profile shows no tracking, treat it
    as that profile's own question, verified live (spawn + inspect
    screen.private_modes directly), not as this platform-wide issue.
    """
    return _profile_bool(
        _term_profile_name(term), "mouse_handling", _MOUSE_HANDLING_ENABLED
    )


def _pin_viewport_enabled(term):
    """Whether mouse-tracking alone should hard-pin the viewport (the
    _tui_like path below). Defaults to True -- unchanged behavior for every
    profile that doesn't set this.

    The mouse_tracking heuristic assumes the app owns its scroll region and
    never emits a real ANSI scroll (Qwen/Vibe: Screen.history never
    populates). A profile whose app streams genuine scrollback content
    despite wanting mouse tracking (for example gotui after its no-alt-
    screen redesign: table/toolbar clicks still forwarded via
    mouse_handling, but log lines are real tea.Println output) can set
    "pin_viewport": false to opt out of the hard pin and let real ST
    scrollback move normally.
    """
    return _profile_bool(_term_profile_name(term), "pin_viewport", True)


def _osc_title_enabled(term):
    """Whether OSC 0/2 title changes (ssh, vim, npm scripts, ...) should
    rename the ST tab. Defaults to False: existing profile-name-based tab
    titling is relied upon and must not change unless opted into, per-profile
    ("osc_title_updates_tab": true) or globally via the same settings key.
    """
    return _setting_bool(
        "osc_title_updates_tab", False, profile_name=_term_profile_name(term)
    )


def _wheel_to_pty_enabled(term):
    """Whether mouse-wheel scroll_lines/scroll_horizontally should be
    swallowed and forwarded to the PTY. Defaults to the profile's
    mouse_handling setting (today's combined behavior).

    Click/drag forwarding (drag_select) and wheel forwarding were always the
    same flag; a profile can now decouple them with "wheel_to_pty": false to
    keep row/toolbar clicks going to the PTY while giving the wheel back to
    ST's native scroll -- for apps like gotui that want clicks but have real
    scrollback content for the wheel to move through.
    """
    return _profile_bool(
        _term_profile_name(term), "wheel_to_pty", _mouse_handling_enabled(term)
    )


def _app_wants_mouse(term):
    """True when the app in this tab has requested mouse tracking (DECSET
    9/1000/1002/1003).

    GhostShell follows Ghostty here (the reference for this path, owner
    2026-09-23): mouse events go only to an app that asked for them, in the
    mode it asked for -- Ghostty's isMouseReporting() is its mouse-reporting
    setting AND the terminal's requested mode (~/tools/ghostty
    src/Surface.zig:3640). mouse_handling / wheel_to_pty are that setting;
    this is the requested mode. An app that never asked (Claude Code,
    PowerShell) keeps Sublime's own clicks, selection and scrolling.
    """
    return bool(term is not None and term.screen.mouse_tracking)


def _mouse_goes_to_app(term):
    """True when mouse events in this tab should go to the app now: the app
    asked (_app_wants_mouse) and Text Edit Mode (term.copy_mode) is off.

    Text Edit Mode hands the whole tab to Sublime -- keys already, and since
    2026-09-23 the mouse too (owner's choice over a second, mouse-only
    toggle; same role as Ghostty's toggle_mouse_reporting, one tab, in memory
    only). The mouse_handling / wheel_to_pty settings are checked separately
    by each caller.
    """
    return _app_wants_mouse(term) and not getattr(term, "copy_mode", False)


def _page_keys_to_pty(term):
    """Whether PageUp/PageDown should reach the PTY instead of paging the
    Sublime view.
    """
    val = _profile_bool(_term_profile_name(term), "page_keys_to_pty", None)
    if val is not None:
        return val
    if _tui_like(term):
        return True
    return False


def _home_end_native_enabled(term):
    """Whether Home/End should go to native ST navigation instead of the PTY
    (PageUp/PageDown go native unless `_page_keys_to_pty` / `_tui_like` --
    see AiTerminalKeypressCommand.run). Defaults to False --
    most profiles run interactive readline-style apps (Claude, Codex, shells)
    that need these keys to reach the PTY and move the app's own input-line
    cursor. Only scrollback-viewer profiles with no real line-editing (e.g.
    "Pybackup Go TUI", which is keyboard-only aside from these) should opt in
    with "home_end_native": true.

    Deliberately NOT tied to wheel_to_pty/mouse_handling: those default to
    False globally for an unrelated reason (avoiding a click/mouse-tracking
    bug in Qwen), and piggybacking Home/End on that default silently broke
    Home/End for every profile that didn't explicitly set mouse_handling.
    """
    return _profile_bool(_term_profile_name(term), "home_end_native", False)


def _tui_like(term):
    """True when the view should be treated as an app-owned fullscreen TUI
    (pin viewport to rest, never let it scroll away on its own).

    mouse_tracking only counts when mouse handling is enabled (globally or
    for this profile) -- with mouse handling off, an app merely requesting
    DEC mouse tracking (but not alt-screen) is no different from a plain
    scrollback shell: nothing is forwarded to it either way, so there is no
    reason to permanently pin the viewport to the top and block real
    scrollable content below the fold.
    Was previously `alt_screen or mouse_tracking` unconditionally -- with the
    host scroll pad removed (_host_rest_y always 0.0 now), that pinned every
    mouse-tracking app's viewport to literal y=0 forever, on every 8ms clamp
    tick, regardless of how the viewport got moved (scroll wheel, keyboard,
    even a direct minimap/scrollbar drag). Confirmed live as the actual cause
    of Qwen's "can't scroll past the top" symptom.
    """
    if term is None:
        return False
    if term.screen.alt_screen:
        return True
    if (
        bool(term.screen.mouse_tracking)
        and _mouse_handling_enabled(term)
        and _pin_viewport_enabled(term)
    ):
        return True
    # Neither alt-screen nor mouse-tracking DECSET is a real signal -- an
    # inline app can still fully clear-and-redraw a bounded frame every
    # keystroke (real \x1b[2J, no DECSET at all: confirmed live for
    # Continue/cn 2026-09-05, cast ai_2026-09-05_100905_174369 -- host
    # scroll-to-bottom churn on every printable key raced its own redraw,
    # duplicating its startup banner in scrollback; a real terminal, which
    # never autonomously repositions the viewport under an app the way
    # _scroll_to_bottom does, does not have this failure mode). No DECSET
    # exists for a profile to opt into automatically, so this is an
    # explicit escape hatch a profile sets when its own redraw model
    # (full clear + redraw from a fixed top-left) needs the same
    # pin-viewport treatment as a real alt-screen/mouse-tracking app.
    return _profile_bool(_term_profile_name(term), "force_tui_like", False)
_DEFAULT_LAUNCH_COMMAND = ["cmd.exe"] if os.name == "nt" else [
    os.environ.get("SHELL") or "/bin/bash"
]
_DEFAULT_SPAWN_ENV = {
    "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
    "CLAUDE_CODE_AI_TERMINAL_SENTINEL": "propagated",
}


def _scrollback_size(profile_name=None):
    """Scrollback line cap: per-profile override else the global setting.

    This is lines for Screen.history. ghostty_engine converts to bytes
    for libghostty-vt's max_scrollback (a byte limit, not a row count).
    """
    return max(
        0,
        _setting_number(
            "scrollback_history_size", _DEFAULT_SCROLLBACK, profile_name=profile_name
        ),
    )


def _broker_scrollback_bytes(profile_name=None):
    """Bounded raw-output budget used to reconstruct detachable terminals."""
    value = _setting_number(
        "broker_scrollback_bytes", 2 * 1024 * 1024,
        cast=int, profile_name=profile_name,
    )
    return max(1024 * 1024, min(value, 256 * 1024 * 1024))


# Seconds a dead tab's final output stays visible before auto-close. Long
# enough to read a one-line error ("file not found", "[process exited]"),
# short enough that a normal `exit` in a shell profile still feels immediate.
_CLOSE_TAB_ON_EXIT_DELAY = 1.5


def _close_tab_on_exit(profile_name=None):
    """Whether a terminal tab should close itself when its PTY process ends
    (crash, clean exit, or the user typing `exit`). Default true: a dead tab
    that lingers with no process behind it isn't a normal terminal-app
    experience. A profile may set ``"close_tab_on_exit": false`` to opt out
    (e.g. a profile you want to keep open to read a crash/update message).
    """
    return _setting_bool("close_tab_on_exit", True, profile_name=profile_name)


def _log_tab_text(profile_name=None):
    """Whether tab text should be appended to a session .log file.

    Source is the text just written to the Sublime tab. Default true, same posture as
    record_asciicast. A profile may set ``"log_tab_text": false`` to opt out.
    """
    return _setting_bool("log_tab_text", True, profile_name=profile_name)


def _make_parser(screen):
    """libghostty-vt is the sole VT engine. See terminal/ghostty_engine.py."""
    return _GhosttyParser(screen)


def _cols_bounds():
    mn = max(1, _setting_number("min_columns", _DEFAULT_MIN_COLS))
    mx = _setting_number("max_columns", None)
    return mn, (max(mn, mx) if mx is not None else None)


def _min_rows():
    """Floor for the row count told to the PTY. Per user directive: there is
    no "comfortable minimum" -- rows must never exceed the pane's actual
    computed height (build_text_and_regions always emits every PTY row, so
    forcing rows above the visible pane leaves trailing blank rows that
    _scroll_to_bottom/_tui_like rest-pin logic can land on, hiding real
    content -- confirmed live as the cause of Claude Code's TUI going blank
    after typing). Floor is 1; the pane-height computation in _measure()
    is the only ceiling.
    """
    return max(1, _setting_number("min_rows", _DEFAULT_MIN_ROWS))


def _platform_argv(value, default=None):
    """Resolve a launch_command setting to an argv list for this platform.

    Accepts either a plain argv list (shared by every platform) or a dict
    keyed by "windows"/"linux"/"osx", so one settings file can drive the
    mirrored Windows and WSL trees. Windows-only commands that survive into
    a POSIX spawn fall back to the default shell rather than launching a
    Windows binary under a Unix pty.
    """
    if isinstance(value, dict):
        key = {"nt": "windows"}.get(os.name, sys.platform)
        if key.startswith("linux"):
            key = "linux"
        elif key == "darwin":
            key = "osx"
        value = value.get(key) or value.get("default")

    if not value or not isinstance(value, list) or not all(
        isinstance(a, str) for a in value
    ):
        return list(default if default is not None else _DEFAULT_LAUNCH_COMMAND)

    if os.name != "nt":
        head = os.path.basename(value[0]).lower()
        if head.endswith(".exe") or head in ("cmd", "powershell", "pwsh"):
            print(
                f"[ai_terminal] launch_command {value[0]!r} is Windows-only; "
                f"using {_DEFAULT_LAUNCH_COMMAND[0]!r} on this platform."
            )
            return list(_DEFAULT_LAUNCH_COMMAND)

    return list(value)


def _launch_command():
    """argv list used to spawn the terminal program. Read from the
    `launch_command` setting so the agent/gateway can be swapped (e.g. to
    `["claude"]` for direct Anthropic API, or `["opencode"]`) without editing
    the plugin. Falls back to _DEFAULT_LAUNCH_COMMAND on any shape error.
    Applied on the next _spawn (reopen the ai_terminal tab)."""
    cmd = _settings_obj().get("launch_command", _DEFAULT_LAUNCH_COMMAND)
    return _platform_argv(cmd)


def _refresh_path_env(env):
    """Rebuild Path from HKLM+HKCU registry and merge with the process Path.

    Sublime Text inherits PATH at launch. `setx` / installer PATH edits only
    hit the registry, so a long-lived ST process can miss npm / agent bins.
    Child PTYs get the refreshed Path so menu launches keep working.
    """
    if os.name != "nt" or not isinstance(env, dict):
        return env
    try:
        import winreg
    except ImportError:
        print("[ai_terminal] refresh PATH from registry: winreg unavailable:\n%s"
              % traceback.format_exc())
        return env

    parts = []
    for root, subkey in (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(root, subkey) as key:
                raw, typ = winreg.QueryValueEx(key, "Path")
        except OSError:
            print("[ai_terminal] refresh PATH from registry: read %s\\%s failed:\n%s"
                  % (root, subkey, traceback.format_exc()))
            continue
        if typ == getattr(winreg, "REG_EXPAND_SZ", 2):
            raw = os.path.expandvars(raw)
        if raw:
            parts.extend(str(raw).split(";"))

    current = (env.get("Path") or env.get("PATH") or "").split(";")
    seen = set()
    merged = []
    for p in parts + current:
        p = (p or "").strip().rstrip("\\")
        if not p:
            continue
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        merged.append(p)
    if not merged:
        return env
    out = dict(env)
    out["Path"] = ";".join(merged)
    out["PATH"] = out["Path"]
    return out


# Windows App Execution Alias: installing WSL puts a 0-byte bash.exe stub on
# PATH ahead of Git Bash. Prefer a real Git install when resolving bare "bash".
_GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)


def _is_wsl_bash_stub(path):
    """True for the WindowsApps WSL bash launcher (not a real shell binary)."""
    if not path:
        return False
    low = os.path.normcase(path)
    if "\\windowsapps\\bash.exe" in low or "/windowsapps/bash.exe" in low:
        return True
    try:
        # App Execution Aliases are often 0-byte reparse points.
        if os.path.isfile(path) and os.path.getsize(path) == 0:
            return "windowsapps" in low
    except OSError:
        print("[ai_terminal] WSL bash stub check: stat %s failed:\n%s"
              % (path, traceback.format_exc()))
    return False


def _prefer_git_bash(resolved, search_path=None):
    """If *resolved* is the WSL bash stub, return Git Bash when installed."""
    if not _is_wsl_bash_stub(resolved):
        return resolved
    for cand in _GIT_BASH_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    # Last resort: any non-stub bash later on PATH (shutil.which only returns first).
    if search_path:
        for entry in search_path.split(os.pathsep):
            cand = os.path.join(entry, "bash.exe")
            if os.path.isfile(cand) and not _is_wsl_bash_stub(cand):
                return cand
    return resolved


def _resolve_launch_argv(argv, env=None):
    """Resolve bare command names for CreateProcessW.

    CreateProcess only auto-appends ``.exe``. npm global shims are ``.cmd``,
    so bare names like ``opencode`` fail with ERROR_FILE_NOT_FOUND (2) — and
    without use_last_error the plugin used to report GetLastError 0.

    Resolve via ``shutil.which`` (PATHEXT + PATH), then wrap ``.cmd``/``.bat``
    with ``cmd.exe /c`` and ``.ps1`` with PowerShell (RemoteSigned, so a shim
    that arrived from the internet still has to be signed to run).

    Bare ``bash`` skips the WSL WindowsApps stub in favour of Git Bash when
    present (WSL is the separate ``WSL Bash`` profile via wsl.exe).
    """
    argv = [str(a) for a in (argv or [])]
    if not argv:
        return argv

    search_path = None
    if env:
        search_path = env.get("Path") or env.get("PATH")

    exe0 = argv[0]

    if os.name != "nt":
        # POSIX: execvpe handles PATH lookup, and there are no .cmd/.ps1
        # shims to wrap. Resolve only to fail fast with a clear message.
        if os.path.isabs(exe0):
            if not os.path.isfile(exe0):
                raise FileNotFoundError(f"command not found: {exe0!r}")
            return argv
        if not shutil.which(exe0, path=search_path):
            raise FileNotFoundError(f"command not found on PATH: {exe0!r}")
        return argv

    if os.path.isabs(exe0) and os.path.isfile(exe0):
        resolved = exe0
    else:
        resolved = shutil.which(exe0, path=search_path)
        if not resolved:
            npm = "yes" if search_path and "npm" in search_path.lower() else "no"
            raise FileNotFoundError(
                f"command not found on PATH: {exe0!r} "
                f"(PATH contains npm dir: {npm}). "
                "Fix User PATH or restart Sublime Text after setx/installers."
            )
        if os.path.basename(exe0).lower() in ("bash", "bash.exe"):
            resolved = _prefer_git_bash(resolved, search_path)

    rest = argv[1:]
    low = resolved.lower()
    if low.endswith((".cmd", ".bat")):
        # /d skips AutoRun; list2cmdline will quote paths with spaces.
        return ["cmd.exe", "/d", "/c", resolved, *rest]
    if low.endswith(".ps1"):
        return [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "RemoteSigned",
            "-File",
            resolved,
            *rest,
        ]
    return [resolved, *rest]


def _spawn_env():
    """Dict of env vars to apply to the spawned terminal process (merged on
    top of os.environ). Read from the `spawn_env` setting so agent-specific
    env can be swapped alongside `launch_command` without editing the plugin.
    Keys and values must be strings; falls back to _DEFAULT_SPAWN_ENV on any
    shape error. Applied on the next _spawn (reopen the ai_terminal tab)."""
    ev = _settings_obj().get("spawn_env", _DEFAULT_SPAWN_ENV)
    if not isinstance(ev, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in ev.items()
    ):
        return dict(_DEFAULT_SPAWN_ENV)
    return dict(ev)


def _profile_is_available(profile_name, settings=None):
    """Quota-free menu availability for a configured terminal profile.

    Never launches the CLI, contacts a provider, refreshes OAuth, or spends
    inference quota. Executable detection prevents stale menu entries from
    launching.
    """
    s = _settings_obj(settings)
    if not profile_name:
        profile_name = s.get("default_profile")
    profile = _profile_settings(profile_name, s)
    path = os.environ.get("Path") or os.environ.get("PATH")
    return _profile_is_available_pure(profile_name, profile, path=path)


def _profile_menu_caption(profile_name, settings=None):
    """Menu caption for a profile: its name, plus "not installed" when the program is missing."""
    if not profile_name:
        profile_name = (
            _settings_obj(settings).get("default_profile") or "Default Profile"
        )
    return _menu_caption_pure(
        profile_name, executable_ok=_profile_is_available(profile_name, settings)
    )


_SECRETS_SETTINGS_NAME = "ai_terminal_secrets.sublime-settings"
_SECRET_PREFIX = "$secret:"
_ENV_PREFIX = "$env:"


def _resolve_env_refs(env):
    """Expand `$env:NAME` setting values from the host environment."""
    out = dict(env)
    for var, value in env.items():
        if not isinstance(value, str) or not value.startswith(_ENV_PREFIX):
            continue
        ref = value[len(_ENV_PREFIX):]
        name, sep, suffix = ref.partition("\\")
        resolved = os.environ.get(name)
        if resolved:
            out[var] = os.path.join(resolved, suffix) if sep and suffix else resolved
        else:
            out.pop(var, None)
    return out


def _resolve_secret_refs(env):
    """Expand `$secret:NAME` values in `env` from the User-only secrets file.

    API keys must not live in this repo (SText is public) nor in the ambient
    user environment (tools that auto-detect an API key there will try to bill
    the key instead of using a subscription login). So profiles reference a
    secret by name:

        "spawn_env": { "GEMINI_API_KEY": "$secret:GEMINI_API_KEY" }

    and the value is read at spawn time from
    Packages/User/ai_terminal_secrets.sublime-settings, which the pybak
    allowlist excludes and .gitignore ignores. The key is therefore only ever
    in the memory of the process that needs it.

    An unresolved reference is dropped rather than passed through literally, so
    an agent never receives the string "$secret:..." as if it were a real key.
    """
    refs = {
        k: v[len(_SECRET_PREFIX):]
        for k, v in env.items()
        if isinstance(v, str) and v.startswith(_SECRET_PREFIX)
    }
    if not refs:
        return env

    try:
        store = sublime.load_settings(_SECRETS_SETTINGS_NAME)
    except (RuntimeError, OSError):
        # Without the store every reference below resolves to nothing, which
        # looks exactly like an unconfigured key — say which it was.
        store = None
        print(
            "ai_terminal: could not load %s, every secret reference will be "
            "dropped:\n%s" % (_SECRETS_SETTINGS_NAME, traceback.format_exc())
        )

    out = dict(env)
    missing = []
    for var, name in refs.items():
        val = store.get(name) if store is not None else None
        if isinstance(val, str) and val:
            out[var] = val
        else:
            out.pop(var, None)
            missing.append("%s (%s)" % (var, name))
    if missing:
        # The agent starts anyway (many providers work off a subscription
        # login), but an unset key otherwise only shows up as an opaque auth
        # error from the child, so put it in front of the user too.
        detail = "no value in %s for %s; spawning without it" % (
            _SECRETS_SETTINGS_NAME,
            ", ".join(missing),
        )
        print("ai_terminal: %s" % detail)
        sublime.status_message("ai_terminal: %s" % detail)
    return out


# ─── on-disk logs ────────────────────────────────────────────────────────────
# Implementations live in terminal/{log_paths,color_scheme_log,
# settings_debug_log,raw_debug_log,cast_recorder,session_text_log}.py.
# Same filenames, messages, and failure handling as the former inlined copies.


def _apply_log_root_setting(settings=None):
    """Point every GhostShell log at the "log_root" setting's folder, or
    back at the default (~/data/logs/ghostshell) when it is empty."""
    from .terminal.log_paths import configure_log_root
    configure_log_root(_settings_obj(settings).get("log_root"))


def _on_settings_change():
    """Live-apply a settings edit: swap each live terminal's history deque to
    the new cap. Column bounds are picked up by the resize poller's next
    _measure (~750ms), so nothing to do here for cols."""
    _settings_debug_log(">>> _on_settings_change CALLED")
    global _profiles_cache
    _profiles_cache = None
    _apply_log_root_setting()
    _report_profile_validation()

    with _term_lock():
        terms = list(_term_registry().values())
    _settings_debug_log(f"Found {len(terms)} active terminal(s)")

    for t in terms:
        try:
            view_id = t.view.id() if t.view else "unknown"
            view_name = t.view.name() if t.view else "unnamed"
            # Per-terminal, not one cap for every terminal: a profile (e.g.
            # pybackup's launcher) can override scrollback_history_size.
            cap = _scrollback_size(getattr(t, "profile_name", None))
            _settings_debug_log(
                f"Processing terminal for view {view_id} ({view_name}), cap={cap}"
            )
            _settings_debug_log(f"Acquiring t._lock for view {view_id}...")
            with t._lock:
                _settings_debug_log(f"Acquired t._lock for view {view_id}. Calling t.screen.set_history_cap({cap})")
                t.screen.set_history_cap(cap)
                _settings_debug_log(f"Successfully returned from set_history_cap for view {view_id}")
        except Exception as e:
            msg = f"ERROR: _on_settings_change failed on terminal {t}: {e}\n{traceback.format_exc()}"
            print(f"[ai_terminal] {msg}")
            _settings_debug_log(msg)
    _settings_debug_log("<<< _on_settings_change FINISHED")


# ─── _Screen: cursor-aware grid ──────────────────────────────────────────────
# Cells carry a packed colour attr alongside the char; the renderer coalesces
# equal-attr runs into add_regions. The cursor-aware layout is what removes the
# Terminus gutter/width bugs.

_BLANK = " "


# ─── _Terminal: per-view owner + registry ────────────────────────────────────
#
# Registry MUST live on sys, not only as a module global. Hot-reload / re-exec
# of this file (PluginLoader, importlib.reload, or agents replacing
# sys.modules['User.ai.ai_terminal'] with a new module object) would otherwise
# create a second empty dict while the already-registered Command classes still
# close over the first one. New tabs spawn into dict B; keypress looks in dict A
# → silent dead keyboard ("Bash 2 not taking keystrokes").
def _term_registry():
    """Process-global {view_id: _Terminal}. Survives module reload / re-bind."""
    reg = getattr(sys, "_stext_ai_terminals", None)
    if not isinstance(reg, dict):
        reg = {}
        sys._stext_ai_terminals = reg
    return reg


def _term_lock():
    lock = getattr(sys, "_stext_ai_terminals_lock", None)
    if lock is None:
        lock = threading.Lock()
        sys._stext_ai_terminals_lock = lock
    return lock


# Module-level aliases (same objects as on sys after first import).
_TERMINALS = _term_registry()
_REG_LOCK = _term_lock()


class _ProcessProxy:
    """Compat shape for modules that used Terminus .process (ai_tab_manager etc)."""

    def __init__(self, pty):
        self._pty = pty

    @property
    def argv(self):
        return self._pty.argv

    @property
    def pid(self):
        return self._pty.pid

    def isalive(self):
        return self._pty.is_alive()


class _Terminal:
    def __init__(self, view, pty, screen, parser, spawn_env=None, profile_name=None):
        self.view = view
        self.pty = pty
        self.screen = screen
        self.parser = parser
        # Route libghostty-vt's query responses (DA/kitty-flags/XTVERSION/
        # size) through the same ordered write queue as real keystrokes --
        # see _on_parser_write_pty. Bound here, before the child exists
        # (_spawn constructs this object then prepare()'s the writer, and
        # only then calls pty.start()). Grok's keyboard-handling probe
        # (CSI ? u) fires the instant the process starts and is never
        # retried in that session, so a late bind makes /doctor report
        # "keyboard protocol is unavailable" for the whole session.
        parser.bind_write_pty(self._on_parser_write_pty)
        self.offset = 0
        # None while living in a normal tab; the output-panel name (e.g.
        # "Ai") while toggled into panel mode. See ai_terminal_toggle_panel /
        # _migrate_terminal_view.
        self.panel_name = None
        # Sticky across tab<->panel round trips (unlike panel_name, never
        # cleared back to None on returning to tab): reusing the same panel
        # name/view every time is what lets Sublime remember the height the
        # user last dragged it to. A fresh name each round trip would create
        # a brand-new panel at ST's tiny default height every single time.
        self._panel_home_name = None
        self.process = _ProcessProxy(pty)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._lock = threading.Lock()
        self._render_pending = False
        # CSI ?2026h/l ("synchronized output", DECSET mode 2026) render-defer
        # state -- see _do_render. term.screen.sync_output is the native-
        # backed level (GhosttyParser queries ghostty_terminal_mode_get after
        # every feed); _sync_defer_forced is this render path's own "stop
        # waiting" override once the 0.5s cap trips, since it cannot force
        # the native mode itself false the way the old regex-tracked flag
        # could force itself false.
        self._sync_defer_started = None
        self._sync_defer_forced = False
        self._reader = None
        # PTY writes must never run on Sublime's main plugin thread. Win32
        # WriteFile is synchronous and can block when ConPTY applies input
        # backpressure, freezing the whole editor after a keypress. A single
        # writer thread preserves input ordering while key commands return
        # immediately.
        self._write_queue = queue.Queue()
        self._writer = None
        self._input_cast_queue = queue.Queue()
        self._input_cast_writer = None
        # Text queued via queue_input(), drained into the PTY only once the
        # session has been quiet (no PTY output) for _QUEUED_INPUT_IDLE_S --
        # unlike send_string, which writes immediately regardless of
        # activity. Lets a caller hand off a follow-up prompt that lands
        # like real typing once the current output/thinking settles, instead
        # of racing live output or a busy agent. See queue_input().
        self._pending_input = []
        self._pending_input_poll_armed = False
        self._last_cols = screen.cols
        self._last_rows = screen.rows
        # Copy mode (ctrl+alt+c / AiTerminalToggleCopyModeCommand): while
        # True, plain navigation keys move the ST caret instead of reaching
        # the PTY. See AiTerminalKeypressCommand.run for the routing.
        self.copy_mode = False
        # Whether the in-tab profile-settings panel (toolbar "Settings" link,
        # AiTerminalToggleTuneProfilePanelCommand) is currently expanded below
        # the close toolbar. See _add_close_toolbar's panel_html branch.
        self.tune_profile_open = False
        # True once the user has moved the ST caret away from the PTY's own
        # cursor by a real gesture (click, native ST nav) -- see
        # AiTerminalViewListener.on_selection_modified. The render loop then
        # stops auto-repositioning the caret until this clears again (re-
        # entering the app's drawn command-line box). Deliberately NOT
        # derived by diffing the caret's absolute buffer offset against the
        # last position we placed it at: a full-buffer view.replace() (the
        # common, non fast-caret render path) collapses/shifts old regions
        # in ways unrelated to user intent, which falsely latched this
        # forever on the very first such frame (caret would freeze while
        # text kept flowing in). _in_render below suppresses selection
        # events that fire from our own edits so only genuine user-driven
        # moves flip this.
        self._user_owns_caret = False
        self._in_render = False
        self._spawn_env = spawn_env or {}
        self.profile_name = profile_name
        # Last OSC 0/2 title applied to the ST tab (osc_title_updates_tab
        # setting). None means "no app-set title" (never set, or cleared).
        self._applied_osc_title = None
        # Auto-follow model (Terminus-style): scroll to the bottom to show new
        # Claude output whenever _auto_follow is True. It starts True, flips
        # False when the user scrolls up to read scrollback (detected in the
        # render by vp drifting below the position we last pinned), and
        # re-engages when the user scrolls back near the bottom or types. Fresh
        # per _spawn, so a restart opens at the prompt (bottom) instead of
        # sticking at the top showing the banner.
        _set_auto_follow(self, True)
        self._last_vp_y = 0.0
        # Terminus-style single-anchor rewrite (ai/TODO.md, stage 2, in
        # progress): mirrors every _last_vp_y write. Not yet read anywhere --
        # purely additive until the follow-decision and viewport-write logic
        # switches over to it in a later commit.
        self._live_anchor_y = 0.0
        # Snapshot of screen.retired_total / len(screen.history) as of the
        # last render, so AiTerminalRenderCommand can tell exactly how many
        # lines the maxlen history deque evicted from the top this frame
        # (see _compensate_trim_scroll). None until the first render.
        self._last_retired_total = None
        self._last_history_len = None
        # Last PTY cell (1-based col,row) hit by a mouse click/drag. Used as
        # the wheel locus — ST's scroll_lines has no pointer coords, and the
        # caret is usually on the command line, which makes TUI scrollbars
        # ignore the wheel. Updated on every routed mouse event.
        self._last_mouse_cell = None
        # Pixel pan accumulator: ST trackpad often only nudges viewport_position
        # (no scroll_lines / no mousemap scroll_up). Clamp converts that dip
        # into TUI wheel ticks before pinning back to (0,0).
        self._vp_pan_accum = 0.0
        # False until viewport has settled at rest after spawn. Without this,
        # clamp sees dy_rest ≈ -pad_height on the empty first frame and injects
        # Up×N into the PTY before Grok draws (casts start with \x1b[A\x1b[A).
        self._vp_pan_armed = False
        self._spawn_mono = time.monotonic()
        # Updated on every _on_data call; read by Session Info to show how
        # long it's been since this session last produced any output, which
        # answers "is this still doing something" -- absolute start time
        # alone (registry created_at) doesn't. Defaults to spawn time so a
        # session that has never produced output yet still reports something
        # sane rather than a missing/zero value.
        self._last_output_at = time.time()
        # Asciicast v3 recording (recording patch). When recording is on,
        # start() opens a per-session .cast file and writes the v3 header;
        # _on_data / send_string / resize / kill append timed events. Off
        # (file is None) => all _cast() calls are no-ops. One file per session
        # (timestamped filename), not per day, so a resume's replay is a
        # separate recording rather than appended duplicates.
        self._cast_recorder = CastRecorder(notify=self._notify)
        self._text_log = SessionTextLog()  # see _log_tab_text() / _on_retire_line
        # Parser failures are reported once per terminal; see _on_data.
        self._feed_failed = False
        self._reattach_bootstrap = False
        self._bootstrap_got_bytes = False
        self._restored_text = ""
        # None normally. Set to a short reason string right before code in
        # this file deliberately ends this tab's connection on purpose --
        # either "handoff" (Open in Windows Terminal: pty.kill() detaches,
        # the session lives on elsewhere) or "killed" (Kill Session:
        # pty.explicit_kill(), the session really does end, on request, not
        # as a surprise crash). Either way the handles this closes are
        # indistinguishable, to the reader thread, from the child actually
        # dying unexpectedly -- this is what tells it "we meant this," and
        # which of the two messages to show. Consumed by _read_loop (skip
        # the exited-process auto-close reasoning either way -- the
        # commands that set this decide for themselves whether the tab
        # stays open or closes, not that heuristic) and by
        # AiTerminalViewListener.on_close (skip explicit_kill: for a
        # hand-off nothing local is ending; for an already-killed session a
        # second KILL would just be a harmless no-op against an
        # already-dead broker, but there is no reason to send it).
        self._expected_termination_reason = None
        # Geometry watcher for standard terminal resize behavior.
        self._watcher = _LayoutWatcher(self)

    @classmethod
    def from_id(cls, view_id):
        with _term_lock():
            return _term_registry().get(view_id)

    def prepare(self, reattach=False):
        """Open session logs and start the writer.

        Call before ``pty.start()`` for a fresh child. A broker reattach calls
        this only after its existing pipe connection succeeds, because no new
        child is being started and failed connection attempts must not create
        recording files.

        Grok (and other TUIs) emit capability probes the instant a fresh child
        exists and do not retry a timed-out keyboard-handling probe in the
        same session. The writer -- and the write_pty bind in __init__ --
        must already be live so the first CSI ? u can be answered.
        The reader cannot start yet: it needs the PTY handles that
        pty.start() creates.
        """
        # Recording patch: asciicast v3. Recording is on if
        # AI_TERMINAL_LOG_LINES is set in the spawn_env setting OR in ST's
        # process environment (_LOG_LINES). Checked per-spawn so a settings
        # edit takes effect on the next Open Ai here... without a restart.
        # When on, open a per-session .cast file (timestamped filename so
        # each session is its own recording -- a resume's replay is a NEW
        # .cast, not appended duplicates) and write the v3 header. Events
        # are appended by _cast() from _on_data / send_string / resize / kill.
        # Recording is on if AI_TERMINAL_LOG_LINES is set in the merged spawn
        # env (profile or legacy top-level) OR in ST's process env.
        # Record by default. The explicit setting gives users one predictable
        # switch instead of requiring every terminal profile to duplicate an
        # environment variable. Existing AI_TERMINAL_LOG_LINES overrides remain
        # supported for backward compatibility.
        try:
            log_on = _setting_bool(
                "record_asciicast", True, profile_name=self.profile_name
            )
        except (AttributeError, TypeError):
            log_on = True
        log_on = log_on or _LOG_LINES
        if not log_on:
            try:
                log_on = bool((self._spawn_env or {}).get("AI_TERMINAL_LOG_LINES"))
            except (AttributeError, TypeError):
                print("[ai_terminal] spawn_env log-lines check failed:\n%s"
                      % traceback.format_exc())
        if log_on:
            try:
                argv = self.pty.argv if hasattr(self.pty, "argv") else []
                # One timestamp shared by the cast and text snapshot makes
                # correlation mechanical. Microseconds prevent two terminals
                # opened in the same second from truncating the same cast.
                wall = time.time()
                stamp = "%s_%06d%s" % (
                    time.strftime("%Y-%m-%d_%H%M%S", time.localtime(wall)),
                    int((wall % 1.0) * 1000000),
                    "_reattach" if reattach else "",
                )
                self._cast_recorder.open(
                    self.screen.cols, self.screen.rows, argv,
                    filename_stamp=stamp,
                )
            except (OSError, AttributeError):
                print("[ai_terminal] cast open failed:\n%s" % traceback.format_exc())
                self._cast_recorder = CastRecorder(notify=self._notify)
                self._notify("recording disabled: could not open the .cast file")
        if _log_tab_text(self.profile_name):
            try:
                if "stamp" not in locals():
                    wall = time.time()
                    stamp = "%s_%06d%s" % (
                        time.strftime("%Y-%m-%d_%H%M%S", time.localtime(wall)),
                        int((wall % 1.0) * 1000000),
                        "_reattach" if reattach else "",
                    )
                self._text_log.open(stamp)
            except (OSError, AttributeError):
                print("[ai_terminal] text log open failed:\n%s" % traceback.format_exc())
                self._text_log = SessionTextLog()
                self._notify("tab text logging disabled: could not open the log file")
        self._ensure_writer()

    def start_reader(self):
        """Begin reading PTY output. Call immediately after pty.start()."""
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _notify(self, message):
        """Put an operational failure where the user will actually see it.

        The console is invisible to most users, so anything that silently
        degrades a session (recording off, input dropped) also goes to the
        status bar. Safe from any thread.
        """
        sublime.set_timeout(
            lambda: sublime.status_message("ai_terminal: %s" % message), 0
        )

    def _on_retire_line(self, text):
        """Screen.on_retire_line callback: one scrollback line just became
        permanent. Append-and-flush so a crash loses at most the current
        in-flight write, not the session."""
        log = getattr(self, "_text_log", None)
        if log is None or log.file is None:
            return
        try:
            log.write_line(text)
        except Exception as e:
            # A write that failed once (full disk, deleted file) fails for
            # every remaining line, so stop logging instead of printing the
            # same error per line for the rest of the session.
            print("[ai_terminal] text log write failed:\n%s" % traceback.format_exc())
            self._notify("text log disabled after write failure: %s" % e)
            self.screen.on_retire_line = None
            log.close()

    def _close_text_log(self):
        """Flush remaining tab lines and close the session text log.

        ``_log_painted_tab`` already queued every painted line, including
        visible scrollback.  ``screen.live_lines_text()`` is only the live
        screen, so using it as a final flush would drop scrollback. Safe to
        call more than once or before start().
        """
        log = getattr(self, "_text_log", None)
        if log is None or log.file is None:
            return
        log.close()

    def _ensure_writer(self):
        """Create the ordered PTY writer, including for hot-reloaded terminals."""
        writer = getattr(self, "_writer", None)
        if writer is not None and writer.is_alive():
            return
        if not hasattr(self, "_write_queue"):
            self._write_queue = queue.Queue()
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()
        cast_writer = getattr(self, "_input_cast_writer", None)
        if cast_writer is None or not cast_writer.is_alive():
            if not hasattr(self, "_input_cast_queue"):
                self._input_cast_queue = queue.Queue()
            self._input_cast_writer = threading.Thread(
                target=self._input_cast_loop, daemon=True
            )
            self._input_cast_writer.start()

    def _input_cast_loop(self):
        """Record input independently so recorder I/O cannot stall PTY writes."""
        while True:
            text = self._input_cast_queue.get()
            if text is None:
                return
            try:
                self._cast("i", text)
            except Exception as e:
                print(f"[ai_terminal] input cast error: {e}")

    def _write_loop(self):
        while True:
            item = self._write_queue.get()
            if item is None:
                return
            text, record = item
            try:
                # Deliver input before touching the optional recorder.  A slow
                # file flush or contention with the output recorder must never
                # hold an Up/Down key (or any other input) ahead of the PTY.
                self.pty.write(text.encode("utf-8", errors="replace"))
            except Exception as e:
                print(f"[ai_terminal] writer error: {e}\n{traceback.format_exc()}")
                if not self.pty.is_alive():
                    # The child is gone: every further keystroke would be
                    # discarded with nothing on screen to say so. Report once
                    # and stop the writer instead of swallowing input forever.
                    self._notify("input dropped, the terminal process is gone")
                    return
                self._notify("could not deliver input to the terminal: %s" % e)
                continue
            # Recording has its own queue/thread.  In particular, never wait
            # here on the recorder lock after the first arrow while later arrows are
            # queued for the PTY behind it. record=False (write_pty query
            # responses -- see _on_parser_write_pty) skips this: nobody
            # typed a DA/kitty-flags/size reply, and logging it as an "i"
            # event would make a replayed .cast show the terminal "typing"
            # escape sequences on its own.
            if record:
                self._input_cast_queue.put(text)

    def _cast(self, code, data):
        """Asciicast v3 event: [delta, code, data]. No-op when recording is
        off. Caller passes `data` already as the right Python type: str for
        "o"/"i"/"x", "{cols}x{rows}" for "r"."""
        rec = getattr(self, "_cast_recorder", None)
        if rec is None:
            return
        rec.write(code, data)

    def _read_loop(self):
        error = None
        try:
            if _is_broker_pty(self.pty) and self._reattach_bootstrap:
                self.pty.read(self._on_data, self._on_broker_replay_complete)
            else:
                self.pty.read(self._on_data)
        except Exception as e:
            error = e
            print(f"[ai_terminal] reader error: {e}\n{traceback.format_exc()}")
        finally:
            self._close_text_log()
            # A reader that died must not be reported as an ordinary exit, and
            # the tab must stay open so the reason remains readable. A
            # deliberate termination (self._expected_termination_reason) makes
            # read() return exactly the same way a real child death does --
            # CancelIoEx on our own handles -- so it needs its own message and
            # must not be treated as either case below.
            reason = self._expected_termination_reason
            if reason == "handoff":
                notice = "\n[detached -- session handed off, still running]\n"
            elif reason == "killed":
                notice = "\n[session killed -- transcript kept, tab not closed]\n"
            elif reason == "closed":
                # Rarely actually seen -- the view is usually already gone
                # by the time this async finally block runs -- kept for
                # the unusual ordering where it isn't yet.
                notice = "\n[closed -- session kept alive, still running]\n"
            elif error is None:
                notice = "\n[process exited]\n"
            else:
                notice = "\n[terminal read failed: %s]\n" % error
            sublime.set_timeout(lambda: _vwrite(self.view, notice), 0)
            if error is None and reason is None:
                # _close_tab_on_exit() reads Settings (via _all_profiles), which
                # is main-thread-only in the Sublime API -- this finally block
                # still runs on the PTY reader thread, so the check itself must
                # be deferred through set_timeout, not just the close that
                # follows it.
                sublime.set_timeout(self._maybe_close_dead_view, 0)

    def _maybe_close_dead_view(self):
        if _close_tab_on_exit(self.profile_name):
            sublime.set_timeout(
                self._close_dead_view, int(_CLOSE_TAB_ON_EXIT_DELAY * 1000)
            )

    def _close_dead_view(self):
        # The user may have already closed this tab by hand in the interim;
        # is_valid() guards against double-closing (or closing a view ID ST
        # has since recycled for something unrelated).
        if self.view.is_valid():
            self.view.close()

    def _on_data(self, data):
        if _DEBUG:
            _debug_log(data)
        self._last_output_at = time.time()
        text = self._decoder.decode(data)
        if getattr(self, "_resize_desynced", False):
            # Bytes can still drain while pty.kill() closes the handles, but
            # they cannot safely be interpreted against stale parser geometry.
            return
        with self._lock:
            try:
                if self._reattach_bootstrap:
                    if data:
                        self._bootstrap_got_bytes = True
                    self.parser.feed_bootstrap(text)
                else:
                    self.parser.feed(text)
            except Exception as e:
                # Losing the reader thread over one bad chunk would strand a
                # live child behind a dead-looking tab, so keep reading; the
                # traceback goes to the console once per terminal.
                if not self._feed_failed:
                    self._feed_failed = True
                    print("[ai_terminal] parser feed failed:\n%s"
                          % traceback.format_exc())
                    self._notify("terminal output could not be parsed: %s" % e)
            # Screen drops a scrollback callback that raised; report it here so
            # a silently stopped text log doesn't look like an empty session.
            retire_error = self.screen.retire_line_error
            if retire_error is not None:
                self.screen.retire_line_error = None
                print(f"[ai_terminal] scrollback callback failed: {retire_error}")
                self._notify("scrollback logging stopped: %s" % retire_error)
        if self._reattach_bootstrap:
            return
        # Recording patch: emit an asciicast v3 "o" (output) event for the
        # raw chunk. Logged once, here, at the stream layer -- not at
        # scroll-off -- so it is faithful to what Claude emitted and does NOT
        # duplicate on resume (a resume is a new session = new .cast file).
        # The decoder is incremental; log the decoded text so the .cast is
        # valid UTF-8 JSON (v3 wants str data, not bytes). Written outside
        # self._lock so the renderer isn't blocked on file I/O; the recorder
        # lock serializes against send_string/resize/kill writes.
        #
        # Filter out highly repetitive "Executing Hooks" status-bar repaints to
        # prevent .cast files from ballooning into hundreds of megabytes.
        if "executing hook" not in text.lower():
            self._cast("o", text)
        _schedule_render(self)

    def _on_broker_replay_complete(self):
        """Publish native grid and history after broker replay.

        Empty replay (no bytes before the boundary) still seeds the
        restored view text so a blank native terminal does not wipe
        readable Sublime-restored rows.
        """
        with self._lock:
            if not self._reattach_bootstrap:
                return
            if getattr(self, "_bootstrap_got_bytes", False):
                self.parser.finish_bootstrap()
            else:
                restored = getattr(self, "_restored_text", "") or ""
                if restored:
                    _seed_restored_history(self.screen, restored)
            self._reattach_bootstrap = False
        _schedule_render(self)

    def send_string(self, s, record=True):
        # A key command must do no I/O and acquire no recording locks on
        # Sublime's main plugin thread.  The ordered writer records and writes
        # this text in sequence. record=False for text nobody actually typed
        # (see _on_parser_write_pty) -- it still gets written to the pty, just
        # not logged as an input event.
        self._write_queue.put((s, record))

    def queue_input(self, text, add_newline=True):
        """Queue text to be written via send_string once this session has
        been idle (no PTY output) for _QUEUED_INPUT_IDLE_S, so it lands
        indistinguishable from a real typed-and-submitted line instead of
        landing mid-output or while a busy agent is still thinking.

        add_newline sends a trailing Enter as its own separate write after
        text, if text doesn't already end with one. Two real findings from
        live testing on 2026-09-09, not assumed:
        - The PTY wants "\r" for Enter, not "\n" (see the keypress path's
          "\r" if chars == "\n" else chars translation) -- a bare "\n" left
          PowerShell sitting at a ">>" continuation prompt instead of
          executing.
        - Enter must be a SEPARATE send_string call from the text, not
          appended to the same string. A CLI's multi-line input box (tested
          against this file's own hosting Claude Code session) accepted a
          combined "text\r" write as text-plus-a-soft-newline and did not
          submit; a second, standalone "\r" write immediately after did.
          Real keystrokes arrive as separate PTY writes even when fast, and
          apparently some apps' input handling depends on that separation,
          not just the bytes.

        Defensive getattr/setattr below: a plugin reload replaces class
        code but does not re-run __init__ on already-live _Terminal
        instances, so any session spawned before this method existed has
        neither attribute -- confirmed live via a real AttributeError on
        this file's own pre-reload hosting-tab instance, 2026-09-09.
        """
        if text.endswith("\n") or text.endswith("\r"):
            text = text[:-1]
            add_newline = True
        with self._lock:
            if not hasattr(self, "_pending_input"):
                self._pending_input = []
            self._pending_input.append((text, add_newline))
        _arm_pending_input_poll(self)

    def _on_parser_write_pty(self, data):
        # Called synchronously from the parser's write_pty callback, which
        # itself fires inside parser.feed() on the PTY reader thread (see
        # _on_data), while self._lock is held. Must not write to the pty
        # directly here -- that's a blocking Win32 WriteFile on a thread
        # that also owns the read loop. Queue through the same ordered
        # writer as real keystrokes instead. The response bytes are VT
        # control sequences (ESC, digits, ASCII letters, ST's ESC \\) --
        # always single-byte-clean, so decoding as latin-1 and letting
        # send_string's utf-8 encode round-trip them is lossless.
        self.send_string(data.decode("latin-1"), record=False)

    def resize(self, cols, rows):
        if cols == self._last_cols and rows == self._last_rows:
            return
        if getattr(self, "_resize_desynced", False):
            return
        with self._lock:
            # The reader also takes this lock, so resize-generated output
            # cannot be fed between changing the child and changing the
            # parser. A child rejection leaves every other state untouched.
            if not self.pty.resize(cols, rows):
                return
            try:
                if hasattr(self.parser, "resize"):
                    self.parser.resize(cols, rows)
                else:
                    self.screen.resize(cols, rows)
            except Exception as e:
                # The child accepted the geometry but the parser did not.
                # Continuing would interpret output against false dimensions.
                self._resize_desynced = True
                message = (
                    "terminal stopped after its parser rejected the applied "
                    "resize to %dx%d: %s" % (cols, rows, e)
                )
                print("[ai_terminal] %s" % message)
                self._notify(message)
                try:
                    self.pty.kill()
                except Exception as kill_error:
                    print("[ai_terminal] resize containment kill failed: %s" % kill_error)
                sublime.set_timeout(
                    lambda m=message: _vwrite(self.view, "\n[%s]\n" % m), 0
                )
                return
            self._last_cols, self._last_rows = cols, rows
        # Record and render only a size applied to both the child and parser.
        self._cast("r", f"{int(cols)}x{int(rows)}")
        # The engine rewraps on resize without marking the screen dirty, so
        # the scheduled render returned early and the rewrapped text waited
        # for the app's next output (measured live 2026-09-22: 1.76 s).
        self.screen.dirty = True
        _schedule_render(self)

    def snapshot(self):
        """Return the current screen as plain text (no colour). Used by any
        external caller that just wants the visible buffer; the renderer itself
        goes through render_cells() + _build_text_and_regions for colour."""
        with self._lock:
            rows, _cy, _cx = self.screen.render_cells()
        return "\n".join("".join(ch for ch, _ in row) for row in rows)

    def kill(self):
        # Drop any in-flight mouse hold so a restart doesn't inherit it.
        vid = self.view.id()
        try:
            _mouse_force_release(self, vid)
        except (KeyError, AttributeError):
            _MOUSE_HOLD.pop(vid, None)
        _MOUSE_LAST_CLICK.pop(vid, None)
        _hover_last_cell.pop(vid, None)
        self._last_mouse_cell = None
        self._vp_pan_accum = 0.0
        # Stop accepting queued input before closing the PTY. The daemon
        # writer may still be blocked inside WriteFile; closing the PTY below
        # releases that call without making Sublime's main thread wait for it.
        self._write_queue.put(None)
        self._input_cast_queue.put(None)
        # Recording patch: emit an "x" (exit) event and close the .cast
        # file so the recording ends cleanly. The stream-layer "o" events
        # already captured everything Claude emitted, so there's no need
        # for a [final screen] dump -- the visible grid's content is in
        # the stream.
        rec = getattr(self, "_cast_recorder", None)
        if rec is not None:
            rec.close()
        self._close_text_log()
        try:
            self.pty.kill()
        except Exception as e:
            print(f"[ai_terminal] kill error: {e}")
        # Free the native ghostty-vt resources this terminal owns (terminal,
        # render state, key encoder -- see GhosttyParser.close). Must not
        # race the reader thread's in-flight parser.feed(): freeing while
        # another thread is mid-call is a native use-after-free, not a
        # Python exception, so join it first. pty.kill() above already
        # closed the pseudoconsole handles, which should unblock the
        # reader's blocked ReadFile promptly; bounded so a reader that
        # somehow doesn't exit can't hang tab-close -- at the cost of
        # leaking (never crashing) in that case.
        reader = self._reader
        if reader is not None and reader.is_alive():
            reader.join(timeout=2.0)
        close = getattr(self.parser, "close", None)
        if close is not None:
            if reader is None or not reader.is_alive():
                with self._lock:
                    close()
            else:
                print(
                    "[ai_terminal] reader thread did not exit in time; "
                    "leaking native ghostty resources for this tab rather "
                    "than risk a use-after-free"
                )


# ─── view helpers ─────────────────────────────────────────────────────────────

_VIEW_NAME = "Ai"
_VIEW_SETTING = "ai_terminal_view"
_TAG_SETTING = "ai_logger"  # so panic_dialog / ClaudeSendTab still find this view

# Persistent bottom-of-tab Close Tab/Relaunch/Windows-Terminal toolbar for
# detachable sessions -- see _add_close_toolbar. Mouse-driven tab close never
# dispatches a plugin-visible command (sublimehq/sublime_text#1922), so a
# close-time confirmation dialog can't be relied on; these are always-
# available buttons instead. Re-anchored on every render frame, not just
# once at spawn/reattach -- a phantom's anchor region does not reliably track
# a growing buffer's end through repeated full-buffer view.replace() calls
# over a long session (confirmed live).
_CLOSE_TOOLBAR_PHANTOM_KEY = "ai_terminal_close_toolbar"

# Persisted (view.settings() survives an ST restart via the workspace session
# file) so a detachable profile's tab can reconnect to its still-running
# agent_broker.py session after Sublime restarts -- see _BrokerPty and
# _reattach_broker_view.
_BROKER_PIPE_SETTING = "ai_terminal_broker_pipe"
_BROKER_PROFILE_SETTING = "ai_terminal_broker_profile"
_BROKER_CWD_SETTING = "ai_terminal_broker_cwd"

# Friendly alias of the pipe name (same value) plus the child process's own
# pid, for external tools (sublime-mcp's get_sheets, AgentIDE) reading
# view.settings() directly instead of also having to open the broker
# registry file themselves. Pid is stamped in once the broker's registry
# record actually reports it -- see _stamp_broker_pid_when_known.
_SESSION_ID_SETTING = "ai_terminal_session_id"
_CHILD_PID_SETTING = "ai_terminal_child_pid"

# Sublime restores views before their final layout is necessarily available.
# Reattaching a detachable terminal during that window can measure an inactive
# tab as one row and immediately send that bogus geometry to the surviving
# broker.  Main-screen profiles deliberately pin their initial row count, so
# the later layout watcher cannot repair that mistake.  Keep reattachment
# separate from ordinary layout watching and require one confirming measure.
_BROKER_REATTACH_PENDING = set()
_BROKER_REATTACH_CANDIDATE = {}
_BROKER_REATTACH_CONFIRM_MS = 250
# Views whose named-pipe connection is currently being attempted by a worker.
# CreateFileW/WaitNamedPipeW may wait for several seconds on a stale broker;
# this set prevents activation/plugin reload from starting duplicate attempts.
_BROKER_CONNECTING = set()

# Marks detachable-broker views mid-whole-window-close so
# AiTerminalTabCloseInterceptor.on_window_command can tell that apart from a
# deliberate single-tab close and skip sending a profile's graceful
# tab_close_input during teardown (killing the window is not the moment to
# ask an agent to exit gracefully -- it's already going away).
_WINDOW_CLOSING_TERM_IDS = set()


class AiTerminalWindowCloseListener(sublime_plugin.EventListener):
    """Marks detachable-broker views so on_window_command
    (AiTerminalTabCloseInterceptor, below) can tell a window closing apart
    from a deliberate single-tab close. See _WINDOW_CLOSING_TERM_IDS."""

    def on_pre_close_window(self, window):
        for view in window.views():
            term = _Terminal.from_id(view.id())
            if term is not None and _is_broker_pty(term.pty):
                _WINDOW_CLOSING_TERM_IDS.add(view.id())


class AiTerminalNoopWindowCommand(sublime_plugin.WindowCommand):
    """Window-command sink used when a terminal consumes native tab-close."""

    def run(self):
        pass


class AiTerminalTabCloseInterceptor(sublime_plugin.EventListener):
    """Ask before closing any ai_terminal tab, and turn it into a graceful
    exit request for profiles that opt into one.

    Sublime's view ``on_close`` notification is too late to ask: the view has
    already been destroyed by the time it fires. Native tab close is
    dispatched as a window command first, so intercept that and substitute a
    no-op until confirmed.

    Originally this only fired for profiles with a configured
    ``tab_close_input`` (a graceful-exit string, e.g. Testing Agent) and
    silently let every other profile's tab close with no warning at all --
    including this coordinator's own hosting tab, which is exactly what got
    closed by mistake via a script's view.close() call on 2026-09-15 (see
    memory: closing-own-tab-via-sublime-mcp). ``view.close()`` bypasses
    window commands entirely -- same class of gap as mouse-X tab-close
    (sublimehq/sublime_text#1922) -- so this hook alone can't catch every
    close path; sublime-mcp's close tool now goes through
    window.run_command() specifically so it lands here instead.

    Every detachable ai_terminal tab now gets a plain yes/no gate regardless
    of profile; profiles with ``tab_close_input`` additionally get the
    graceful-exit flow (send the exit string, wait for the agent to quit on
    its own) once confirmed.
    """

    # "close" is what Ctrl+W and File > Close File actually dispatch (NOT
    # close_file -- that's only Ctrl+F4). Confirmed 2026-09-15 via
    # sublime.load_resource("Packages/Default/Default (Windows).sublime-keymap")
    # and Main.sublime-menu -- prior comments/memory in this codebase
    # claiming Ctrl+W fires close_file were wrong, and this hook had
    # consequently never actually fired for either Ctrl+W or the File menu
    # since it was written.
    _CLOSE_COMMANDS = {"close", "close_file", "close_by_index"}

    @staticmethod
    def _target_view(window, command_name, args):
        if command_name == "close_by_index":
            args = args or {}
            try:
                group = int(args["group"])
                index = int(args["index"])
            except (KeyError, TypeError, ValueError):
                print("[ai_terminal] tab-close target view lookup failed for args %r:\n%s"
                      % (args, traceback.format_exc()))
                return None
            # -1 is Sublime's own sentinel for "current" on both group and
            # index (this is what the stock Tab Context menu's "Close Tab"
            # entry actually sends) -- views_in_group(-1) is not "active
            # group" via the public API, so resolve the sentinel explicitly
            # rather than letting it silently fail to find anything.
            if group == -1:
                group = window.active_group()
            try:
                if index == -1:
                    return window.active_view_in_group(group)
                return window.views_in_group(group)[index]
            except (AttributeError, TypeError, ValueError, IndexError):
                print("[ai_terminal] tab-close target view lookup failed for args %r:\n%s"
                      % (args, traceback.format_exc()))
                return None
        return window.active_view()

    # Window commands that change every view's width or height without any
    # view setting changing, so _on_layout_setting_change never hears of them.
    _LAYOUT_COMMANDS = frozenset((
        "toggle_minimap", "toggle_side_bar", "toggle_status_bar",
        "toggle_tabs", "toggle_menu", "set_layout", "toggle_full_screen",
        "toggle_distraction_free",
    ))

    def on_post_window_command(self, window, command_name, args):
        """Ask each terminal's layout watcher to re-measure right away."""
        if command_name not in self._LAYOUT_COMMANDS:
            return
        with _term_lock():
            terms = list(_term_registry().values())
        for term in terms:
            if term._watcher is not None:
                term._watcher.request()

    def on_window_command(self, window, command_name, args):
        if command_name not in self._CLOSE_COMMANDS:
            return None
        view = self._target_view(window, command_name, args)
        if view is None or not view.settings().get(_VIEW_SETTING):
            return None
        term = _Terminal.from_id(view.id())
        if term is None or view.id() in _WINDOW_CLOSING_TERM_IDS:
            return None

        profile = _profile_settings(getattr(term, "profile_name", None))
        exit_input = profile.get("tab_close_input") if profile else None

        if isinstance(exit_input, str) and exit_input:
            if not getattr(term, "_tab_close_requested", False):
                confirmed = sublime.ok_cancel_dialog(
                    "End %s session?\n\n"
                    "The tab will remain open until the agent exits."
                    % (term.profile_name or "agent"),
                    "Exit Agent",
                    "Close Agent Session",
                )
                if not confirmed:
                    return ("ai_terminal_noop_window", {})
                term._tab_close_requested = True
                term.send_string(exit_input)
                sublime.status_message(
                    "Ai terminal: waiting for %s to exit"
                    % (term.profile_name or "process")
                )
            # Repeat close attempts while shutdown is pending stay swallowed
            # -- the eventual real close happens via the PTY EOF path, not
            # here -- so this always returns noop once requested, unlike the
            # plain-confirm branch below which resolves in a single pass.
            return ("ai_terminal_noop_window", {})

        # No configured graceful-exit command for this profile: block the
        # native window-command close outright, no dialog at all. Tried a
        # native sublime.yes_no_cancel_dialog here first (2026-09-15) -- it
        # works, but a native OS-modal dialog can lose focus and end up
        # hidden behind other windows on Windows while it blocks all of
        # Sublime, with zero in-app indication it's even pending (confirmed
        # live the same day; see memory:
        # ghostshell-close-dialog-can-hide-behind-windows). Ending or
        # detaching a session is reachable through controls this plugin
        # fully owns instead and that can't hide behind another window:
        # Tab Context menu / Command Palette's Close & Keep Alive / Kill
        # Session / End Session (AiTerminalCloseKeepAliveCommand /
        # AiTerminalKillSessionCommand / AiTerminalEndSessionCommand) --
        # an in-tab toolbar equivalent is the natural next step here.
        sublime.status_message(
            "Ai terminal: window close is disabled for ai_terminal tabs -- "
            "use this tab's Close & Keep Alive / Kill Session / End Session "
            "controls (Tab Context menu or Command Palette) instead"
        )
        return ("ai_terminal_noop_window", {})


def _vwrite(view, text):
    def _do(t=text):
        view.set_read_only(False)
        view.run_command("append", {"characters": t, "scroll_to_end": True})
    sublime.set_timeout(_do, 0)


class _LayoutWatcher:
    """Standard terminal-style layout watcher.

    Watches a single ai_terminal view for genuine geometry changes (window
    resize, gutter/line_numbers/fold_buttons/margin toggles, font changes)
    and resizes the PTY only when the measured (cols, rows) actually changes.

    Key design choices that prevent the resize<->replay oscillation bug:
      - We never auto-resize in response to PTY output.
      - We measure only the viewport; transient content-width fluctuations
        (scrollbars appearing/disappearing because of the TUI's own output)
        do not change the viewport size, so they do not trigger a resize.
      - Changes are debounced: rapid-fire layout events coalesce into one
        resize call after the viewport size has been stable for a short window.
      - We resize only if the new (cols, rows) differs from the last one we
        told the PTY.
    """

    _DEBOUNCE_MS = 150
    # Was 250ms forever-tick. Event-driven request() + 2s poll is enough;
    # PTY resize does not need sub-second polling when the user owns the tab.
    _POLL_MS = 2000
    # How many quick re-checks in a row _run may schedule to confirm a new
    # size before falling back to the poll (see _run).
    _MAX_FAST_CONFIRMS = 3

    def __init__(self, term):
        self.term = term
        self._fast_confirms = 0
        self._pending = False
        self._token = None
        self._last_measure = None
        self._candidate = None
        self._candidate_count = 0

    def request(self):
        """Request a resize check. Safe to call frequently; debounces."""
        if self._pending:
            return
        self._pending = True
        self._token = sublime.set_timeout(lambda: self._run(), self._DEBOUNCE_MS)

    def _run(self):
        self._pending = False
        view = self.term.view
        if not view or not view.is_valid():
            return
        # The console (and other bottom panels) steal vertical space from
        # every group in the window, shrinking this view's viewport without
        # the user ever touching the terminal's own layout. Resizing the PTY
        # to that transient size churns a full-screen redraw (duplicate
        # banners, TUI repaint) for no reason -- skip measuring while any
        # panel is showing; closing it restores the prior viewport, which
        # already matches term._last_cols/_last_rows, so no resize fires.
        try:
            window = view.window()
            if window is not None and window.active_panel():
                # A terminal living in a panel (see ai_terminal_toggle_panel)
                # is itself "the active panel" -- only skip the measure for
                # some OTHER panel (find/console/build output) stealing
                # vertical space, not our own.
                own_panel = self.term.panel_name
                mine = own_panel and window.active_panel() == "output." + own_panel
                if not mine:
                    return
        except AttributeError:
            print("[ai_terminal] layout watcher: active-panel check failed:\n%s"
                  % traceback.format_exc())
        try:
            size = _measure(view, profile_name=getattr(self.term, "profile_name", None))
        except Exception as e:
            print(f"[ai_terminal] layout measure error: {e}")
            return
        self._last_measure = size
        # Require the same candidate size on two consecutive polls before
        # acting on it. A resize can itself toggle the horizontal scrollbar
        # (see _measure's width comment), which shifts the viewport by one
        # column and produces a different reading next poll -- without this
        # debounce, two boundary sizes (e.g. 114/115 cols) chase each other
        # forever: resize -> scrollbar flips -> remeasure -> resize back ->
        # repeat, forcing the TUI to redraw on every tick (looks like a
        # frozen/flickering terminal). Requiring stability kills the loop;
        # worst case we just don't chase that last column.
        #
        # This must run every poll, even when size == the previous poll's
        # measurement -- that repeat IS the confirmation the candidate check
        # is waiting for. An earlier version returned early right here on
        # "size unchanged since last poll", which meant the second (and every
        # later) identical reading was swallowed before ever reaching the
        # candidate-count logic below, so candidate_count could never reach 2
        # once the size stabilized -- resize silently stopped firing whenever
        # the viewport was NOT actively fluctuating, i.e. the common case.
        if size == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = size
            self._candidate_count = 1
        if self._candidate_count < 2:
            # Confirm a new size _DEBOUNCE_MS from now instead of on the
            # next 2 s poll: waiting for the poll made a minimap toggle take
            # up to 4 s to resize (measured live 2026-09-22). Capped, so a
            # size that keeps flipping (the scrollbar case above) falls back
            # to the poll instead of re-checking every 150 ms forever.
            if (size != (self.term._last_cols, self.term._last_rows)
                    and getattr(self, "_fast_confirms", 0) < self._MAX_FAST_CONFIRMS):
                self._fast_confirms = getattr(self, "_fast_confirms", 0) + 1
                self.request()
            return
        self._fast_confirms = 0
        self._candidate = None
        self._candidate_count = 0
        cols, rows = size
        # 32↔33 (and 99→100 gutter) attractor: never grow by exactly one
        # column. See terminal.layout.accepted_cols and ai/TODO.md
        # "Status-line resize/rewrap loop".
        cols = _accepted_cols(self.term._last_cols, cols)
        # 47↔48 attractor: H-bar / int(h/lh)-1. A 1-row SIGWINCH dumps
        # the TUI transcript; ignore both directions. Real sash ≥2.
        rows = _accepted_rows(self.term._last_rows, rows)
        changed = (cols, rows) != (self.term._last_cols, self.term._last_rows)
        if changed:
            self.term.resize(cols, rows)
            print(f"[ai_terminal] resized PTY to {self.term._last_cols}x{self.term._last_rows}")


    def dispose(self):
        if self._token is not None:
            try:
                sublime.cancel_timeout(self._token)
            except (RuntimeError, AttributeError):
                print("[ai_terminal] layout watcher: cancel timer failed:\n%s"
                      % traceback.format_exc())
            self._token = None
        self._pending = False


# Lightweight periodic watcher: real window resizes and some setting toggles do
# not always fire add_on_change, so we poll the measured size every 250ms and
# resize only when it actually changes. This is NOT auto-resize-on-output: it
# is purely geometry polling.
_WATCHER_TOKEN = None


def _start_layout_watcher():
    global _WATCHER_TOKEN
    if _WATCHER_TOKEN is not None:
        return
    _layout_tick()


def _layout_tick():
    global _WATCHER_TOKEN
    _WATCHER_TOKEN = None
    try:
        with _term_lock():
            terms = list(_term_registry().values())
        for term in terms:
            if term._watcher is None:
                continue
            term._watcher.request()
    except Exception as e:
        print(f"[ai_terminal] layout watcher error: {e}")
    _WATCHER_TOKEN = sublime.set_timeout(_layout_tick, _LayoutWatcher._POLL_MS)


def _stop_layout_watcher():
    global _WATCHER_TOKEN
    if _WATCHER_TOKEN is not None:
        try:
            sublime.cancel_timeout(_WATCHER_TOKEN)
        except (RuntimeError, AttributeError):
            print("[ai_terminal] stop layout watcher: cancel timer failed:\n%s"
                  % traceback.format_exc())
        _WATCHER_TOKEN = None


def _next_ai_name(window, prefix=None):
    """Return a unique Ai tab name for the window: 'prefix', then 'prefix 2', ...
    Distinct view.name() per tab so send_to_view (and other name-based tools) can
    target a specific Ai tab instead of hitting the ambiguous 'Ai' every tab had
    when _VIEW_NAME was hardcoded."""
    used = set()
    for v in window.views():
        if v.settings().get(_VIEW_SETTING, False):
            used.add(v.name())
    pfx = prefix or _VIEW_NAME
    if pfx not in used:
        return pfx
    n = 2
    while f"{pfx} {n}" in used:
        n += 1
    return f"{pfx} {n}"


def _next_ai_panel_name(window, prefix=None):
    """Panel-mode counterpart to _next_ai_name: 'prefix', then 'prefix 2', ...
    unique among this window's currently-open output panels."""
    pfx = prefix or _VIEW_NAME
    if window.find_output_panel(pfx) is None:
        return pfx
    n = 2
    while window.find_output_panel(f"{pfx} {n}") is not None:
        n += 1
    return f"{pfx} {n}"


def _apply_terminal_view_settings(v, profile_name=None):
    """Settings/scheme shared by both the tab (new_file) and panel
    (get_output_panel) terminal views. Caller sets the name/scratch flag,
    which differ (or don't apply) between the two."""
    v.settings().set("word_wrap", False)
    v.settings().set("gutter", True)
    v.settings().set("line_numbers", True)
    v.settings().set("fold_buttons", True)
    # margin=0 on the terminal view: the right margin is "scrollable" in ST
    # (the horizontal scroll range grows 1px per 1px of margin), so any
    # nonzero margin shows up as a horizontal scrollbar. Terminals don't need
    # text padding anyway. See _measure for the width calc.
    v.settings().set("margin", 0)
    # Thin bar caret, not block: the ST caret is always visible now (see
    # _HOST_CARET_HEX) and under the user's own control, same as editing any
    # normal document -- a thin bar reads as a normal editing caret rather
    # than competing visually with the synthesized/reverse-video TUI cursor
    # glyph at the PTY's own live position.
    v.settings().set("block_caret", False)
    v.settings().set("caret_extra_width", 0)
    # draw_centered=False isolates the terminal from the user's global
    # preference. scroll_past_end must be True so two-finger trackpad gestures
    # keep generating scroll_lines even when the TUI framebuffer fits the
    # viewport — with False, ST fires scroll once then stops ("worked once,
    # then dead"). Visible bounce is prevented by always swallowing
    # scroll_lines and pinning the viewport, not by disabling scroll_past_end.
    # NOTE: is_widget=True was tried (matching Terminus) to stop ST's
    # on-activate viewport reposition, but it makes ST hide the main menu while
    # the terminal is focused -- unacceptable, so it is NOT set.
    v.settings().set("draw_centered", False)
    v.settings().set("scroll_past_end", True)
    v.settings().set(_VIEW_SETTING, True)
    v.settings().set(_TAG_SETTING, True)
    # Instant resize on gutter / line_numbers / fold_buttons / margin toggles.
    # add_on_change fires on the main thread right after the setting changes,
    # but viewport_extent() may not yet reflect the new gutter width (ST lays
    # out asynchronously), so defer the measure+resize to the next main-thread
    # tick. Without this, the poller catches the change up to 750ms later and
    # the TUI keeps the old column count (text gets truncated / scrollbars
    # appear) for that lag.
    vid = v.id()

    def _on_layout_setting_change():
        # Genuine windowing-layer trigger (gutter/line_numbers/fold_buttons/
        # margin toggle) -- ask the term's own _LayoutWatcher to check, same
        # debounced/stability-gated path the 250ms poll uses. This only saves
        # latency (reacts immediately instead of waiting up to 250ms for the
        # next tick); it does not bypass the oscillation guard, so it can't
        # reintroduce the resize/replay flood.
        with _term_lock():
            term = _term_registry().get(vid)
        if term is not None and term._watcher is not None:
            term._watcher.request()

    for _key in ("gutter", "line_numbers", "fold_buttons", "margin", "font_face", "font_size"):
        v.settings().add_on_change(_key, _on_layout_setting_change)
    # Dedicated colour scheme: defines the ai.fg/bg/fb.* scopes the renderer
    # maps cells to (see gen_color_scheme.py). Scoped to this view only, so the
    # rest of the editor keeps the user's theme. find_resources (plural) returns
    # the installed path; fall back to the canonical Packages/GhostShell path.
    try:
        hits = sublime.find_resources("ai_terminal.sublime-color-scheme")
        if hits:
            v.settings().set("color_scheme", hits[0])
        else:
            v.settings().set("color_scheme",
                             "Packages/GhostShell/ai_terminal.sublime-color-scheme")
    except (RuntimeError, AttributeError):
        v.settings().set("color_scheme",
                         "Packages/GhostShell/ai_terminal.sublime-color-scheme")
    # Per-profile font override, global fallback, then ST's own current font.
    # A profile's "font_face"/"font_size" wins; else the top-level setting of
    # the same name; else erase so the view just inherits whatever ST is
    # already using (no override at all) -- this is what lets one profile go
    # untouched (system font) while another opts into something distinct for
    # visual differentiation between agents.
    font_face = _setting_string("font_face", None, profile_name=profile_name)
    if font_face:
        v.settings().set("font_face", font_face)
    else:
        v.settings().erase("font_face")
    font_size = _setting_number("font_size", None, cast=float, profile_name=profile_name)
    if font_size:
        v.settings().set("font_size", font_size)
    else:
        v.settings().erase("font_size")
    # NOT read-only: on_text_command swallows insert/left_delete/right_delete/
    # move and forwards them to the PTY. Making the view read-only suppresses
    # keyboard `insert` before the listener fires, so real typing would do
    # nothing (only programmatic run_command("insert") bypasses the block).


def _terminal_view(window, name=None, profile_name=None):
    v = window.new_file()
    v.set_name(name or _next_ai_name(window))
    v.set_scratch(True)
    _apply_terminal_view_settings(v, profile_name=profile_name)
    return v


def _terminal_panel_view(window, panel_name, profile_name=None):
    """Panel-mode counterpart to _terminal_view. get_output_panel returns a
    cached view keyed by name -- reused as-is across toggles (the caller
    forces a full render right after, so stale panel content never shows)."""
    v = window.get_output_panel(panel_name)
    _apply_terminal_view_settings(v, profile_name=profile_name)
    return v


def _dont_close_window_when_empty(func):
    """Closing the last regular tab (moving it into a panel) must not take
    the whole window with it. Mirrors Terminus's decorator of the same
    purpose; the setting is restored a moment later so it doesn't leak into
    the user's normal editing session."""
    def f(*args, **kwargs):
        s = sublime.load_settings("Preferences.sublime-settings")
        prev = s.get("close_windows_when_empty")
        s.set("close_windows_when_empty", False)
        try:
            func(*args, **kwargs)
        finally:
            if prev:
                sublime.set_timeout(
                    lambda: s.set("close_windows_when_empty", prev), 1000
                )
    return f


def _forget_view_mouse_state(vid):
    """Drop per-view mouse/hover bookkeeping keyed by a view id that's about
    to stop existing. Mirrors _Terminal.kill's cleanup; migration doesn't
    call kill() (the PTY survives), so this has to happen separately."""
    _MOUSE_HOLD.pop(vid, None)
    _MOUSE_LAST_CLICK.pop(vid, None)
    _hover_last_cell.pop(vid, None)


@_dont_close_window_when_empty
def _migrate_terminal_view(term, new_view):
    """Move a live terminal (PTY + Screen keep running) from its current
    view to new_view, then close the old one.

    Unlike Terminus, we don't capture/replay the old view's text: we own
    the Screen (with scrollback) already, so forcing term.screen.dirty and
    re-rendering paints new_view from the single source of truth. The
    registry is re-keyed to new_view's id BEFORE the old view closes, so
    AiTerminalViewListener.on_close's lookup misses on the old id and does
    nothing -- no separate "don't kill the PTY" flag needed.
    """
    old_view = term.view
    old_vid = old_view.id()
    new_vid = new_view.id()

    with _term_lock():
        _term_registry().pop(old_vid, None)
        _term_registry()[new_vid] = term

    term.view = new_view
    _add_close_toolbar(term)
    term.screen.dirty = True
    # _do_render's skip_all fast path compares against these caches to avoid
    # repainting unchanged content -- but they were populated by the paint
    # into old_view. The new_view has never been painted, so without
    # invalidating them here, the identical signature makes _do_render skip
    # the paint entirely and the new view stays blank.
    term._last_plain_sig = None
    term._last_render_text = None
    term._last_caret_off = None
    # AiTerminalRenderCommand's auto-follow heuristic compares the view's
    # current viewport y against term._last_vp_y to detect "user scrolled
    # away" (see its "vp[1] < term._last_vp_y - lh*1.5" check). new_view
    # always starts at vp (0, 0) while _last_vp_y still holds old_view's
    # scroll position -- without resetting it here, that comparison reads as
    # a scroll-away on the very first render and latches auto_follow False,
    # stranding the view at the top instead of following to the cursor.
    _set_auto_follow(term, True)
    term._last_vp_y = 0.0
    term._live_anchor_y = 0.0
    _schedule_render(term)

    _forget_view_mouse_state(old_vid)
    if old_view.is_valid():
        old_view.close()


def _measure(view, profile_name=None):
    ex = view.viewport_extent()
    cw = view.em_width() or 7.0
    lh = view.line_height() or 18.0
    # Width math: three things eat horizontal space -- the gutter (line
    # numbers), the fold buttons, and the `margin` setting. viewport_extent
    # already excludes the gutter + fold buttons (they live left of the
    # viewport -- confirmed: cols drops when line_numbers/fold_buttons turn
    # on). `margin` is padding INSIDE the viewport (left + right of the text),
    # so it must be subtracted here, otherwise cols is overestimated by the
    # margin. margin may be an int (all sides) or [left, top, right, bottom].
    # The terminal view sets margin=0 (see _terminal_view) so this is normally
    # a no-op, but keep it for safety in case a setting toggles margin back on.
    margin = view.settings().get("margin", 0) or 0
    if isinstance(margin, (list, tuple)):
        ml = margin[0] if len(margin) > 0 else 0
        mr = margin[2] if len(margin) > 2 else ml
    else:
        ml = mr = margin
    usable_w = ex[0] - ml - mr
    # ST's native gutter (excluded from viewport_extent above) widens by one
    # digit every time total_lines crosses 10**n (999->1000, 9999->10000...).
    # During an active full-history replay that crossing happens repeatedly
    # as lines are added/rebuilt, so the *real* gutter digit count -- and
    # therefore usable_w and cols -- genuinely changes mid-replay. That is
    # the resize<->replay oscillation (ai/TODO.md "4-digit gutter reserve").
    # Compensate here so our column math always behaves as if the gutter
    # were reserved at the digit width of this profile's scrollback cap
    # (scrollback_history_size -- the buffer's real ceiling, so the digit
    # count it implies never changes once reached), regardless of the
    # buffer's actual current line count. ST's own gutter still resizes for
    # real, but that no longer moves `cols`, so it can't retrigger a PTY
    # resize mid-replay.
    if view.settings().get("line_numbers", True):
        total_lines = view.rowcol(view.size())[0] + 1
        usable_w -= _gutter_digit_delta(total_lines, _scrollback_size(profile_name)) * cw
    # The ~4 blank columns after wrapped text are a bug in the *view*, not
    # a feature. They exist because ST's layout width is (line + ~3) cells
    # (EOL caret + padding), not the glyph count. If we report a wider PTY
    # size, the TUI rewraps to that size (visible when you resize a tab and
    # scroll back). Those longer lines then trip the H-scrollbar. The bar
    # changes viewport_extent; we report width again. In no-alt-screen
    # Claude that is a full conversation replay (~1000 lines) which repeats
    # ~10 times. Do not "fix" the gap by sending more columns.
    #
    # A real fix has to make ST stop charging those extra cells (or hide
    # the H-bar) so we can report a width that fills the view *and* still
    # have no bar after the TUI rewraps. Until then the 4.0*cw subtract
    # is a stopper, not a solution.
    #
    # word_wrap=True hides the H-bar but any line at the wrap threshold
    # (box-drawing slightly wider than em_width) becomes an extra visual
    # row and a vertical scrollbar.
    mn, mx = _cols_bounds()
    cols = max(mn, int((usable_w - 4.0 * cw) / cw))
    if mx is not None:
        cols = min(mx, cols)
    # Subtract 1 row for a vertical safety margin: int(ex[1]/lh) fills the
    # viewport EXACTLY (content_h == viewport_h), and ST shows a "solid"
    # vertical scrollbar (thumb fills the track, won't move) whenever
    # layout_extent >= viewport -- even when they're exactly equal. Whether
    # that happens depends on where the TUI's current frame lands on the row
    # boundary, so the bar appeared intermittently ("sometimes solid, won't
    # move"). The -1 guarantees content_h < viewport_h by one line, so no
    # vertical scrollbar ever appears.
    #
    # The blank line(s) sometimes visible at the top of the view are NOT from
    # this calc: ST itself reserves a 1-line top margin, and Claude's TUI
    # independently leaves text line 1 (and sometimes line 2) blank. Those
    # stack; neither is ai_terminal's doing.
    rows = max(_min_rows(), int(ex[1] / lh) - 1)
    return cols, rows


# ─── debounced renderer ──────────────────────────────────────────────────────

# Match Terminus renderer cadence (intermission period=0.03s). Faster full
# replaces starve ST key dispatch on Windows; slower feels laggy vs Terminus.
# Debounce after PTY output (was 30ms). Still event-armed, not a free-running
# paint loop; 100ms cuts main-thread churn on bursty TUIs.
_RENDER_MS = 100
_RENDER_MIN_INTERVAL_MS = 30

# How long a session must produce no PTY output before queue_input() will
# drain a queued line into it. Long enough that a brief pause mid-output
# (e.g. between two lines of a streaming response) doesn't look like "done
# and waiting"; short enough that a real return-to-prompt is not felt as a
# delay by whatever queued the input.
_QUEUED_INPUT_IDLE_S = 3.0
_PENDING_INPUT_POLL_MS = 250

# Gap between the queued text's own pty.write() and its trailing Enter's.
# Must be enough real wall-clock separation that the two land as genuinely
# separate stdin chunks/ticks on the child's side, not just two Python-level
# calls -- see the comment at the call site (_check_pending_input) for the
# specific Claude-Code-side race this avoids. 150ms comfortably clears
# Claude Code's own PASTE_COMPLETION_TIMEOUT_MS (100ms, confirmed by reading
# its usePasteHandler.ts) in case that path is ever involved too.
_QUEUED_INPUT_ENTER_DELAY_MS = 150


def _arm_pending_input_poll(term):
    """Start (if not already running) a self-rescheduling poll that drains
    term._pending_input once the session goes idle. Only runs while there is
    something queued -- queue_input() re-arms it each time it appends, and
    the poll disarms itself once the queue is empty so idle sessions with
    nothing queued cost nothing.

    getattr, not term._pending_input_poll_armed directly: a session spawned
    before queue_input existed has neither attribute (reload replaces class
    code, not already-live instance state) -- confirmed live on this file's
    own hosting-tab instance, 2026-09-09.
    """
    if getattr(term, "_pending_input_poll_armed", False):
        return
    term._pending_input_poll_armed = True
    sublime.set_timeout(lambda: _check_pending_input(term), _PENDING_INPUT_POLL_MS)


def _check_pending_input(term):
    view = getattr(term, "view", None)
    if not view or not view.is_valid():
        term._pending_input_poll_armed = False
        return
    item = None
    with term._lock:
        if not getattr(term, "_pending_input", None):
            term._pending_input_poll_armed = False
            return
        if time.time() - term._last_output_at >= _QUEUED_INPUT_IDLE_S:
            item = term._pending_input.pop(0)
    if item is not None:
        text, add_newline = item
        term.send_string(text)
        if add_newline:
            # A separate, delayed write -- not appended to text -- because
            # a same-chunk text+Enter write reproduces a real, documented
            # race in Claude Code's own input layer: usePasteHandler.ts
            # (in the app, not this file) says plainly that when a paste
            # and a following Enter arrive in the same stdin chunk, "both
            # wrappedOnInput calls run in the same discreteUpdates batch
            # before React commits -- the second call reads stale state...
            # if that key is Enter, it submits the old input and the paste
            # is lost." One pty.write() call is one chunk from the child's
            # side; two separate calls, given real separation, are two
            # ticks with committed state in between. Confirmed live +
            # against that source on 2026-09-09/10, not assumed.
            sublime.set_timeout(
                lambda t=term: t.send_string("\r"), _QUEUED_INPUT_ENTER_DELAY_MS
            )
    sublime.set_timeout(lambda: _check_pending_input(term), _PENDING_INPUT_POLL_MS)

# CSI ?2026h / CSI ?2026l -- DECSET/DECRST "synchronized output" (mode 2026).
# A level, not a stack: some apps (Grok --minimal and its full TUI both,
# confirmed live 2026-08-15 via a raw pre-decode ReadFile capture -- 136 "h"
# to 68 "l" in one session, in a perfectly regular h,h,l,h,h,l,... order)
# send a redundant "h" while already open, which is a spec-legal no-op for a
# boolean DECSET mode. Painting mid-batch shows a genuinely incomplete frame
# that then gets corrected once "l" arrives -- the write-then-retract
# stutter. term.screen.sync_output is the native-backed level (see
# GhosttyParser._sync); _do_render defers while it's set.


def _schedule_render(term, delay_ms=None):
    """Arm a paint from PTY/screen dirty (Terminus-style: no pre-PTY caret)."""
    if term._render_pending:
        # Mark that more work arrived while armed; _do_render re-arms if dirty.
        term._render_coalesce = True
        return
    term._render_pending = True
    term._render_coalesce = False
    delay = _RENDER_MS if delay_ms is None else max(0, int(delay_ms))
    last = getattr(term, "_last_render_mono", 0.0) or 0.0
    if last:
        # Cap paint rate so burst typing cannot schedule a full replace every
        # key once the previous frame completes.
        elapsed_ms = (time.monotonic() - last) * 1000.0
        if elapsed_ms < _RENDER_MIN_INTERVAL_MS:
            delay = max(delay, int(_RENDER_MIN_INTERVAL_MS - elapsed_ms))
    sublime.set_timeout(lambda: _do_render(term), delay)


def _rearm_if_dirty(term):
    """Arm another frame when keys/PTY dirtied the screen while we painted.

    Clearing the coalesce flag even when nothing is dirty is what keeps a
    stalled frame (Grok often goes silent for seconds) from staying armed.
    """
    with term._lock:
        dirty = term.screen.dirty
    if dirty or getattr(term, "_render_coalesce", False):
        term._render_coalesce = False
        if dirty:
            _schedule_render(term)


def _plain_cells_signature(rows):
    """Stable content fingerprint before host-cursor paint (no ST deps)."""
    if not rows:
        return ""
    # Join chars only — attrs changes still need a full colour rebuild, so
    # include a coarse attr token per cell. Cheap string, not a hash object.
    parts = []
    for row in rows:
        for ch, attr in row:
            parts.append(ch)
            if attr:
                parts.append("\x00")
                parts.append(str(int(attr)))
        parts.append("\n")
    return "".join(parts)


def _clear_view_selection(view, term=None):
    """Collapse to a single empty caret at the start of the buffer.

    When term is given, guarded by term._in_render so on_selection_modified
    does not mistake this internal mutation for a user gesture and latch
    term._user_owns_caret -- this runs from _do_render/_selection_paint_
    blocked, outside AiTerminalRenderCommand's own _in_render window.
    """
    prev = None
    if term is not None:
        prev = getattr(term, "_in_render", False)
        term._in_render = True
    try:
        sel = view.sel()
        sel.clear()
        sel.add(sublime.Region(0))
    except (RuntimeError, AttributeError):
        print("[ai_terminal] clear view selection failed:\n%s" % traceback.format_exc())
    finally:
        if term is not None:
            term._in_render = prev


def _text_is_pad_only(text):
    """True when text is only host pad / whitespace / host cursor block █."""
    if text is None:
        return True
    return sum(1 for c in text if c not in " \n\t\r\u2588") < 8


def _view_lags_screen(term):
    """True when ST still shows the empty host-pad frame (or never painted).

    Grok can fill the PTY screen while the view stays on the first pad-only
    paint if a selection/guard aborts later frames. Never treat that as a
    user copy-selection worth freezing for.
    """
    last = getattr(term, "_last_render_text", None)
    if last is None:
        return True
    return _text_is_pad_only(last)


def _selection_is_spurious(view, term):
    """True for selections that should not freeze TUI paints.

    Copy-first mode + an empty host-pad frame (or the tiny first paint before
    Grok draws) makes a full-buffer drag select Region(0, size) trivial. That
    selection then trips _selection_paint_blocked forever — screen keeps the
    real TUI, the ST view stays blank. Detect pad-only / never-drawn frames.
    """
    try:
        sels = list(view.sel())
    except (RuntimeError, AttributeError):
        print("[ai_terminal] selection-spurious check: view.sel() failed:\n%s"
              % traceback.format_exc())
        return False
    if not sels or all(s.empty() for s in sels):
        return False
    # Only the "selected the whole (tiny) buffer" case is treated as spurious.
    if len(sels) != 1:
        return False
    s0 = sels[0]
    size = view.size()
    if size <= 0 or s0.begin() != 0 or s0.end() != size:
        return False
    # Real Grok/Claude frames are thousands of chars; pad+cursor first paint
    # is ~2 * HOST_SCROLL_PAD + a few rows (often ~60).
    tiny = size < max(120, int(term.screen.rows) + int(term.screen.cols))
    if tiny:
        return True
    try:
        text = view.substr(sublime.Region(0, size))
    except (RuntimeError, AttributeError):
        print("[ai_terminal] selection-spurious check: view.substr() failed:\n%s"
              % traceback.format_exc())
        return True
    return _text_is_pad_only(text)


_SELECTION_PAINT_BLOCK_MAX_S = 2.5


def _selection_paint_blocked(view, term):
    """True when a full paint would destroy an in-progress ST text selection.

    Live response/prompt rows dirty the screen every frame; thinking text is
    often static. Without a guard, the first mousedown (still empty sel) loses
    to a 30ms paint that sel.clear()+caret-pins — selection never sticks
    except on quiet regions (e.g. finished thinking blocks).

    Must NOT freeze forever on a full-buffer select of the empty host pad
    (Grok trust modal / first frame under copy-first): clear that selection
    and allow paint. First successful paint is also never blocked. While the
    ST view still lags the PTY (pad-only last paint), never block either.
    """
    # Pad-only / never-painted view: always paint. Clear any accidental full
    # select so copy-first drag on the empty frame cannot freeze forever.
    if _view_lags_screen(term):
        try:
            if any(not s.empty() for s in view.sel()):
                _clear_view_selection(view, term)
        except (RuntimeError, AttributeError):
            _clear_view_selection(view, term)
        term._st_select_guard_until = 0.0
        term._paint_block_since = None
        return False

    if _selection_is_spurious(view, term):
        _clear_view_selection(view, term)
        term._st_select_guard_until = 0.0
        term._paint_block_since = None
        return False

    try:
        has_sel = any(not s.empty() for s in view.sel())
    except (RuntimeError, AttributeError):
        has_sel = False
    if has_sel:
        now = time.monotonic()
        since = getattr(term, "_paint_block_since", None)
        if since is None:
            term._paint_block_since = now
            return True
        if now - since < _SELECTION_PAINT_BLOCK_MAX_S:
            return True
        # Blocked too long: a stale/abandoned selection must not freeze the
        # view forever -- keystrokes keep reaching the PTY while blocked, so
        # an unbounded block makes the view silently fall behind until
        # something happens to collapse the selection (see ai/TODO.md,
        # "Debug instrumentation baton"). Let the next paint through; the
        # full-buffer replace will naturally take the selection with it.
        term._paint_block_since = None
        return False
    term._paint_block_since = None
    guard = float(getattr(term, "_st_select_guard_until", 0.0) or 0.0)
    return guard > 0.0 and time.monotonic() < guard


def _arm_st_select_guard(term, seconds=1.25):
    """Block full paints briefly so drag_select can establish a non-empty range."""
    until = time.monotonic() + float(seconds)
    prev = float(getattr(term, "_st_select_guard_until", 0.0) or 0.0)
    if until > prev:
        term._st_select_guard_until = until


def _maybe_apply_osc_title(term):
    """Rename the ST tab to the app's OSC 0/2 title, if opted in.

    Runs every render tick (main thread, already-throttled by
    _schedule_render) rather than per PTY chunk on the reader thread --
    view.set_name() is main-thread-only Sublime API.
    """
    get_title = getattr(term.parser, "get_title", None)
    if get_title is None:
        return
    title = get_title()
    if title == term._applied_osc_title:
        return
    term._applied_osc_title = title
    if title and _osc_title_enabled(term):
        term.view.set_name(title)


def _log_painted_tab(term, text):
    """Write newly visible tab lines. `text` is what this paint put on the view."""
    log = getattr(term, "_text_log", None)
    if log is None or log.file is None:
        return
    try:
        painted = text or ""
        log.observe(
            painted.splitlines(), trailing_newline=painted.endswith("\n")
        )
    except Exception as e:
        print("[ai_terminal] text log write failed:\n%s" % traceback.format_exc())
        term._notify("text log disabled after write failure: %s" % e)
        term.screen.on_retire_line = None
        term._text_log.close()


def _update_debug_status(term):
    """Live status-bar readout of scroll/follow state (2026-08-27).

    Gated by debug_status_bar_enabled (ai_terminal.sublime-settings,
    default off) -- a live meter you can glance at while it happens,
    per a live user report that console debug prints ("log output") are
    a firehose (every keystroke/mouse move) with no way to correlate them
    to what's on screen, unlike Ghostty's Inspector overlay. view.set_status
    is the cheap ST-native equivalent: persistent status-bar text, no
    separate pane/overlay to build.
    """
    view = getattr(term, "view", None)
    if view is None or not view.is_valid():
        return
    if not _setting_bool("debug_status_bar_enabled", False, profile_name=_term_profile_name(term)):
        return
    try:
        follow = bool(getattr(term, "_auto_follow", True))
        tui = _tui_like(term)
        cols = getattr(term, "_last_cols", "?")
        rows = getattr(term, "_last_rows", "?")
        vp_y = view.viewport_position()[1]
        view.set_status(
            "ai_terminal_debug",
            f"ai_terminal: follow={follow} tui={tui} cols×rows={cols}×{rows} vp_y={vp_y:.0f}",
        )
    except (RuntimeError, AttributeError):
        print("[ai_terminal] update debug status failed:\n%s" % traceback.format_exc())


def _do_render(term):
    view = term.view
    if not view or not view.is_valid():
        term._render_pending = False
        return
    # Defer while selecting/copying: full-buffer replace + caret re-pin wipes it.
    # Poll until selection clears and the post-drag guard expires.
    if _selection_paint_blocked(view, term):
        sublime.set_timeout(lambda: _do_render(term), _RENDER_MS)
        return  # leave _render_pending True so _schedule_render doesn't double-arm
    # Defer while the app is mid CSI ?2026h/l ("synchronized output") batch:
    # painting now would show a genuinely incomplete frame that the next
    # chunk corrects a moment later -- the write-then-retract stutter.
    # term.screen.sync_output is native-backed ground truth (queried fresh
    # after every feed), not a Python-tracked flag, so it can't be forced
    # false the way a regex-tracked one could -- _sync_defer_forced is this
    # render path's own separate "stop waiting" latch for the capped safety
    # valve below. Capped: a closing "l" that never arrives (crashed
    # mid-batch, a client that forgets it) must not freeze the tab forever.
    if not term.screen.sync_output:
        term._sync_defer_started = None
        term._sync_defer_forced = False
    elif not term._sync_defer_forced:
        started = getattr(term, "_sync_defer_started", None) or time.monotonic()
        term._sync_defer_started = started
        if time.monotonic() - started < 0.5:
            sublime.set_timeout(lambda: _do_render(term), _RENDER_MS)
            return  # leave _render_pending True, same as the selection case
        term._sync_defer_forced = True  # safety valve: stop waiting, paint
    term._render_pending = False
    _maybe_apply_osc_title(term)
    # Host cursor: ST caret stays invisible when the app paints reverse-video
    # (Claude). When it does not (Grok / shells), paint_host_cursor puts a
    # white █ on the blank insertion cell (display-only). Caret row must come
    # from adjust_display_caret — Grok's live `│ >` row, not a history `>`.
    #
    # 2026-08-27 (Terminus-style rewrite, stage 1, ai/TODO.md /
    # CURSOR_SYSTEM_HISTORY.md item 2) -- NOT YET the default anywhere live:
    # caret_footer_pinning_enabled's False branch (raw hardware cursor, no
    # remap) is the Terminus-equivalent target for this whole gate, and is
    # the confirmed fix for the multi-line cursor bug / stale-prompt-lock
    # theory. But ai_terminal.sublime-settings itself documents a specific,
    # unreconciled 2026-08-21 report of real scrollback content loss ("300
    # lines missing, still shrinking") when this gate was previously
    # disabled, attributed to trim_display_rows. Investigated 2026-08-27:
    # trim_display_rows hasn't changed since before that report (git log),
    # and tests/test_trim_display.py's test_content_below_cursor_is_kept
    # proves it cannot drop non-blank content regardless of cursor position
    # (last_nb is computed independent of cy) -- nor does scrollback
    # eviction (screen.py) or native scrollback sync (ghostty_engine.py)
    # depend on cursor row at all. Strong evidence this is already safe, but
    # not fully reconciled with the original report, and this exact code
    # backs two live, in-progress conversations (this one and a concurrent
    # Codex session) that share this same loaded plugin process -- a reload
    # affects both. Per explicit user direction: do NOT flip this gate's
    # default; only the "Testing Agent" mock profile
    # (ai_terminal.sublime-settings' per-profile override, all five gates
    # false) may exercise the unpinned path until this is live-verified in
    # a genuinely separate process.
    with term._lock:
        if not term.screen.dirty:
            return
        rows, cy, cx = term.screen.render_cells()
        if _setting_bool("caret_footer_pinning_enabled", False, profile_name=_term_profile_name(term)):
            cy, cx = _adjust_display_caret(term.screen, cy, cx)
        rows = _pad_row_for_caret(rows, cy, cx)
        # The Screen holds the tab's full row count, so a plain shell's mostly-
        # blank grid isn't reflowed just because there's less real content
        # yet. Rendering all of it puts a wall of blank lines below the
        # cursor. Trim trailing blanks; a cursor parked two or more rows
        # below content (Claude last-row CUP + overflow \\n) is not kept.
        # Empty prompt on the next line is. See trim_display_rows.
        trimmed = _trim_display_rows(rows, cy)
        if _setting_bool("steady_screen_height_enabled", True):
            rows = _keep_screen_height_steady(term, rows, trimmed)
        else:
            rows = trimmed
        # Cursor visibility/shape captured under the same lock as rows/cy/cx
        # below -- reading them after releasing the lock let a concurrent
        # parser feed change cursor state between the grid snapshot above and
        # the cursor read, pairing one frame's grid with another frame's
        # cursor visibility/shape.
        cursor_visible = term.screen.cursor_visible
        cursor_shape = term.screen.cursor_shape
        # Clear under the lock so a concurrent parser feed cannot set dirty
        # then have us wipe it without painting that feed.
        term.screen.dirty = False
    plain_sig = _plain_cells_signature(rows)
    # Bisection gate (ai_terminal.sublime-settings): when disabled, rely
    # solely on the real ST caret + whatever reverse-video the app itself
    # already sends, Terminus-style -- see settings comment.
    if cursor_visible and _setting_bool("host_cursor_paint_enabled", False, profile_name=_term_profile_name(term)):
        rows, _host_painted = _paint_host_cursor(rows, cy, cx, shape=cursor_shape)
    else:
        # App hid the real cursor (DECTCEM off, ESC[?25l) -- fullscreen TUIs
        # (Textual, ratatui, curses) do this and draw their own focus/
        # highlight styling instead. Painting a synthetic block here chases
        # the raw last-write cell around the screen on every redraw (visible
        # as a flickering cursor "popping up all over").
        _host_painted = False
    text, regions = _build_text_and_regions(rows)
    caret_off = _cursor_text_offset(rows, cy, cx)
    # Host-only pads above+below: trackpad can pan both ways. Shift colour
    # region offsets and caret by the top pad length (newlines only).
    top_pad_chars = _HOST_SCROLL_PAD_LINES  # "\n" * N → N chars
    if top_pad_chars and regions:
        regions = [
            (b + top_pad_chars, e + top_pad_chars, scope)
            for (b, e, scope) in regions
        ]
    if caret_off is not None:
        caret_off = caret_off + top_pad_chars
    text = _append_host_scroll_pad(text)
    _log_painted_tab(term, text)
    # Drop any leftover HTML host-cursor phantoms from the prior experiment.
    _clear_host_cursor_phantom(view)

    prev_plain = getattr(term, "_last_plain_sig", None)
    prev_text = getattr(term, "_last_render_text", None)
    prev_caret = getattr(term, "_last_caret_off", None)
    caret_now = caret_off if caret_off is not None else -1
    # Burst typing / L-R cursor: PTY cells unchanged (plain_sig stable); only
    # host █ / reverse highlight + ST selection move. Mid-line reverse moves
    # keep the *text* identical — still a fast frame (regions + selection).
    # Bisection gate (ai_terminal.sublime-settings): off = always full-buffer
    # replace, no partial diff-patching -- see settings comment.
    fast_caret = (
        _setting_bool("fast_caret_patch_enabled", False, profile_name=_term_profile_name(term))
        and prev_plain is not None
        and prev_plain == plain_sig
        and prev_text is not None
        and (prev_text != text or prev_caret != caret_now)
    )
    skip_all = (
        prev_plain is not None
        and prev_plain == plain_sig
        and prev_text is not None
        and prev_text == text
        and prev_caret == caret_now
    )
    if skip_all:
        term._last_render_mono = time.monotonic()
        _rearm_if_dirty(term)
        return

    # Absolute caret offset (with top pad) so mid-line typing stays put.
    # Do not pass prev_text through command args (ST JSON-serializes them;
    # duplicating a full TUI buffer per key would reintroduce main-thread lag).
    # AiTerminalRenderCommand diffs against the live view when fast_caret.
    view.run_command(
        "ai_terminal_render",
        {
            "text": text,
            "cursor": [cy, cx],
            "cursor_offset": caret_off if caret_off is not None else -1,
            "regions": regions,
            "fast_caret": bool(fast_caret),
        },
    )
    term._last_plain_sig = plain_sig
    term._last_render_text = text
    term._last_caret_off = caret_off if caret_off is not None else -1
    term._last_render_mono = time.monotonic()
    _add_close_toolbar(term)
    _update_debug_status(term)
    _rearm_if_dirty(term)


def _build_text_and_regions(rows):
    """Flatten structured rows into the view text + colour regions.

    Delegates layout to the pure core; uses _scope_for so new scopes still
    get registered into the dynamic color scheme.
    """
    return _build_text_and_regions_pure(rows, scope_for=_scope_for)


# Per-view set of colour region keys added last frame, so we can erase stale
# scopes (whose cells scrolled away or changed attr) on the next render.
_LAST_COLOR_KEYS = {}
_COLOR_KEY_PREFIX = "ai_term_c_"
# Legacy key from the HTML-phantom experiment; still erased so old sessions
# do not keep an inserted grey cell after upgrade.
_HOST_CURSOR_PHANTOM = "ai_term_host_cursor"


def _clear_host_cursor_phantom(view):
    try:
        view.erase_phantoms(_HOST_CURSOR_PHANTOM)
    except (RuntimeError, AttributeError):
        print("[ai_terminal] clear host-cursor phantom failed:\n%s" % traceback.format_exc())


def _apply_color_regions(view, regs):
    """Group regions by scope and add them; erase any scope keys we added last
    frame but did not re-add this frame, so stale colour doesn't linger.

    Host cursor is applied last so its fill wins ST's undefined region z-order.
    """
    by_scope = {}
    host_rs = []
    for begin, end, scope in regs:
        if scope == _HOST_CURSOR_SCOPE:
            host_rs.append(sublime.Region(begin, end))
            continue
        by_scope.setdefault(scope, []).append(sublime.Region(begin, end))
    used = set()
    for scope, rs in by_scope.items():
        key = _COLOR_KEY_PREFIX + scope
        # The scheme gives every ai.fb.* scope a solid #000001 background
        # (off-by-one from the view's #000000 global bg -- ST collapses a rule
        # bg that EQUALS the global bg to None, which re-triggers the swap; so
        # #000001, visually indistinguishable from pure black, is used) plus
        # the text colour as foreground. ST's add_regions only colours the
        # TEXT when the scope defines BOTH fg and a SOLID bg; with only fg it
        # swaps, painting the fg as the fill and leaving the text default. So
        # we keep the fill (DRAW_NO_OUTLINE, no DRAW_NO_FILL): the #000001 fill
        # is invisible and the foreground renders on the text. DRAW_NO_OUTLINE:
        # no border around the run.
        view.add_regions(key, rs, scope=scope, flags=sublime.DRAW_NO_OUTLINE)
        used.add(key)
    # Permanent grey block last (z-order). Same flags as colour runs.
    if host_rs:
        key = _COLOR_KEY_PREFIX + _HOST_CURSOR_SCOPE
        view.add_regions(
            key, host_rs, scope=_HOST_CURSOR_SCOPE, flags=sublime.DRAW_NO_OUTLINE
        )
        used.add(key)
    vid = view.id()
    last = _LAST_COLOR_KEYS.get(vid, ())
    for k in last:
        if k not in used:
            view.erase_regions(k)
    _LAST_COLOR_KEYS[vid] = used


# ─── debug / recording env gates ──────────────────────────────────────────────
# Raw ANSI debug log: terminal/raw_debug_log.py (gated on _DEBUG).
# Asciicast v3 recording: terminal/cast_recorder.py. On if
# AI_TERMINAL_LOG_LINES is set in spawn_env OR in ST's process env, or if
# the record_asciicast setting is true (default). Per stext-settings-json-strict
# the env toggle is NOT a top-level setting key; it lives in spawn_env.
# Session text logs: terminal/session_text_log.py -- see _log_tab_text().
_LOG_LINES = bool(os.environ.get("AI_TERMINAL_LOG_LINES"))


_BOX_BORDER_CHARS = set("─━═╌╍╭╮╰╯┌┐└┘┏┓┗┛╔╗╚╝")

# A border row counts even with a title/hint label mixed in (e.g.
# "╭─ Claude ─────╮" or a bottom row carrying a status hint) as long as most
# of its non-space chars are border-drawing chars.
_BOX_BORDER_ROW_MIN_FRACTION = 0.6


def _command_line_row_range(term):
    """Screen-row span (top_border, bottom_border) of the box the PTY cursor
    currently sits inside, or None if the cursor isn't inside a drawn box.

    Ink-style TUIs (Claude Code, OpenCode, ...) frame their live input with a
    box-drawing border above and below it -- that's a reliable, app-agnostic
    signal for "this is the command line" vs. plain scrollback/response text.
    Plain shells (cmd.exe, PowerShell, bash) never draw such a box, so this
    returns None for them -- callers must fall back to a different signal
    (e.g. _live_cursor_row) rather than leaving copy_mode/caret-ownership
    untouched, since "no box" is the common case for plain shells, not a
    rare edge case.
    """
    try:
        with term._lock:
            rows, cy, cx = term.screen.render_cells()
    except (AttributeError, RuntimeError):
        print("[ai_terminal] command-line row range: render_cells failed:\n%s"
              % traceback.format_exc())
        return None
    n = len(rows)
    if not (0 <= cy < n):
        return None

    def is_border_row(idx):
        chars = [c for c, _a in rows[idx] if c and c != " "]
        if len(chars) < 3:
            return False
        border = sum(1 for ch in chars if ch in _BOX_BORDER_CHARS)
        return (border / len(chars)) >= _BOX_BORDER_ROW_MIN_FRACTION

    top = None
    for r in range(cy, max(cy - 30, -1) - 1, -1):
        if is_border_row(r):
            top = r
            break
    bottom = None
    for r in range(cy, min(cy + 30, n - 1) + 1):
        if is_border_row(r):
            bottom = r
            break
    if top is None or bottom is None or top == bottom:
        return None
    return (top, bottom)


def _live_cursor_row(term):
    """Buffer row of the PTY's actual hardware cursor -- the live input line.

    Fallback for _command_line_row_range when no drawn box is found (plain
    shells: cmd.exe, PowerShell, bash never draw one). Deliberately not a
    per-agent prompt-box scan -- the hardware cursor row is raw
    terminal-protocol state every app reports the same way.
    """
    try:
        with term._lock:
            hist = 0 if term.screen.alt_screen else len(term.screen.history)
            return hist + int(term.screen.y)
    except (AttributeError, TypeError):
        print("[ai_terminal] live cursor row lookup failed:\n%s" % traceback.format_exc())
        return None


# ─── view event listener: keystroke forwarding + lifecycle ───────────────────


class AiTerminalViewListener(sublime_plugin.ViewEventListener):
    @classmethod
    def is_applicable(cls, settings):
        return settings.get(_VIEW_SETTING, False)

    @classmethod
    def applies_to_primary_view_only(cls):
        return False

    def on_text_command(self, command, args):
        term = _Terminal.from_id(self.view.id())
        if term is None:
            return None
        # Recorded on every dispatch (regardless of copy_mode) so
        # on_selection_modified can tell a real mouse click on the command
        # line apart from a keyboard move that merely landed there -- only
        # the former should auto-disengage Text Edit Mode. See the click-vs-
        # keyboard comment there for why this distinction matters.
        term._last_sel_command_was_click = (command == "drag_select")
        if term.copy_mode:
            # Copy mode hands the view fully back to ST -- every command,
            # including "insert"/"left_delete"/"right_delete" from plain
            # typing that falls through the keymap's copy_mode context gate,
            # runs as ST's own default binding would. Nothing here should be
            # special-cased into a noop or diverted to the PTY.
            return None
        if command == "insert":
            chars = (args or {}).get("characters", "")
            if chars:
                _set_auto_follow(term, True)
                _scroll_to_bottom(self.view)
                term._last_vp_y = self.view.viewport_position()[1]
                term._live_anchor_y = term._last_vp_y
                # Enter in ST is an insert of "\n"; TUIs expect CR.
                term.send_string("\r" if chars == "\n" else chars)
            return ("ai_terminal_noop", {})
        if command == "left_delete":
            _set_auto_follow(term, True)
            _scroll_to_bottom(self.view)
            term._last_vp_y = self.view.viewport_position()[1]
            term._live_anchor_y = term._last_vp_y
            term.send_string("\x7f")
            return ("ai_terminal_noop", {})
        if command == "right_delete":
            _set_auto_follow(term, True)
            _scroll_to_bottom(self.view)
            term._last_vp_y = self.view.viewport_position()[1]
            term._live_anchor_y = term._last_vp_y
            term.send_string("\x1b[3~")
            return ("ai_terminal_noop", {})
        if command == "move":
            by = (args or {}).get("by")
            fwd = (args or {}).get("forward", False)
            # Fallback if arrows aren't bound to ai_terminal_keypress.
            # No scroll_to_bottom (resize thrash with layout watcher).
            application_mode = 1 in term.screen.private_modes
            if by == "characters":
                term.send_string(_get_key_code(
                    "right" if fwd else "left",
                    application_mode=application_mode,
                ))
                return ("ai_terminal_noop", {})
            if by == "lines":
                term.send_string(_get_key_code(
                    "down" if fwd else "up",
                    application_mode=application_mode,
                ))
                return ("ai_terminal_noop", {})
        return None

    def on_modified(self):
        # Catch programmatic inserts that bypass on_text_command (e.g.
        # send_to_view's run_command("insert") from another plugin, IME/unicode
        # input, paste). on_text_command does NOT fire for these, so without
        # this handler they'd land in the buffer and get wiped on the next
        # render without ever reaching the PTY -- which is why send_to_view
        # worked on Terminus tabs but not here. Mirrors Terminus's
        # event_listeners.on_modified: read command_history(0), forward "insert"
        # chars to the PTY, skip own commands. Unlike Terminus we do NOT
        # soft_undo others -- the full-view replace in ai_terminal_render wipes
        # stray text within a frame, and soft_undo risks recursion / clobbering
        # other plugins' writes to this view. ViewEventListener.on_modified
        # takes only self (view is self.view), unlike Terminus's plain
        # EventListener which takes a view arg.
        view = self.view
        term = _Terminal.from_id(view.id())
        if term is None or not term.pty.is_alive() or term.copy_mode:
            return
        try:
            command, args, _ = view.command_history(0)
        except (RuntimeError, AttributeError):
            print("[ai_terminal] on_modified: command_history() failed:\n%s"
                  % traceback.format_exc())
            return
        if not command:
            return
        # skip our own commands (ai_terminal_render replaces the whole view;
        # ai_terminal_send_string/keypress already wrote to the PTY) and the
        # "[process exited]" append marker, plus undo machinery to avoid loops
        if (command.startswith("ai_terminal")
                or command in ("append", "soft_undo", "undo", "redo")):
            return
        if command == "insert" and isinstance(args, dict) and "characters" in args:
            chars = args["characters"]
            if chars and len(view.sel()) == 1 and view.sel()[0].empty():
                _set_auto_follow(term, True)
                _scroll_to_bottom(view)
                term._last_vp_y = view.viewport_position()[1]
                term._live_anchor_y = term._last_vp_y
                # Forward raw. \n submits in Claude Code's TUI (a pasted multi-line
                # block becomes multi-prompt, one submit per line); converting a
                # lone \n to \r would NOT submit (verified) -- so send \n as-is.
                term.send_string(chars)

    def on_selection_modified(self):
        # term._user_owns_caret tells the render loop (AiTerminalRenderCommand)
        # whether the user has taken manual control of the caret, so it stops
        # fighting a position the user just placed. Planting the caret back
        # inside the app's drawn input box (bounded by box-drawing borders,
        # see _command_line_row_range) hands control back to the PTY cursor.
        #
        # Deliberately does NOT auto-*engage* copy_mode when the caret lands
        # outside the box (an earlier version of this method did). Bounds
        # detection reads the live screen grid, which is transiently wrong
        # or absent during permission prompts and "thinking" redraws -- that
        # false-positived copy_mode ON and silently swallowed the very
        # keystrokes needed to dismiss the prompt. copy_mode ON must stay an
        # explicit ctrl+alt+c action; see AiTerminalKeypressCommand's own
        # comment on why the identical heuristic was already removed once.
        #
        # It IS safe to auto-*disengage* copy_mode on a click back into the
        # box: that direction can only ever hand control back to the PTY,
        # never trap the user, so a missed/late bounds read just means the
        # user keeps using the manual ctrl+alt+c toggle instead.
        #
        # BUT: this must only fire for an actual mouse click, not a keyboard
        # move that happens to land the caret on that row -- confirmed live
        # 2026-09-11 that plain cursor-up navigation while in Text Edit Mode
        # was silently kicking the mode back off the moment the caret
        # reached the command line, with no click involved and no warning.
        # See on_text_command's `_last_sel_command_was_click` for the flag
        # that disambiguates the two.
        view = self.view
        term = _Terminal.from_id(view.id())
        if term is None or not term.pty.is_alive():
            return
        if getattr(term, "_in_render", False):
            # Selection changed as a side effect of our own render pass
            # (buffer patch or auto-caret placement) -- not a user gesture.
            return
        sel = view.sel()
        if len(sel) != 1:
            return
        # A non-empty selection is unambiguously user-owned, even when its
        # active endpoint happens to land inside the TUI's command-line row.
        # Treating only that endpoint as a caret click let the next render
        # collapse a visibly selected range; Ctrl+C then saw an empty
        # selection and forwarded ETX to the agent instead of copying.
        if not sel[0].empty():
            term._user_owns_caret = True
            return
        pt = sel[0].b
        row = view.rowcol(pt)[0]
        bounds = _command_line_row_range(term)
        if bounds is not None:
            on_command_line = bounds[0] <= row <= bounds[1]
        else:
            # No drawn box (plain shells: cmd.exe, PowerShell, bash) -- fall
            # back to comparing against the PTY's actual hardware cursor row
            # instead of unconditionally latching term._user_owns_caret,
            # which used to freeze the caret forever on the very first
            # selection event in any such shell (see ai/TODO.md).
            cursor_row = _live_cursor_row(term)
            on_command_line = cursor_row is not None and abs(row - cursor_row) <= 1
        if on_command_line:
            term._user_owns_caret = False
            # _do_render only repaints when term.screen.dirty -- i.e. on new
            # PTY bytes. A click back into the box hands tracking back to
            # the live PTY cursor (above), but nothing marks the screen
            # dirty, so the caret would otherwise keep showing the click's
            # landing spot -- stale -- until the next keystroke/output
            # happens to trigger a frame. Force one now so it snaps to the
            # true PTY cursor immediately.
            term.screen.dirty = True
            _schedule_render(term)
            if term.copy_mode and getattr(term, "_last_sel_command_was_click", False):
                term.copy_mode = False
                view.set_read_only(False)
                _scroll_to_bottom(view)
                _set_auto_follow(term, True)
                sublime.status_message("Ai terminal: command line")
        else:
            term._user_owns_caret = True

    def on_close(self):
        term = _Terminal.from_id(self.view.id())
        if term is None:
            return
        if term._watcher is not None:
            term._watcher.dispose()
            term._watcher = None
        with _term_lock():
            _term_registry().pop(self.view.id(), None)
        self.view.erase_phantoms(_CLOSE_TOOLBAR_PHANTOM_KEY)

        # Closing a tab -- by any method (mouse-X, Ctrl+W, whole-window
        # close, a script's view.close()) -- only ever detaches now, never
        # ends the session. Real termination is deliberately opt-in only:
        # '/exit' inside the agent itself, or the explicit Kill Session/End
        # Session commands (Tab Context menu, Command Palette -- no toolbar
        # shortcut, see _add_close_toolbar), which set
        # _expected_termination_reason themselves before anything closes.
        # A bare close can't tell mouse-X
        # apart from a real command anyway (sublimehq/sublime_text#1922), so
        # there is no signal here to decide a kill on -- see conversation
        # 2026-09-09 for the confirmation-dialog approach this replaced.
        threading.Thread(target=term.kill, daemon=True).start()

        # Every OTHER way to reach this point (Close & Keep Alive, Kill
        # Session, End Session, AiTerminalTabCloseInterceptor's blocked
        # window commands) sets _expected_termination_reason first and/or
        # shows its own status message. Since 2026-09-15,
        # AiTerminalTabCloseInterceptor blocks every native window-close
        # command outright, so the ONLY remaining unannounced path here is
        # mouse-X (sublimehq/sublime_text#1922 -- structurally uncatchable).
        # Without this, a mouse-X close silently orphans a live session with
        # zero indication anything is still running -- the user only finds
        # out by noticing a tab is missing and going looking for it. Not a
        # persistent/sticky notice (a modal here would have the exact
        # hide-behind-other-windows problem the close dialog was dropped
        # for -- see memory: ghostshell-close-dialog-can-hide-behind-windows)
        # but at least an immediate one.
        if getattr(term, "_expected_termination_reason", None) is None and _is_broker_pty(term.pty):
            sublime.status_message(
                "Ai terminal: %s tab closed -- session still running in the "
                "background. Use 'Ai: Recover Session...' to get it back."
                % (term.profile_name or "agent")
            )

    # ─── pre-empt ST's internal view.show on focus/hover ───────────────────
    #
    # ST's compositor repaints the view on Windows activation messages
    # (WM_ACTIVATE / WM_KILLFOCUS) and on hover, and briefly paints at a stale
    # viewport position even though vp is (0,0). The continuous clamp loop
    # catches the resulting vp drift within ~16ms (1 frame), but the user sees
    # that 1 frame. These handlers run BEFORE ST's internal repaint on the same
    # event, so clamping vp here pre-empts the bad paint instead of waiting for
    # the 16ms tick. Only fires when content fits within 1 line of overflow, so
    # it never fights the user scrolling up to read scrollback.
    #
    # Dip-only, like every other viewport writer since 57624a0: corrects a
    # horizontal drift or a position ABOVE rest, never a deliberate scroll
    # DOWN into the scroll-past-end area. That is where the toolbar and the
    # Settings panel sit, so the old "any nonzero vp -> (0, 0)" check snapped
    # a short, fitting tab back to the top on every mouse hover or focus
    # change -- confirmed live 2026-09-22 with Vibe (layout 752 vs viewport
    # 743), both snaps logged from on_hover and on_deactivated.
    def _preclamp_vp(self):
        v = self.view
        try:
            if not v or not v.is_valid():
                return
            le = v.layout_extent()
            ve = v.viewport_extent()
            vp = v.viewport_position()
            lh = v.line_height() or 12.0
            rest = _host_rest_y(v)
            if le[1] - ve[1] <= lh and (vp[1] < rest - 1.0 or vp[0] != 0.0):
                _set_viewport(v, (0.0, rest), False)
        except (RuntimeError, AttributeError, TypeError):
            print("[ai_terminal] preclamp viewport failed:\n%s" % traceback.format_exc())

    def on_hover(self, point, hover_zone):
        self._preclamp_vp()

    def on_activated(self):
        self._preclamp_vp()
        # Sublime can restore a terminal view with a stale viewport/frame
        # even though the PTY screen and backing buffer are current.  A
        # layout change (moving the tab to another column) happens to force a
        # repaint, which otherwise leaves prompts/output invisible until the
        # user jiggles the layout.  Mark the screen dirty and request one
        # activation paint so focus changes are visually self-healing.
        term = _Terminal.from_id(self.view.id())
        if term is not None and term.pty.is_alive():
            with term._lock:
                term.screen.dirty = True
            _schedule_render(term, delay_ms=0)
        _maybe_reattach_broker(self.view)

    def on_deactivated(self):
        self._preclamp_vp()


def _mouse_hist_len(term):
    """Lines above the PTY grid in the ST view (host top pad + scrollback).

    Host top pad is always present; terminal history is only on main screen.
    """
    pad = _HOST_SCROLL_PAD_LINES
    if term.screen.alt_screen:
        return pad
    return pad + len(term.screen.history)


def _event_to_pty_cell(view, term, event):
    """Map a ST mouse event to 1-based (col, row) in the PTY grid, or None."""
    if not event or "x" not in event or "y" not in event:
        return None
    try:
        pt = view.window_to_text((event["x"], event["y"]))
        row, col = view.rowcol(pt)
    except (RuntimeError, AttributeError, TypeError):
        print("[ai_terminal] mouse event->cell mapping failed:\n%s" % traceback.format_exc())
        return None
    return _view_point_to_cell(
        row,
        col,
        hist_len=_mouse_hist_len(term),
        screen_rows=term.screen.rows,
        screen_cols=term.screen.cols,
    )


def _route_click_to_cursor_fallback(view, term, event):
    """Reposition the PTY's own line-editor cursor via synthesized arrow keys.

    For apps with no DEC mouse-tracking receiver (Claude Code, Gemini,
    Antigravity, Codex, Kimi, Kiro, Junie -- confirmed via asciicast scan,
    2026-08-11: they never send CSI ?1000/1002/1003 h) a click has no PTY-side
    mechanism to move the app's real edit cursor at all -- ST's own selection
    moves, but the app's readline-style buffer does not, so left/right still
    act on the old position. This fakes it: only when the hardware cursor is
    already sitting on the live `>` prompt row (so we know screen.x is really
    the app's cursor column, not a footer-park artifact -- see caret.py), map
    the click column to a delta from the current column and send that many
    Left/Right presses, exactly what a human would type to get there by hand.

    Off the prompt row (find_prompt_row None) or hardware cursor parked
    elsewhere (spinner, footer) this intentionally no-ops -- there is no
    reliable column to diff against, so guessing would risk moving the
    cursor to the wrong place instead of just leaving it be.
    """
    cell = _event_to_pty_cell(view, term, event)
    if cell is None:
        return False
    col, row = cell  # 1-based
    screen = term.screen
    # Locked: screen.x/y/private_modes/cols are mutated by the reader thread
    # concurrently, and every other grid consumer (_command_line_row_range,
    # _Terminal.kill, the render path) already takes this lock first -- an
    # unlocked mid-scroll read here could compute delta against a torn
    # screen.x and silently send arrow keys to the wrong column.
    with term._lock:
        py = _find_prompt_row(screen)
        if py is None or screen.y != py or (row - 1) != py:
            return False
        start = _input_start_col(screen, py)
        limit = _field_right_limit(screen, py)
        target = min(max(col - 1, start), limit)
        current = int(screen.x)
        delta = target - current
        application_mode = 1 in screen.private_modes
        cols = int(screen.cols)
    if delta == 0:
        return True
    if abs(delta) > cols:
        # A stale/torn screen.x would otherwise turn into a large visible
        # cursor jump; treat an implausible delta as a no-op instead.
        return False
    key = "right" if delta > 0 else "left"
    code = _get_key_code(key, application_mode=application_mode)
    term.send_string(code * abs(delta))
    return True


# Button-hold state for 1002/1003:
#   view_id -> (proto_btn, col, row, gen, t0, moved)
# ST never delivers a clean mouse-up. We release after idle so:
#   tap   = press … short idle → release  (touchpad-friendly)
#   drag  = press … motion… longer idle → release
#   double = 2nd hit same cell / ST by=words → release + full click
_MOUSE_HOLD = {}
# Last completed click (for double-tap after release): view_id -> (col, row, t)
_MOUSE_LAST_CLICK = {}
# Touchpad taps are one short drag_select; complete them quickly.
_MOUSE_TAP_RELEASE_MS = 130
# After the pointer actually moves (thumb drag), keep hold longer.
_MOUSE_DRAG_RELEASE_MS = 500
# Touchpads are slower than mice; allow a wider double-tap window.
_MOUSE_DBLCLICK_MS = 700
# Rows at the bottom of a fullscreen TUI are usually the input line — bad
# default for two-finger scroll (would aim at the prompt, not the history).
_WHEEL_AVOID_BOTTOM_ROWS = 4


def _encode_pty_mouse(
    term, button, col, row, press=True, motion=False, shift=False, meta=False, ctrl=False
):
    """Encode one mouse report via libghostty-vt. Empty if filtered.

    Encodes in the tracking mode and format the app requested; callers only
    get here when _mouse_goes_to_app(term) is true (Ghostty's model, see
    _app_wants_mouse).

    Falls back to mouse.py only if the parser encoder is missing.
    """
    encode = getattr(getattr(term, "parser", None), "encode_mouse", None)
    if encode is not None:
        try:
            seq = encode(
                button, col, row,
                press=press, motion=motion,
                shift=shift, meta=meta, ctrl=ctrl,
            )
        except (RuntimeError, ValueError, TypeError, OSError):
            seq = None
        if seq is not None:
            return seq
    return _encode_mouse(
        button, col, row, press=press, motion=motion,
        sgr=bool(getattr(getattr(term, "screen", None), "mouse_sgr", True)),
        shift=shift, meta=meta, ctrl=ctrl,
    )


def _mouse_force_release(term, view_id):
    """Emit SGR/X10 release for any held button and clear hold state."""
    if not _mouse_handling_enabled(term):
        _MOUSE_HOLD.pop(view_id, None)
        return
    hold = _MOUSE_HOLD.pop(view_id, None)
    if not hold or term is None:
        return
    btn, col, row = hold[0], hold[1], hold[2]
    try:
        seq = _encode_pty_mouse(term, btn, col, row, press=False)
        if seq:
            term.send_string(seq)
        _MOUSE_LAST_CLICK[view_id] = (col, row, time.time())
    except (AttributeError, OSError):
        print("[ai_terminal] mouse force-release failed:\n%s" % traceback.format_exc())


def _schedule_mouse_release(view, gen, delay_ms):
    """Release the held button if no further drag events arrive."""
    vid = view.id()

    def _fire():
        hold = _MOUSE_HOLD.get(vid)
        if hold is None or hold[3] != gen:
            return
        t = _Terminal.from_id(vid)
        _mouse_force_release(t, vid)

    sublime.set_timeout(_fire, int(delay_ms))


def _send_full_click(term, view_id, proto, col, row, sgr=None):
    """Press+release one click and remember it for double-tap detection."""
    if not _mouse_handling_enabled(term):
        return
    _mouse_force_release(term, view_id)
    press = _encode_pty_mouse(term, proto, col, row, press=True)
    release = _encode_pty_mouse(term, proto, col, row, press=False)
    term.send_string((press or "") + (release or ""))
    _MOUSE_LAST_CLICK[view_id] = (col, row, time.time())


# Copy-first tap arm: view_id not needed — stored on term.
# term._cf_tap = (col, row, gen, proto) while waiting to see if the pointer moves.
_COPYFIRST_TAP_MS = 150


def _cancel_copyfirst_tap(term):
    """Invalidate any pending copy-first tap→PTY click."""
    if term is None:
        return
    try:
        term._cf_tap = None
    except AttributeError:
        print("[ai_terminal] cancel copy-first tap failed:\n%s" % traceback.format_exc())


def _arm_or_cancel_copyfirst_tap(view, term, event):
    """Copy-first + mouse tracking: arm a delayed full click, or cancel on move.

    Returns True when this event should be swallowed (noop) so ST does not
    start a text selection on a TUI button tap. Returns False to let ST
    own a drag-select (pointer moved to another cell).
    """
    cell = _event_to_pty_cell(view, term, event)
    if cell is None:
        _cancel_copyfirst_tap(term)
        return False
    col, row = cell
    st_btn = event.get("button", 1) if event else 1
    proto = _st_button_to_proto(st_btn)
    if proto is None:
        _cancel_copyfirst_tap(term)
        return False

    pending = getattr(term, "_cf_tap", None)
    if pending is None:
        gen = int(getattr(term, "_cf_tap_gen", 0) or 0) + 1
        term._cf_tap_gen = gen
        term._cf_tap = (col, row, gen, proto)
        term._last_mouse_cell = (col, row)
        _set_auto_follow(term, False)
        vid = view.id()

        def _fire(v_id=vid, g=gen, c=col, r=row, p=proto):
            t = _Terminal.from_id(v_id)
            if t is None:
                return
            arm = getattr(t, "_cf_tap", None)
            if arm is None or arm[2] != g:
                return  # cancelled (drag or newer tap)
            t._cf_tap = None
            if not _mouse_goes_to_app(t) or not t.pty.is_alive():
                return
            sgr = t.screen.mouse_sgr
            try:
                _send_full_click(t, v_id, p, c, r, sgr)
            except Exception as e:
                print(f"[ai_terminal] copy-first tap click failed: {e}")

        sublime.set_timeout(_fire, _COPYFIRST_TAP_MS)
        # Swallow so a button tap does not leave a ST selection flash that
        # also freezes paints under the select guard.
        return True

    p_col, p_row, p_gen, p_proto = pending[0], pending[1], pending[2], pending[3]
    if (col, row) != (p_col, p_row):
        # Pointer moved → user is drag-selecting text; cancel PTY tap.
        _cancel_copyfirst_tap(term)
        _arm_st_select_guard(term)
        return False
    # Same cell re-delivery / jitter: keep the arm, swallow.
    return True


def _wheel_locus(view, term):
    """1-based (col, row) for wheel reports when ST gives no pointer.

    Priority:
      1) last click/drag cell (user was aiming at scrollbar / history)
      2) centre of the history panel (not the command line, not only the
         far-right chrome — Grok often needs the pointer over the message list)
    """
    cell = getattr(term, "_last_mouse_cell", None)
    if cell is not None:
        return cell
    cols = max(1, int(term.screen.cols))
    rows = max(1, int(term.screen.rows))
    avoid = min(_WHEEL_AVOID_BOTTOM_ROWS, max(0, rows - 1))
    usable = max(1, rows - avoid)
    row = max(1, (usable + 1) // 2)
    col = max(1, (cols + 1) // 2)  # message list centre
    return col, row


def _route_mouse_click(view, term, event, *, discrete_click=False):
    """Send a mouse report when the app enabled tracking. Return True if handled.

    discrete_click: True for multi-click (ST by=words/lines) — always a full
    press+release so double-clicks reach the TUI instead of becoming ST word
    selection. False for normal drag_select: in 1002/1003 modes use press +
    motion + idle release so scroll-thumb drag works; in 1000 mode always
    press+release.

    Multi-click often arrives without event x/y — fall back to last cell so
    double-clicks on the scroll control still reach the PTY.

    Touchpad notes: taps are short; we auto-release quickly until motion is
    seen, then keep the hold longer for drag-grab. Double-tap window is wide.
    """
    if not _mouse_handling_enabled(term) or not _mouse_goes_to_app(term):
        return False
    mode = term.screen.mouse_tracking
    cell = _event_to_pty_cell(view, term, event)
    if cell is None and discrete_click:
        cell = getattr(term, "_last_mouse_cell", None)
    if cell is None:
        # Click in scrollback (or off-grid): let ST select text.
        return False
    st_btn = event.get("button", 1) if event else 1
    proto = _st_button_to_proto(st_btn)
    if proto is None:
        return False
    col, row = cell
    term._last_mouse_cell = (col, row)
    # This click goes to the app, so Sublime never sees it and can't clear
    # its own selection. Clear it here, as Ghostty does before reporting a
    # click (~/tools/ghostty src/Surface.zig:3910): a selection left behind
    # pauses this tab's painting (_selection_paint_blocked), and nothing but
    # another Shift-click freed it (found live 2026-09-23).
    _clear_view_selection(view, term)
    # User is interacting with the TUI chrome — don't yank viewport to bottom.
    _set_auto_follow(term, False)
    sgr = term.screen.mouse_sgr
    vid = view.id()
    now = time.time()

    # Multi-click or click-only mode: complete any prior hold, then one click.
    if discrete_click or mode < 1002:
        _send_full_click(term, vid, proto, col, row, sgr)
        return True

    # 1002 (drag) / 1003 (any-event): press, then motion while held.
    hold = _MOUSE_HOLD.get(vid)
    if hold is None:
        # Fresh press. If a previous click just finished on this cell, this is
        # the second half of a double-tap (common on touchpads after release).
        prev = _MOUSE_LAST_CLICK.get(vid)
        if (
            prev is not None
            and prev[0] == col
            and prev[1] == row
            and (now - prev[2]) * 1000.0 <= _MOUSE_DBLCLICK_MS
        ):
            _send_full_click(term, vid, proto, col, row, sgr)
            return True
        seq = _encode_pty_mouse(term, proto, col, row, press=True)
        gen = 1
        _MOUSE_HOLD[vid] = (proto, col, row, gen, now, False)
        if seq:
            term.send_string(seq)
        _schedule_mouse_release(view, gen, _MOUSE_TAP_RELEASE_MS)
        return True

    btn_prev, c_prev, r_prev, gen_prev, t0, moved = hold
    elapsed_ms = (now - t0) * 1000.0
    same_cell = (col, row) == (c_prev, r_prev)

    # Second hit on the same cell soon after press, without having dragged =
    # double-click (ST sometimes omits by=words). Finish first click, send
    # a full second click. Do NOT emit "motion" — that ate double-clicks.
    if same_cell and not moved and elapsed_ms <= _MOUSE_DBLCLICK_MS:
        _send_full_click(term, vid, proto, col, row, sgr)
        return True

    # Same cell, still holding, no movement: ST re-delivery / jitter. Keep
    # the press alive (refresh idle timer) without spamming the TUI.
    if same_cell:
        gen = gen_prev + 1
        _MOUSE_HOLD[vid] = (proto, col, row, gen, t0, moved)
        delay = _MOUSE_DRAG_RELEASE_MS if moved else _MOUSE_TAP_RELEASE_MS
        _schedule_mouse_release(view, gen, delay)
        return True

    # Cell changed → drag motion (scroll-thumb grab).
    seq = _encode_pty_mouse(term, proto, col, row, press=True, motion=True)
    gen = gen_prev + 1
    _MOUSE_HOLD[vid] = (proto, col, row, gen, t0, True)
    if seq:
        term.send_string(seq)
    _schedule_mouse_release(view, gen, _MOUSE_DRAG_RELEASE_MS)
    return True


def _route_mouse_wheel(view, term, amount):
    """Send one scroll gesture to the PTY as mouse-wheel events, nothing else.

    Called only when wheel_to_pty is on and the app asked for the mouse
    (_app_wants_mouse; see its two callers). Sends wheel up/down reports
    (buttons 64/65) and never translates the wheel into arrow keys or
    PageUp/PageDown (owner, 2026-09-22: no hidden translations).

    Direction is content-grab: amount > 0 drags the text down and asks for
    older history (wheel up), amount < 0 wheel down. Sublime gives no pointer
    position for a scroll, so the report is placed at _wheel_locus.
    """
    try:
        see_older = float(amount) > 0
    except (TypeError, ValueError):
        see_older = True
    _set_auto_follow(term, False)
    term._last_scroll_send_t = time.time()
    col, row = _wheel_locus(view, term)
    btn = _BTN_WHEEL_UP if see_older else _BTN_WHEEL_DOWN
    # One scroll event from Sublime = one wheel report (one notch).
    report = _encode_pty_mouse(term, btn, col, row, press=True) or ""
    if report:
        term.send_string(report)
    return True


def _pin_terminal_viewport(view, term):
    """Snap viewport back after a trackpad pan (kill visible jiggle).

    TUI / mouse-tracking: rest at top of real content (below top pad) so
    both up and down pans still have headroom. Not y=0 — that blocked
    down-drag forever.
    """
    try:
        view.settings().set("scroll_past_end", True)
        # Never hard-pin after pan/scroll -- user owns the Sublime tab.
        # Only correct the negative overshoot glitch (vp below rest).
        _pin_viewport_rest_dip_only(view, None, term)
    except (RuntimeError, AttributeError):
        print("[ai_terminal] pin terminal viewport failed:\n%s" % traceback.format_exc())


class AiTerminalKeyInterceptor(sublime_plugin.EventListener):
    """Ctrl+C/V, and mouse → PTY when the app enabled DEC mouse tracking."""

    def on_text_command(self, view, command_name, args):
        if not view.settings().get(_VIEW_SETTING):
            return None
        term = _Terminal.from_id(view.id())
        if term is None:
            return None
        if command_name == "copy":  # Ctrl+C (no selection) -> interrupt
            if not view.sel() or all(s.empty() for s in view.sel()):
                term.send_string("\x03")
                return ("ai_terminal_noop", {})
        if command_name == "paste":  # Ctrl+V -> forward clipboard
            text = sublime.get_clipboard()
            if text:
                # Only wrap in bracketed-paste markers when the running
                # program actually opted in (DECSET ?2004h). Wrapping
                # unconditionally sends literal "~200~"/"~201~" garbage into
                # anything that never asked for it -- cmd.exe, PowerShell, a
                # plain REPL. When the mode IS on, the wrapper still matters:
                # it makes a multi-line paste land as one paste event instead
                # of each newline acting as Enter (auto-submitting early).
                if 2004 in term.screen.private_modes:
                    text = "\x1b[200~" + text + "\x1b[201~"
                term.send_string(text)
            return ("ai_terminal_noop", {})
        # ── Xterm mouse tracking (Grok / fullscreen TUIs) ──────────────────
        # Apps enable via CSI ?1000/1002/1003 h (+ usually ?1006 h SGR).
        # Without this, clicks only move ST's selection and never reach the PTY.
        #
        # drag_forwards_by_default (ai_terminal.sublime-settings):
        #   true  — mouse-first (old): plain drag → PTY; Shift/Ctrl-drag → ST select
        #   false — copy-first: ALL drags → ST select (plain, Shift, Ctrl). PTY
        #           drag is off so Grok cannot steal the gesture; wheel still
        #           routes via scroll_lines. (Modifier-drag → PTY was a bad flip:
        #           it removed the only working select bypasses.)
        if command_name == "drag_select":
            args = args or {}
            event = args.get("event") or {}
            modified = bool(
                args.get("extend") or args.get("additive") or args.get("subtractive")
            )
            multi = args.get("by") in ("words", "lines", "columns")
            # No DEC mouse-tracking receiver for this click (mouse_handling
            # off -- the common case, see ai_terminal.sublime-settings profile
            # comments -- or the app never asked for tracking): fall back to
            # synthesized arrow keys so a plain click on the live prompt still
            # moves the app's real cursor, not just ST's own selection. Skip
            # for modifier-drags (text selection) and multi-click (word/line
            # select) -- those are never cursor-placement gestures.
            tracked = _mouse_handling_enabled(term) and _mouse_goes_to_app(term)
            # Bisection gate (ai_terminal.sublime-settings): off by default --
            # see settings comment.
            if (
                not modified
                and not multi
                and not tracked
                and not term.copy_mode
                and _setting_bool("click_to_cursor_fallback_enabled", False, profile_name=_term_profile_name(term))
            ):
                _route_click_to_cursor_fallback(view, term, event)
            if not _mouse_handling_enabled(term) or not _mouse_goes_to_app(term):
                return None
            # The click below goes to the PTY and is swallowed, so Sublime
            # never gets the mouse-down it would use to focus this pane.
            # Focus it here, as every terminal does on a click, then send the
            # click unchanged (found live 2026-09-23: clicks in an unfocused
            # Claude pane with mouse_handling on never focused it).
            window = view.window()
            if window is not None and not _same_view(window.active_view(), view):
                window.focus_view(view)
            raw = sublime.load_settings(_SETTINGS_NAME).get(
                "drag_forwards_by_default", True
            )
            if isinstance(raw, str):
                forward_by_default = raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                forward_by_default = bool(raw)
            # Copy-first: ST owns *drags* (text select between agent tabs).
            # Still deliver short *taps* and multi-clicks to the PTY when the
            # app enabled mouse tracking — otherwise Grok trust dialogs /
            # buttons never receive clicks (ST only emits drag_select).
            if not forward_by_default:
                if modified:
                    _cancel_copyfirst_tap(term)
                    _mouse_force_release(term, view.id())
                    _arm_st_select_guard(term)
                    return None
                # mouse_handling is on and the app asked (checked above).
                if multi:
                    _cancel_copyfirst_tap(term)
                    if _route_mouse_click(
                        view, term, event, discrete_click=True
                    ):
                        return ("ai_terminal_noop", {})
                    return None
                # Tap vs drag: arm a delayed full PTY click; cancel if the
                # pointer moves to another cell (then ST keeps the select).
                if _arm_or_cancel_copyfirst_tap(view, term, event):
                    return ("ai_terminal_noop", {})
                return None
            # Mouse-first: Shift/Ctrl-drag select; plain drag → PTY when tracking.
            if modified:
                _mouse_force_release(term, view.id())
                _arm_st_select_guard(term)
                return None
            if _route_mouse_click(view, term, event, discrete_click=multi):
                return ("ai_terminal_noop", {})
            return None
        # Two-finger trackpad / mouse wheel with wheel_to_pty on and an app
        # that asked for the mouse: swallow the scroll and send it to the PTY
        # as mouse-wheel events only (_route_mouse_wheel), then pin the
        # viewport so the tab never visibly pans. Otherwise Sublime scrolls.
        if command_name in ("scroll_lines", "scroll_horizontally") and not (
                _wheel_to_pty_enabled(term) and _mouse_goes_to_app(term)):
            return None
        if command_name in ("scroll_lines", "scroll_horizontally"):
            args = args or {}
            if command_name == "scroll_lines":
                amt = args.get("amount", 1)
                n = int(getattr(term, "_scroll_lines_log_n", 0) or 0)
                if n < 12:
                    print(
                        f"[ai_terminal] scroll_lines amount={amt!r} "
                        f"mouse={term.screen.mouse_tracking} "
                        f"alt={term.screen.alt_screen}"
                    )
                    term._scroll_lines_log_n = n + 1
                _route_mouse_wheel(view, term, amt)
            _pin_terminal_viewport(view, term)
            # Immediate second pin on next tick: ST may apply residual pan
            # after on_text_command returns even when we replace the command.
            sublime.set_timeout(
                lambda v=view, t=term: _pin_terminal_viewport(v, t), 0
            )
            return ("ai_terminal_noop", {})
        return None

    def on_query_context(self, view, key, operator, operand, match_all):
        # Lets Default.sublime-keymap gate every ai_terminal_keypress binding
        # on copy_mode being off, so when copy_mode is on those keys fall
        # through to ST's own default keybindings untouched -- true native
        # ST navigation/selection/copy, not a hand-picked subset re-routed
        # through custom move/move_to calls. Only ctrl+alt+c (the toggle
        # itself, bound unconditionally) still reaches this plugin while in
        # copy mode.
        #
        # ai_terminal_wheel_to_pty: gates Default.sublime-mousemap's
        # scroll_up/scroll_down bindings, so the wheel/trackpad reaches
        # ai_terminal_trackpad_scroll only while wheel_to_pty is on; off, the
        # binding does not apply and Sublime scrolls the tab natively. Without
        # these bindings no scroll ever reached the plugin (found live
        # 2026-09-22: a two-finger swipe sent no scroll_lines command).
        if key not in ("ai_terminal_copy_mode", "ai_terminal_wheel_to_pty"):
            return None
        if not view.settings().get(_VIEW_SETTING):
            return None
        term = _Terminal.from_id(view.id())
        if key == "ai_terminal_wheel_to_pty":
            val = bool(term is not None and _wheel_to_pty_enabled(term)
                       and _mouse_goes_to_app(term))
        else:
            val = bool(term.copy_mode) if term is not None else False
        if operator == sublime.OP_EQUAL:
            return val == bool(operand)
        if operator == sublime.OP_NOT_EQUAL:
            return val != bool(operand)
        return None


def _quick_panel_item(trigger, details, annotation, kind):
    """sublime.QuickPanelItem when available, else a plain [trigger, detail] row.

    ST 4 renders rich rows (kind glyph, annotation, dimmed detail); older builds
    fall back to the two-line list form rather than losing the command.
    """
    item = getattr(sublime, "QuickPanelItem", None)
    if item is None:
        detail = " ".join(p for p in (details, annotation) if p)
        return [trigger, detail or " "]
    return item(trigger, details, annotation, kind)


# ─── commands ────────────────────────────────────────────────────────────────


# Markers that mean "this directory is a project root" (Claude / agents care).
# Checked after `.git`; first hit walking *up* from a file wins (nearest root).
_PROJECT_MARKERS = (
    "CLAUDE.md",
    "Claude.md",
    "AGENTS.md",
    "Agents.md",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "composer.json",
)


def _norm_path(path):
    return os.path.normcase(os.path.abspath(path))


def _containing_window_folder(window, path):
    """Deepest window.folders() entry that contains path, or None."""
    if not window or not path:
        return None
    path_n = _norm_path(path)
    best = None
    best_len = -1
    for folder in window.folders() or []:
        folder_n = _norm_path(folder)
        if path_n == folder_n or path_n.startswith(folder_n + os.sep):
            if len(folder_n) > best_len:
                best = folder
                best_len = len(folder_n)
    return best


def _has_git(path):
    """True if path is a git work tree (dir) or gitfile (submodule)."""
    if not path or not os.path.isdir(path):
        return False
    git = os.path.join(path, ".git")
    return os.path.isdir(git) or os.path.isfile(git)


def _has_project_markers(path):
    if not path or not os.path.isdir(path):
        return False
    return any(os.path.exists(os.path.join(path, m)) for m in _PROJECT_MARKERS)


def _looks_like_project_root(path):
    return _has_git(path) or _has_project_markers(path)


def _nearest_project_root(path, stop_at=None):
    """Walk up from path; return nearest project root for agent cwd.

    Prefers a `.git` directory (real repo) over markdown/package markers so an
    umbrella folder like ~/projects that only has CLAUDE.md/AGENTS.md does not
    win over nested repos (SText, finance, …). Stops at *stop_at* (inclusive)
    when given — typically the containing window folder.
    """
    if not path:
        return None
    cur = os.path.abspath(path if os.path.isdir(path) else os.path.dirname(path))
    if not cur:
        return None
    stop = os.path.abspath(stop_at) if stop_at else None
    stop_n = _norm_path(stop) if stop else None
    start = cur
    marker_hit = None

    while True:
        if _has_git(cur):
            return cur
        if marker_hit is None and _has_project_markers(cur):
            marker_hit = cur
        at_stop = bool(stop_n and _norm_path(cur) == stop_n)
        parent = os.path.dirname(cur)
        if at_stop or parent == cur:
            return marker_hit or (stop if at_stop else None) or start
        if stop_n:
            parent_n = _norm_path(parent)
            # Do not walk above the window-folder boundary.
            if not (parent_n == stop_n or stop_n.startswith(parent_n + os.sep)
                    or parent_n.startswith(stop_n + os.sep)):
                return marker_hit or stop or start
            if len(parent_n) < len(stop_n) and not stop_n.startswith(parent_n + os.sep):
                return marker_hit or stop or start
        cur = parent


def _child_project_dirs(folder, limit=80):
    """Immediate subdirs worth offering as agent cwd.

    Prefers git-backed children. Marker-only dirs (CLAUDE.md / AGENTS.md with
    no .git) are used only when the parent has no git children — so an umbrella
    like ~/projects is not re-listed under ~ just because it has a layer-1 map.
    """
    git_kids = []
    marker_kids = []
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        print("[ai_terminal] child project dirs: listdir %s failed:\n%s"
              % (folder, traceback.format_exc()))
        return []
    for name in names:
        if name.startswith("."):
            continue
        child = os.path.join(folder, name)
        if not os.path.isdir(child):
            continue
        if _has_git(child):
            git_kids.append(child)
        elif _has_project_markers(child):
            marker_kids.append(child)
    chosen = git_kids if git_kids else marker_kids
    return chosen[:limit]


def _cwd_candidates(window):
    """Folders the user can sensibly launch an agent into.

    Umbrella sidebar roots (e.g. ~/projects with many nested repos) expand to
    those children. The umbrella itself is only listed when it is a real git
    repo (monorepo) — a CLAUDE.md at the projects map layer is not enough.
    """
    folders = list(window.folders() or []) if window else []
    if not folders:
        return []
    candidates = []
    seen = set()

    def _add(p):
        n = _norm_path(p)
        if n in seen:
            return
        seen.add(n)
        candidates.append(p)

    for folder in folders:
        children = _child_project_dirs(folder)
        if children:
            for child in children:
                _add(child)
            # Monorepo root: also offer the folder itself.
            if _has_git(folder):
                _add(folder)
        else:
            _add(folder)
    return candidates


def _sole_auto_cwd(folders):
    """Return a single unambiguous cwd, or None to force a picker.

    A lone window folder is only auto-used when it is a real git repo, or when
    it has no nested project children. Umbrella maps (CLAUDE.md + many repos)
    always return None so the user picks the real project.
    """
    if len(folders) != 1:
        return None
    folder = folders[0]
    if _child_project_dirs(folder):
        return folder if _has_git(folder) else None
    return folder


def _resolve_editor_path(view):
    """cwd for editor/context launches: nearest project root for the file."""
    window = view.window()
    path = view.file_name()
    if path:
        boundary = _containing_window_folder(window, path)
        return _nearest_project_root(path, stop_at=boundary)
    folders = window.folders() if window else []
    return _sole_auto_cwd(folders or [])


def _resolve_here_path(window, paths):
    """cwd for sidebar / Tools-menu launches.

    Priority:
      1. Explicit sidebar paths (dir as-is; file -> its project root)
      2. Active view's nearest project root
      3. Sole unambiguous window folder
      4. None → caller should offer a picker (never silently use umbrella dirs)
    """
    if paths:
        path = paths[0]
        if os.path.isdir(path):
            return path
        boundary = _containing_window_folder(window, path)
        return _nearest_project_root(path, stop_at=boundary)

    view = window.active_view() if window else None
    if view and view.file_name():
        boundary = _containing_window_folder(window, view.file_name())
        return _nearest_project_root(view.file_name(), stop_at=boundary)

    folders = window.folders() if window else []
    return _sole_auto_cwd(folders or [])


# Sticky per-window cwd override, set explicitly with the "Set Working Directory"
# command (pick once, reuse silently after that, never re-ask). The fast path is
# keyed by window id. The choice is also remembered across Sublime restarts in one
# small JSON file in Sublime's cache folder. It holds machine-specific folder paths, so
# it is deliberately not in Packages/User, which users often sync between computers.
# If the file is missing GhostShell simply asks again.
_working_dirs = {}
_WORKING_DIR_FILE = "working_directories.json"


class _WorkingDirStore:
    """The saved working directories: a JSON file in Sublime's cache folder.

    Offers the get / set / save calls that the working-directory code below uses.
    """

    def __init__(self):
        self._path = os.path.join(sublime.cache_path(), "GhostShell", _WORKING_DIR_FILE)
        self._data = {}
        try:
            with open(self._path, encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError):
            return
        if isinstance(loaded, dict):
            self._data = loaded

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value

    def save(self):
        """Write the file through a temporary file, so a crash cannot leave half a file."""
        folder = os.path.dirname(self._path)
        try:
            os.makedirs(folder, exist_ok=True)
            descriptor, temporary_path = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle)
            os.replace(temporary_path, self._path)
        except OSError:
            print("[ai_terminal] could not save the working directory:\n%s" % traceback.format_exc())


def _working_dir_identity(window):
    """Primary/legacy User-settings key without modifying the project."""
    if window is None:
        return None
    project_file_name = getattr(window, "project_file_name", None)
    project_file = project_file_name() if callable(project_file_name) else None
    if project_file:
        return "project:" + os.path.normcase(os.path.abspath(project_file))
    folders = [
        os.path.normcase(os.path.abspath(path))
        for path in (window.folders() or [])
    ]
    return "folders:" + "|".join(sorted(folders)) if folders else None


def _working_dir_identities(window, selected_path=None):
    """Lookup/storage aliases stable across common window-shape changes."""
    if window is None:
        return []
    keys = []
    project_file_name = getattr(window, "project_file_name", None)
    project_file = project_file_name() if callable(project_file_name) else None
    if project_file:
        keys.append("project:" + os.path.normcase(os.path.abspath(project_file)))

    folders = list(window.folders() or [])
    context_path = selected_path
    if context_path is None:
        view = window.active_view()
        context_path = view.file_name() if view and view.file_name() else None
    containing = _containing_window_folder(window, context_path) if context_path else None
    if containing:
        keys.append("folder:" + os.path.normcase(os.path.abspath(containing)))
    elif len(folders) == 1:
        keys.append("folder:" + os.path.normcase(os.path.abspath(folders[0])))

    legacy = _working_dir_identity(window)
    if legacy:
        keys.append(legacy)
    return list(dict.fromkeys(keys))

def _get_working_dir(window):
    path = _working_dirs.get(window.id()) if window else None
    if path and os.path.isdir(path):
        return path
    if window:
        _working_dirs.pop(window.id(), None)
    identities = _working_dir_identities(window)
    if identities:
        saved = _WorkingDirStore()
        raw = saved.get("directories", {})
        directories = dict(raw) if isinstance(raw, dict) else {}
        stale = False
        for identity in identities:
            path = directories.get(identity)
            if path and os.path.isdir(path):
                migrated = False
                for alias in _working_dir_identities(window, selected_path=path):
                    if directories.get(alias) != path:
                        directories[alias] = path
                        migrated = True
                if migrated:
                    saved.set("directories", directories)
                    saved.save()
                _working_dirs[window.id()] = path
                return path
            if path:
                directories.pop(identity, None)
                stale = True
        if stale:
            saved.set("directories", directories)
            saved.save()
            sublime.status_message(
                "Ai terminal: removed a saved working directory that no longer exists"
            )
    return None

def _set_working_dir(window, path):
    _working_dirs[window.id()] = path
    identities = _working_dir_identities(window, selected_path=path)
    if identities:
        saved = _WorkingDirStore()
        directories = saved.get("directories", {})
        directories = dict(directories) if isinstance(directories, dict) else {}
        for identity in identities:
            directories[identity] = path
        saved.set("directories", directories)
        saved.save()
    sublime.status_message("Ai terminal: working directory set to %s" % path)

def _clear_working_dir(window):
    removed = _working_dirs.pop(window.id(), None)
    identities = _working_dir_identities(window, selected_path=removed)
    if identities:
        saved = _WorkingDirStore()
        directories = saved.get("directories", {})
        directories = dict(directories) if isinstance(directories, dict) else {}
        changed = False
        for identity in identities:
            if directories.pop(identity, None) is not None:
                changed = True
        if changed:
            saved.set("directories", directories)
            saved.save()
            removed = True
    if removed:
        sublime.status_message("Ai terminal: working directory cleared")


def _pick_cwd_then(window, on_path):
    """Resolve cwd without ever prompting, then call on_path.

    Priority: an explicitly set working directory always wins. Otherwise fall
    back to automatic resolution (sole window folder, active file's project
    root). Tools → Ai Terminal → profile (e.g. Grok Build) lands here with no
    sidebar paths — do **not** auto-spawn from an umbrella map (~/projects via
    CLAUDE.md, ~ via AGENTS.md); if nothing is unambiguous, tell the user to
    set a working directory instead of showing a picker.
    """
    sticky = _get_working_dir(window)
    if sticky:
        sublime.status_message("Ai terminal: using saved working directory %s" % sticky)
        on_path(sticky)
        return

    folders = list(window.folders() or []) if window else []
    sole = _sole_auto_cwd(folders)
    if sole:
        # No saved override (e.g. just cleared) but exactly one folder is
        # open, so this is unambiguous -- still say so. Silence here reads
        # as "did Clear Working Directory even do anything?" right after
        # the one action that should have changed something.
        sublime.status_message("Ai terminal: using window folder %s" % sole)
        on_path(sole)
        return

    candidates = _cwd_candidates(window)
    if not candidates:
        # No sidebar projects: last resort is active-file / sole resolve.
        path = _resolve_here_path(window, [])
        if path:
            sublime.status_message(
                "Ai terminal: using active file's project root %s" % path
            )
            on_path(path)
            return
        # A status message alone is easy to miss entirely (small, bottom-left,
        # transient) -- this is a hard stop with no fallback, so make it a
        # modal the user cannot miss instead of a silent-feeling no-op.
        sublime.error_message(
            "Ai Terminal: no folder open and no working directory set.\n\n"
            "Open a folder, or use Tools → Ai Terminal → "
            "Set Working Directory, then try again."
        )
        return
    if len(candidates) == 1:
        sublime.status_message("Ai terminal: using project folder %s" % candidates[0])
        on_path(candidates[0])
        return

    sublime.error_message(
        "Ai Terminal: multiple project folders open.\n\n"
        "Right-click one in the sidebar and choose 'Set Ai Terminal Working "
        "Directory'."
    )


class AiTerminalSetWorkingDirectoryCommand(sublime_plugin.WindowCommand):
    """Sidebar: pin one folder as this window's Ai Terminal cwd.

    Command palette / sidebar right-click: "Set Ai Terminal Working
    Directory". Every subsequent launch uses this folder silently, including
    after a Sublime restart, until cleared or reset to a different one. The
    mapping lives in Packages/User, never inside the selected project.
    """

    def run(self, paths=None):
        if paths:
            path = paths[0]
            if not os.path.isdir(path):
                path = os.path.dirname(path)
        else:
            # Command Palette invocation: no sidebar selection to go on, so
            # fall back to the same unambiguous auto-resolve everything else
            # uses (active file's project root, else the sole window folder)
            # — never a picker, matching the point of this command.
            path = _resolve_here_path(self.window, [])
        if not path:
            sublime.status_message(
                "Ai terminal: no folder resolved — right-click a folder in "
                "the sidebar and use Set Ai Terminal Working Directory instead"
            )
            return
        _set_working_dir(self.window, path)

    def is_visible(self, paths=None):
        # None means "no paths arg at all" (Command Palette) vs. [] (sidebar
        # right-click with nothing usable selected) — only the latter hides.
        return True if paths is None else bool(paths)


class AiTerminalClearWorkingDirectoryCommand(sublime_plugin.WindowCommand):
    """Sidebar: forget this window's pinned Ai Terminal working directory."""

    def run(self):
        _clear_working_dir(self.window)

    def is_visible(self):
        return _get_working_dir(self.window) is not None


def _resolve_profile_launch(profile_name, s=None):
    """Resolve a profile name into (profile_name, argv, extra_env) -- the
    launch_command/spawn_env half of _spawn's old body, split out so
    AiTerminalRelaunchCommand can redo this resolution (profile settings may
    have changed since the tab was first spawned) without duplicating it."""
    s = s or _settings_obj()
    profile_data = _profile_settings(profile_name, s)

    if profile_data:
        argv = _platform_argv(
            profile_data.get("launch_command", _DEFAULT_LAUNCH_COMMAND)
        )
        shared_env = s.get("shared_spawn_env", {})
        if not isinstance(shared_env, dict):
            shared_env = {}
        extra_env = dict(shared_env)
        extra_env.update(profile_data.get("spawn_env", {}))
    else:
        # Fallback to legacy single command settings
        argv = _launch_command()
        extra_env = _spawn_env()
        profile_name = "Legacy" if profile_name else None
    return profile_name, argv, extra_env


def _spawn(window, path, profile=None):
    if not _PTY_OK:
        sublime.error_message("ai_terminal: no PTY backend available (ConPTY ctypes binding failed).")
        return

    s = _settings_obj()
    profile_name, argv, extra_env = _resolve_profile_launch(profile or s.get("default_profile"), s)

    # Determine unique tab name
    pfx = "Ai"
    if profile_name:
        if "Gemini" in profile_name:
            pfx = "Gemini"
        elif "Claude" in profile_name:
            pfx = "Claude"
        else:
            pfx = profile_name
    tab_name = _next_ai_name(window, prefix=pfx)

    view = _terminal_view(window, name=tab_name, profile_name=profile_name)
    window.focus_view(view)
    _spawn_into_view(view, path, profile_name, argv, extra_env)


def _spawn_into_view(view, path, profile_name, argv, extra_env):
    """Start a fresh PTY/Screen/_Terminal and attach it to an already-
    created, already-focused terminal view -- the tail end of _spawn's old
    body, factored out so AiTerminalRelaunchCommand can reuse it against an
    *existing* tab (kill the old session, reset the view, spawn a new
    process into it) instead of duplicating this bring-up sequence."""
    cols, rows = _measure(view, profile_name=profile_name)

    # Host (ST plugin host / agent shells) often has NO_COLOR=1, FORCE_COLOR=0,
    # TERM=dumb — Grok doctor then reports color=none. Sanitize before spawn;
    # profile spawn_env still wins for any key it sets.
    env = _sanitize_pty_env(os.environ, extra_env)
    # Pick up PATH changes from setx/installers without requiring an ST restart.
    env = _refresh_path_env(env)
    # Resolve "$secret:NAME" placeholders from the User-only secrets file, so
    # API keys reach the spawned agent without ever entering this (public)
    # repo or the ambient environment.
    env = _resolve_env_refs(env)
    env = _resolve_secret_refs(env)

    try:
        argv = _resolve_launch_argv(argv, env)
    except FileNotFoundError as e:
        sublime.error_message(f"ai_terminal: {e}")
        view.close()
        return

    print(f"[ai_terminal] launch cwd: {path!r}")
    print(f"[ai_terminal] launch argv: {argv!r}")

    # Build the VT engine *before* the child exists. Grok's keyboard-handling
    # probe (CSI ? u) fires the instant the process starts and is never
    # retried in that session -- /doctor then reports the cached miss. The
    # previous order (pty.start, then parser, then bind, then writer/reader)
    # left that first probe unanswered. Parser construction also loads the
    # DLL, which is the slow part of bring-up.
    try:
        screen = _Screen(cols, rows, history_cap=_scrollback_size(profile_name))
        parser = _make_parser(screen)
    except Exception as e:
        print("[ai_terminal] VT engine init failed:\n%s" % traceback.format_exc())
        sublime.error_message(f"ai_terminal: failed to initialize the VT engine:\n{e}")
        view.close()
        return

    # False here is only the fallback for a settings object with no
    # top-level "detachable" key at all (e.g. a minimal one built in tests).
    # The shipped ai_terminal.sublime-settings sets "detachable": true at
    # the top level, which makes every profile -- plain shells included --
    # detachable and thus eligible for recovery/Open in Windows Terminal by
    # default. A profile can still set "detachable": false to opt out.
    detachable = _setting_bool("detachable", False, profile_name=profile_name)

    if os.name != "nt":
        pty = _PosixPty(argv, path, cols, rows, env)
        print("[ai_terminal] Spawning PTY process using 'posix' backend.")
    elif detachable:
        # Do NOT write the pipe name into view.settings() yet -- doing so
        # before the terminal is registered below opens a window where
        # on_activated's reattach check (_maybe_reattach_broker) sees "pipe
        # name set, no live _Terminal yet" and races this same spawn with a
        # second one for the identical pipe name (observed live: 4 broker
        # processes competing for one named pipe). Settings are written only
        # after pty.start() + registry insertion succeed, below.
        pipe_name = "ghostshell_" + uuid.uuid4().hex[:20]
        pty = _BrokerPty(
            pipe_name, argv, path, cols, rows, env,
            scrollback_bytes=_broker_scrollback_bytes(profile_name),
            profile_name=profile_name,
        )
        print(f"[ai_terminal] Spawning PTY process using 'broker' backend (detachable, pipe={pipe_name!r}).")
    else:
        pty = _Pty(argv, path, cols, rows, env)
        print("[ai_terminal] Spawning PTY process using 'conpty' backend.")

    term = _Terminal(
        view,
        pty,
        screen,
        parser,
        spawn_env=extra_env,
        profile_name=profile_name,
    )
    term.prepare()
    try:
        pty.start()
    except Exception as e:
        print("[ai_terminal] PTY start failed:\n%s" % traceback.format_exc())
        try:
            term.kill()
        except (OSError, AttributeError, RuntimeError):
            print("[ai_terminal] cleanup kill after failed PTY start also failed:\n%s"
                  % traceback.format_exc())
        sublime.error_message(f"ai_terminal: failed to start PTY:\n{e}")
        view.close()
        return
    if detachable and _is_broker_pty(pty):
        view.settings().set(_BROKER_PIPE_SETTING, pty.pipe_name)
        view.settings().set(_BROKER_PROFILE_SETTING, profile_name)
        view.settings().set(_BROKER_CWD_SETTING, path)
        view.settings().set(_SESSION_ID_SETTING, pty.pipe_name)
        _stamp_broker_pid_when_known(view, pty.pipe_name)
    with _term_lock():
        _term_registry()[view.id()] = term
    _add_close_toolbar(term)
    term.start_reader()


def _stamp_broker_pid_when_known(view, pipe_name, tries=20):
    """The broker writes its own registry record (with child_pid) shortly
    after spawn, not synchronously with pty.start() returning -- poll it
    briefly so external tools reading this view's settings get a real pid
    instead of none. Gives up silently after ~2s (tries*100ms); the pid
    just stays unset, same as before this existed."""
    try:
        if not view.is_valid():
            return
    except (RuntimeError, AttributeError):
        return
    record = _read_broker_registry_record(pipe_name)
    child_pid = (record or {}).get("child_pid")
    if child_pid:
        try:
            view.settings().set(_CHILD_PID_SETTING, child_pid)
        except (RuntimeError, AttributeError):
            pass
        return
    if tries > 0:
        sublime.set_timeout(
            lambda: _stamp_broker_pid_when_known(view, pipe_name, tries - 1), 100)


def _maybe_reattach_broker(view, _confirm=False):
    """If `view` is a detachable-profile ai_terminal tab restored by Sublime
    (workspace session restore after a restart) but has no live _Terminal,
    reconnect it to its still-running agent_broker.py session instead of
    leaving it orphaned. Cheap no-op for every other view."""
    try:
        if not view.settings().get(_VIEW_SETTING):
            return
        if _Terminal.from_id(view.id()) is not None:
            return
        pipe_name = view.settings().get(_BROKER_PIPE_SETTING)
        if not pipe_name:
            return

        # A forced process kill can leave Session.sublime_session one save
        # behind even though the broker and its child survived. The broker's
        # own registry is written outside Sublime's workspace save cycle, so
        # prefer a newer matching live-session record when the restored pipe
        # has no record of its own.
        profile_name = view.settings().get(_BROKER_PROFILE_SETTING)
        cwd = view.settings().get(_BROKER_CWD_SETTING)
        registered = _registered_brokers(profile_name, cwd)
        registered_names = {item.get("pipe_name") for item in registered}
        if pipe_name not in registered_names and registered:
            pipe_name = registered[0]["pipe_name"]
            view.settings().set(_BROKER_PIPE_SETTING, pipe_name)
            print(f"[ai_terminal] recovered stale restored broker identity as {pipe_name!r}")
        view.settings().set(_SESSION_ID_SETTING, pipe_name)
        _stamp_broker_pid_when_known(view, pipe_name)

        vid = view.id()
        if vid in _BROKER_CONNECTING:
            return
        if not _confirm:
            if vid in _BROKER_REATTACH_PENDING:
                return
            _BROKER_REATTACH_PENDING.add(vid)
            _BROKER_REATTACH_CANDIDATE[vid] = _measure(
                view, profile_name=view.settings().get(_BROKER_PROFILE_SETTING)
            )
            sublime.set_timeout(
                lambda v=view: _maybe_reattach_broker(v, _confirm=True),
                _BROKER_REATTACH_CONFIRM_MS,
            )
            return

        size = _measure(
            view, profile_name=view.settings().get(_BROKER_PROFILE_SETTING)
        )
        previous = _BROKER_REATTACH_CANDIDATE.get(vid)
        if size != previous:
            _BROKER_REATTACH_CANDIDATE[vid] = size
            sublime.set_timeout(
                lambda v=view: _maybe_reattach_broker(v, _confirm=True),
                _BROKER_REATTACH_CONFIRM_MS,
            )
            return

        # An inactive restored sheet can remain at the minimum measurement
        # until Sublime lays it out for display.  Do not turn that transient
        # value into the permanent main-screen height; on_activated will
        # start a fresh confirmed attempt.  A genuinely one-row ACTIVE pane
        # is still valid and is not inflated to a made-up minimum.
        window = view.window()
        if size[1] <= _min_rows() and window is not None and window.active_view() != view:
            _BROKER_REATTACH_PENDING.discard(vid)
            _BROKER_REATTACH_CANDIDATE.pop(vid, None)
            return

        _BROKER_REATTACH_PENDING.discard(vid)
        _BROKER_REATTACH_CANDIDATE.pop(vid, None)
        _reattach_broker_view(view, pipe_name)
    except (AttributeError, RuntimeError, OSError, KeyError, TypeError):
        try:
            vid = view.id()
            _BROKER_REATTACH_PENDING.discard(vid)
            _BROKER_REATTACH_CANDIDATE.pop(vid, None)
        except (RuntimeError, AttributeError):
            print("[ai_terminal] reattach check: pending-set cleanup failed:\n%s"
                  % traceback.format_exc())
        print("[ai_terminal] reattach check failed:\n%s" % traceback.format_exc())


def _seed_restored_history(screen, restored_text):
    """Fallback when broker replay is empty: seed restored plain rows."""
    restored_lines = restored_text.splitlines()
    keep = screen.history_cap + screen.rows
    if keep:
        restored_lines = restored_lines[-keep:]
    for line in restored_lines:
        screen.history.append([(ch, 0) for ch in line])
    return len(restored_lines)


def _reattach_broker_view(view, pipe_name):
    """Connect `view` to the broker on `pipe_name` and replay teed bytes.

    feed_bootstrap advances only the native VT during the snapshot;
    finish_bootstrap then imports native grid and scrollback. Restored
    view text is history only if that replay is empty. Resize reflow is
    GhosttyParser.resize()/_sync_scrollback(), not reconnect.
    """
    if not _PTY_OK or os.name != "nt":
        return
    vid = view.id()
    if vid in _BROKER_CONNECTING:
        return
    _BROKER_CONNECTING.add(vid)
    # Workspace-restored views do not pass through _terminal_view(). Reapply
    # the complete terminal view configuration before parsing the broker's
    # redraw, especially the generated ANSI colour scheme. Sublime may restore
    # only the user's global scheme for an untitled scratch buffer.
    profile_name = view.settings().get(_BROKER_PROFILE_SETTING)
    _apply_terminal_view_settings(view, profile_name=profile_name)
    path = view.settings().get(_BROKER_CWD_SETTING)
    s = _settings_obj()
    profile_data = _profile_settings(profile_name, s) if profile_name else None

    if profile_data:
        argv = _platform_argv(profile_data.get("launch_command", _DEFAULT_LAUNCH_COMMAND))
        shared_env = s.get("shared_spawn_env", {})
        if not isinstance(shared_env, dict):
            shared_env = {}
        extra_env = dict(shared_env)
        extra_env.update(profile_data.get("spawn_env", {}))
    else:
        argv = _launch_command()
        extra_env = _spawn_env()

    env = _sanitize_pty_env(os.environ, extra_env)
    env = _refresh_path_env(env)
    env = _resolve_env_refs(env)
    env = _resolve_secret_refs(env)
    try:
        argv = _resolve_launch_argv(argv, env)
    except FileNotFoundError as e:
        _BROKER_CONNECTING.discard(vid)
        print(f"[ai_terminal] reattach: {e}")
        return

    cols, rows = _measure(view, profile_name=profile_name)
    restored_text = view.substr(sublime.Region(0, view.size()))
    try:
        screen = _Screen(cols, rows, history_cap=_scrollback_size(profile_name))
        parser = _make_parser(screen)
    except (ValueError, TypeError, IndexError, MemoryError):
        _BROKER_CONNECTING.discard(vid)
        print("[ai_terminal] reattach: VT engine init failed:\n%s" % traceback.format_exc())
        return

    pty = _BrokerPty(
        pipe_name, argv, path, cols, rows, env, allow_spawn=False,
        profile_name=profile_name,
    )
    term = _Terminal(view, pty, screen, parser, spawn_env=extra_env, profile_name=profile_name)
    term._reattach_bootstrap = True
    term._bootstrap_got_bytes = False
    term._restored_text = restored_text

    def _finish_connected():
        _BROKER_CONNECTING.discard(vid)
        try:
            usable = bool(view.is_valid() and view.window())
        except (RuntimeError, AttributeError):
            usable = False
        if not usable:
            # The user closed the restored view while the worker connected.
            # BrokerPty.kill is detach-only and never sends the broker KILL
            # control message.
            pty.kill()
            return
        with _term_lock():
            existing = _term_registry().get(vid)
            if existing is not None:
                pty.kill()
                return
            # A restored broker connection is now proven live and this view
            # has won the registry race. Only now create its recording files;
            # preparing before the blocking pipe connection produced orphaned
            # and zero-byte casts when reconnect failed or lost this race.
            term.prepare(reattach=True)
            view.settings().set(_VIEW_SETTING, True)
            view.settings().erase("ai_terminal_orphaned")
            _term_registry()[vid] = term
        _add_close_toolbar(term)
        term.start_reader()
        print(f"[ai_terminal] reattached view {vid} to pipe {pipe_name!r}")

    def _finish_failed(message, trace):
        _BROKER_CONNECTING.discard(vid)
        # Leave the restored buffer readable, but stop advertising it as a
        # live terminal.  Keeping ai_terminal_view=true with no registered
        # _Terminal makes the global keymap capture arrows/PageUp/etc. and
        # then drop them, producing an apparently frozen tab after a failed
        # reattach.  Preserve the pipe identity for explicit recovery.
        try:
            view.settings().set(_VIEW_SETTING, False)
            view.settings().set("ai_terminal_orphaned", True)
            view.set_read_only(False)
        except (RuntimeError, AttributeError):
            print("[ai_terminal] reattach: mark view orphaned failed:\n%s"
                  % traceback.format_exc())
        print("[ai_terminal] reattach: broker connect failed:\n%s" % trace)
        sublime.status_message(f"Ai terminal: could not reattach ({message})")

    def _connect_worker():
        try:
            pty.start()
        except Exception as e:
            trace = traceback.format_exc()
            sublime.set_timeout(
                lambda message=str(e), detail=trace: _finish_failed(message, detail), 0
            )
            return
        sublime.set_timeout(_finish_connected, 0)

    # Named-pipe connection is blocking by design. Never perform it on
    # Sublime's main thread: stale/busy restored sessions previously froze the
    # entire editor on startup and on every tab activation.
    threading.Thread(target=_connect_worker, daemon=True).start()


class AiTerminalOpenHereCommand(sublime_plugin.WindowCommand):
    """Open a Claude TUI terminal in the chosen directory.

    Resolves cwd from sidebar selection, else the active file's nearest
    project root (.git / CLAUDE.md / …), else a quick-panel of project
    folders — never silently falls back to an umbrella sidebar root
    like ~/projects.

    Menu: Side Bar.sublime-menu — "Open Ai Terminal here..."
          Main.sublime-menu → Tools → Ai Terminal → profiles
    Command palette: "Ai: Open Terminal Here"
    """

    def run(self, paths=None, profile=None):
        paths = paths or []
        if paths:
            path = _resolve_here_path(self.window, paths)
            if not path:
                sublime.status_message("Ai terminal: no folder resolved")
                return
            _spawn(self.window, path, profile=profile)
            return

        def on_path(path):
            _spawn(self.window, path, profile=profile)

        _pick_cwd_then(self.window, on_path)

    def is_enabled(self, paths=None, profile=None):
        return _profile_is_available(profile)

    def is_visible(self, paths=None, profile=None):
        return _profile_is_available(profile)

    def description(self, paths=None, profile=None):
        # Menu entries without an explicit "caption" render this live label,
        # e.g. "Claude — not installed" when the program is missing.
        return _profile_menu_caption(profile)


def _tab_menu_target_view(window, group, index):
    """The view a Tab Context menu command should act on.

    Sublime substitutes real values into a menu entry's `"group": -1,
    "index": -1` placeholders with the group/index of the *right-clicked*
    tab -- but only for Tab Context.sublime-menu. Context.sublime-menu
    (in-editor right click) and the Command Palette never pass these, so
    group/index stay at their -1 defaults; -1 falls back to the window's
    active view, which is correct there too (right-clicking inside a view's
    text -- as opposed to its tab -- does make that view active, and a
    Palette invocation has no clicked tab at all, only "whatever's focused").

    Without this, a WindowCommand or a naive self.view-based TextCommand in
    Tab Context.sublime-menu silently acts on the *active* tab instead of
    the *right-clicked* one whenever they differ -- confirmed live
    2026-09-02: right-clicking a background tab and choosing Kill Session
    killed the session in the focused tab instead.
    """
    if group is not None and group >= 0 and index is not None and index >= 0:
        views = window.views_in_group(group)
        if 0 <= index < len(views):
            return views[index]
    return window.active_view()


class AiTerminalOpenInEditorCommand(sublime_plugin.WindowCommand):
    """Open a Claude TUI terminal in the active file's project root.

    Uses the nearest project root containing the file (not the bare
    file directory, and not the umbrella window folder).

    Menu: Context.sublime-menu / Tab Context.sublime-menu — "Open Ai Terminal here..."
    Command palette: "Ai: Open Terminal in Editor"

    A WindowCommand, not a TextCommand, specifically so Tab Context's
    group/index (see _tab_menu_target_view) can target the right-clicked
    tab rather than whatever tab happens to be focused.
    """

    def run(self, profile=None, group=-1, index=-1):
        view = _tab_menu_target_view(self.window, group, index)
        window = self.window
        path = _resolve_editor_path(view) if view else None
        if path and window:
            _spawn(window, path, profile=profile)
            return
        if not window:
            sublime.status_message("Ai terminal: no folder resolved")
            return

        def on_path(p):
            _spawn(window, p, profile=profile)

        _pick_cwd_then(window, on_path)

    def is_enabled(self, profile=None, group=-1, index=-1):
        return _profile_is_available(profile)

    def is_visible(self, profile=None, group=-1, index=-1):
        return _profile_is_available(profile)

    def description(self, profile=None, group=-1, index=-1):
        return _profile_menu_caption(profile)


class AiTerminalSendStringCommand(sublime_plugin.TextCommand):
    """Send an arbitrary string to the PTY (terminus_send_string equivalent).

    No key/menu/palette binding; invoked programmatically.
    """

    def run(self, edit, string=""):
        term = _Terminal.from_id(self.view.id())
        if term:
            term.send_string(string)


class AiTerminalSendStringWindowCommand(sublime_plugin.WindowCommand):
    """Window-level variant: send a string to the terminal PTY without needing
    the terminal view to be focused.

    Resolves the target terminal view in this order:
      1. the active view in the window, if it is an ai_terminal view;
      2. otherwise the first ai_terminal view found in the window.

    Lets external callers (agents, other plugins, key bindings scoped to a
    non-terminal context) inject input into the terminal from anywhere.

    No key/menu/palette binding; invoked programmatically.
    """

    def run(self, string=""):
        view = self.window.active_view()
        if view is None or not view.settings().get(_VIEW_SETTING, False):
            for v in self.window.views():
                if v.settings().get(_VIEW_SETTING, False):
                    view = v
                    break
        if view is None:
            return
        term = _Terminal.from_id(view.id())
        if term:
            term.send_string(string)


class AiTerminalQueueInputCommand(sublime_plugin.WindowCommand):
    """Queue a string to land in the terminal PTY once the session goes
    idle (no PTY output for a few seconds) -- as if it had been typed and
    submitted live, rather than racing current output or a busy agent the
    way send_string's immediate write can.

    Same view-resolution as AiTerminalSendStringWindowCommand: active view
    if it's an ai_terminal view, else the first one found in the window.

    No key/menu/palette binding; invoked programmatically.
    """

    def run(self, string=""):
        view = self.window.active_view()
        if view is None or not view.settings().get(_VIEW_SETTING, False):
            for v in self.window.views():
                if v.settings().get(_VIEW_SETTING, False):
                    view = v
                    break
        if view is None:
            return
        term = _Terminal.from_id(view.id())
        if term:
            term.queue_input(string)


class AiTerminalQueueInputToCommand(sublime_plugin.WindowCommand):
    """Queue a string into a SPECIFIC ai_terminal tab, addressed explicitly
    by name (partial, case-insensitive) or index into window.views() --
    unlike AiTerminalQueueInputCommand above, which only ever guesses
    "active view, else the first ai_terminal view found."

    Exists because that guess is genuinely ambiguous with more than one
    ai_terminal tab open in the same window (a coordinating agent's own
    hosting tab is itself an ai_terminal view, so it can be the "active"
    one and wrongly swallow input meant for a different tab) -- confirmed
    live 2026-09-14, resolved only by hand-walking the internal terminal
    registry instead of anything callable directly. This is that
    resolution, made a real, reusable, one-call primitive: any external
    caller (another agent, a script, `run_command` via sublime-mcp) can
    now target a tab by name/index with no risk of hitting the wrong one.

    No key/menu/palette binding; invoked programmatically.
    """

    def run(self, string="", name="", index=-1):
        view = None
        views = self.window.views()
        if index >= 0:
            if 0 <= index < len(views):
                view = views[index]
        elif name:
            needle = name.lower()
            for v in views:
                if v.settings().get(_VIEW_SETTING, False) and needle in (v.name() or "").lower():
                    view = v
                    break
        if view is None or not view.settings().get(_VIEW_SETTING, False):
            sublime.status_message(
                "Ai terminal: no matching tab found for queue_input_to target"
            )
            return
        term = _Terminal.from_id(view.id())
        if term:
            term.queue_input(string)


# Host-only blank lines above AND below the TUI (not sent to the PTY).
# Pad *below* alone left rest_y=0, so ST could only pan dy>0 (one direction).
# Pad *above* gives headroom for dy<0 (finger-down / content-down). Rest
# viewport sits at the top of the real content so both drags produce signal.
_HOST_SCROLL_PAD_LINES = 0  # removed per user directive -- was blank filler lines
                             # above/below content, reachable via direct
                             # scrollbar/minimap drag (nothing clamped that
                             # gesture), landing users in dead blank space
                             # instead of real content. Every consumer of this
                             # constant (_append_host_scroll_pad, _host_rest_y,
                             # _real_content_height, _mouse_hist_len, and the
                             # pin/follow logic in AiTerminalRenderCommand)
                             # degrades to a clean no-op at 0.


def _append_host_scroll_pad(text):
    """Wrap TUI text in host-only pads so trackpad can pan both ways."""
    if text is None:
        text = ""
    pad = "\n" * _HOST_SCROLL_PAD_LINES
    if pad:
        # The bottom pad needs its own line to sit on -- only meaningful
        # when there's an actual pad to attach. With _HOST_SCROLL_PAD_LINES
        # at 0 (pad -- see its comment) this used to fire unconditionally
        # anyway, appending a bare trailing blank line after the real last
        # line (the command line) in every render, tab or panel.
        if text and not text.endswith("\n"):
            text += "\n"
    return pad + text + pad


def _host_rest_y(view):
    """Viewport y that shows the top of real TUI content (below top pad)."""
    lh = view.line_height() or 12.0
    return float(_HOST_SCROLL_PAD_LINES) * lh


def _pin_viewport_rest_dip_only(view, rest=None, term=None):
    """Pin to host rest_y, but only to correct a NEGATIVE overshoot below
    rest -- ST's view.show() briefly parking vp[1] below rest (e.g. -20)
    when content fits the viewport, the same glitch _clamp_vp_loop's
    near_fit branch guards against. A deliberate forward scroll past rest
    (e.g. pushing a short conversation's permission prompt up to read it in
    full) is left alone: for a real app-owned TUI, any drift genuinely is
    noise to correct, but content that merely happens to fit the viewport
    is not "owned" by anything that requires a fixed rest position. A
    direction-agnostic predecessor (pinned on ANY drift, removed 2026-09-19
    once every caller had moved to this dip-only version -- see
    docs/DEVIATIONS_FROM_TERMINUS.md section 1) caused exactly that: the
    render loop's content_fits branch snapped a deliberate scroll straight
    back on every render (Claude Code's CLI redraws its footer roughly
    every half-second even while idle), reported live before the fix.

    Unlike a direction-agnostic pin, does not force
    term._last_vp_y/_live_anchor_y to `rest` when no correction was made --
    they track wherever the viewport actually is, so the next render's
    drift-disengage check doesn't compare against a rest value the
    viewport was deliberately never returned to.
    """
    if rest is None:
        rest = _host_rest_y(view)
    try:
        cur = view.viewport_position()[1]
        if cur < rest - 1.0:
            _set_viewport(view, (0.0, rest), False)
            cur = rest
        if term is not None:
            term._last_vp_y = cur
            term._live_anchor_y = cur
    except (RuntimeError, AttributeError):
        print("[ai_terminal] pin viewport rest (dip-only) failed:\n%s"
              % traceback.format_exc())


def _real_content_height(view):
    """layout height of real terminal content (excludes both host pads).

    Derived from view.text_to_layout(view.size()) + line_height(), not
    view.layout_extent() -- the latter is inflated by exactly one
    line_height() on a view with scroll_past_end enabled (the default
    here; see _scroll_to_bottom, which was fixed for the same reason).
    Sharing this helper keeps content_fits/near_bottom/near_fit checks
    in agreement with _scroll_to_bottom's own follow target -- the two
    disagreeing by one line height was root-caused as a "jiggles up a
    line" symptom on certain keystrokes (content right at the fit
    boundary flips content_fits/near_bottom one frame and not the next).
    """
    lh = view.line_height() or 12.0
    bottom = float(view.text_to_layout(view.size())[1]) + lh
    return max(0.0, bottom - 2 * _HOST_SCROLL_PAD_LINES * lh)


def _follow_ignore_trailing_lines(term):
    """How many trailing rows snap-to-bottom should skip. Default 0.

    Negative values are allowed (deliberately not clamped to 0) and mean the
    opposite: overshoot the follow target that many extra lines past real
    content height. Live diagnostic lever for a persistent one-line-short
    follow target -- see _follow_content_height.
    """
    if term is None:
        return 0
    n = _setting_number(
        "follow_ignore_trailing_lines",
        0,
        profile_name=_term_profile_name(term),
    )
    try:
        return int(n or 0)
    except (TypeError, ValueError):
        print("[ai_terminal] follow_ignore_trailing_lines cast failed:\n%s"
              % traceback.format_exc())
        return 0


def _follow_content_height(view, ignore_trailing=0):
    """Content height snap-to-bottom should chase.

    Subtracts follow_ignore_trailing_lines (a profile setting, default 0)
    so a TUI whose last N rows wobble does not move the follow target.
    A negative value ADDS instead (overshoot past real content) -- not
    clamped to 0, see _follow_ignore_trailing_lines.
    """
    lh = view.line_height() or 12.0
    drop = int(ignore_trailing or 0)
    return max(0.0, _real_content_height(view) - drop * lh)


def _compensate_trim_scroll(view, term, vp):
    """Undo the visual shift caused by the history deque evicting old lines.

    Not a follow/snap heuristic like the machinery gated behind
    scroll_manipulation_enabled above -- that decides where the viewport
    *should* go. This corrects for the buffer changing size under a
    viewport that never moved: _do_render replaces the whole view text
    every frame, and once screen.history is at its maxlen cap, each newly
    retired line silently evicts the oldest one, shifting every remaining
    line's position by one. The same fixed pixel offset then shows
    different text than it did last frame -- the text moved, not the
    viewport (confirmed live 2026-08-18). The eviction count is exact
    (Screen.retired_total vs len(history) between two renders), so the
    compensation is exact too: it keeps whatever was on screen on screen,
    it never picks a target. Deliberately bypasses the kill switch.

    REVERTED 2026-08-23: a same-day attempt to skip this write while
    following the live prompt (_auto_follow True) -- reasoning that
    last-row-overflow evictions shouldn't yank the prompt off screen --
    made live jumping worse, not better. Skipping compensation during
    follow also skips it for the common case (any real eviction while
    actively following, which is most of the time once history is at
    cap), so the ordinary one-line-per-eviction text-slide this function
    exists to cancel went uncorrected continuously instead of the
    original, rarer large-overshoot case. Reported live as bigger/more
    frequent jumps than before the change. Back to unconditional: this
    function corrects a real pixel/text mismatch regardless of follow
    state, it does not decide whether to follow.
    """
    if term is None or view is None:
        return vp
    screen = term.screen
    total = getattr(screen, "retired_total", None)
    if total is None or getattr(screen, "alt_screen", False):
        return vp
    hist_len = len(screen.history)
    # getattr, not term._last_retired_total: an already-running terminal from
    # before this code existed has no such attribute on its instance (a
    # plugin reload only affects the class/module, not live objects'
    # __dict__) -- confirmed live 2026-08-18, would otherwise crash every
    # render for any tab opened before this landed.
    last_total = getattr(term, "_last_retired_total", None)
    last_len = getattr(term, "_last_history_len", None)
    term._last_retired_total = total
    term._last_history_len = hist_len
    if last_total is None:
        return vp
    evicted = (total - last_total) - (hist_len - (last_len or 0))
    if evicted <= 0:
        return vp
    # 2026-09-22: while following, leave the viewport alone (bookkeeping
    # above still runs, so nothing piles up for later). Old lines are only
    # evicted while following (Screen.trim_paused is on whenever follow is
    # off), and following means the follow code puts the tail back at the
    # bottom -- so this write moved the view up N lines and the follow write
    # moved it straight back down: the up-and-down jiggle on status updates
    # (logged live: 4185 -> 4157 -> 4185 on every eviction). With the write
    # skipped, 6 evictions over 55 frames moved the view 0 px. This repeats
    # the 2026-08-23 attempt reverted below; what changed since is that the
    # tab no longer rewrites the whole buffer every frame
    # (line_diff_render_enabled) and no longer changes height with an app's
    # footer (steady_screen_height_enabled).
    if term._auto_follow and not _setting_bool(
            "compensate_trim_while_following", False):
        return vp
    lh = view.line_height() or 20
    new_y = max(0.0, vp[1] - evicted * lh)
    if new_y != vp[1]:
        view.set_viewport_position((vp[0], new_y), False)
        vp = (vp[0], new_y)
        # Without this, the render loop's own "vp[1] < term._last_vp_y -
        # lh*1.5" user-scroll detector (a few lines below this call site)
        # reads this write as the user having scrolled up, disengages
        # _auto_follow, which now also latches screen.trim_paused True --
        # and with scroll_manipulation_enabled off, nothing ever moves the
        # viewport back to "near bottom" to re-engage it, so one real
        # eviction permanently stops all future trimming (confirmed live
        # 2026-08-18: unbounded growth, 300 -> 1000+ lines in seconds).
        if term is not None:
            term._last_vp_y = new_y
            term._live_anchor_y = new_y
    return vp


def _scroll_to_bottom(view):
    """Jump the viewport to the bottom of real terminal content (not the pad).

    Called on user input so typing brings the user back to the prompt after
    scrolling up to read scrollback. No-op when real content fits the viewport.
    A profile may set follow_ignore_trailing_lines to leave the last N
    rows out of the snap target.

    Root-caused 2026-09-06 (see project_text_jiggling_bug_reported memory):
    this used to target layout_extent()[1] (via _follow_content_height /
    _real_content_height), but on a view with scroll_past_end enabled --
    the default here -- layout_extent() is inflated by that virtual
    scroll-past-end padding, measured live as exactly one full
    line_height() taller than the buffer's real last-character position.
    Comparison against Terminus (a similar ST terminal plugin) confirmed
    it instead derives its target from view.text_to_layout(view.size()),
    never from layout_extent() -- avoiding that padding entirely. Same fix
    applied here, plus Terminus's own deadband (skip re-snapping if
    already within one line_height of the target): widening
    _pin_viewport_rest's unrelated 1px/3px tolerance earlier the same day
    did not help, because that function is for TUI/alt-screen apps only
    and was never in this code path to begin with.
    """
    ve = view.viewport_extent()
    lh = view.line_height() or 12.0
    top = _host_rest_y(view)
    term = _Terminal.from_id(view.id()) if view is not None else None
    drop = _follow_ignore_trailing_lines(term) * lh
    # _real_content_height already applies the +lh top-of-row -> bottom-of-
    # row correction (text_to_layout(size()) gives the TOP of the row
    # containing the last character, not its bottom -- confirmed live as
    # the recurring root cause of every "last line half/fully hidden"
    # report in this session: 211/212, 341/342, 343/344, ...). Routed
    # through the shared helper, not inlined here a second time, so this
    # and content_fits/near_bottom can never drift apart again the way
    # they did when only this function got the fix (see
    # _real_content_height's own docstring).
    real_h = max(0.0, _real_content_height(view) - drop)
    if real_h > ve[1]:
        # Bottom of real content = top pad + real_h
        target = top + real_h - ve[1]
    else:
        target = top
    cur = view.viewport_position()[1]
    # Only reposition when the tail would otherwise be genuinely hidden --
    # below the visible range (needs a forward scroll to reveal it) or
    # above it (scrolled too far past the end, needs a backward scroll) --
    # never just to force the exact bottom-pixel position when the tail is
    # already visible somewhere in the viewport. `target` is the MINIMUM
    # cur that puts the tail at the very bottom edge; `target + ve[1]` is
    # the MAXIMUM cur before the tail scrolls above the top edge. Anywhere
    # between, the tail sits visible somewhere in the middle/upper part of
    # the viewport with blank space below -- exactly what deliberately
    # pushing the tail up produces, and it must be left alone. Reported
    # live as "unnecessary snapping ... the command line is in full view
    # already": the old check (`abs(target - cur) < 1.0`) only tolerated
    # being within 1px of the one exact canonical position, not "visible
    # anywhere in the viewport" -- so every typed character / render
    # forced it back to that exact pixel row even though nothing was
    # actually hidden.
    if target - 1.0 <= cur <= target + ve[1] + 1.0:
        return
    # 2026-09-07: backward corrections (content legitimately shrinking, e.g.
    # Claude Code's own status footer redrawing shorter) were eased via a
    # smoothstep tween, then a retargeting IIR filter, to avoid a jarring
    # snap. Both were reverted the same day: retargeting mid-flight (which
    # happens on nearly every render during active output, as layout
    # catches up one frame at a time) produced real, confirmed-live bugs --
    # a restarting tween that never built motion, then an IIR that could
    # get stuck with no visible progress for dozens of frames, then a fix
    # for interrupting scroll-back that didn't resolve the stuck case
    # either. Simple instant correction has none of these failure modes;
    # the actual bug that mattered (the last real line being hidden) is
    # fixed above via the `+ lh` term, independent of whether the
    # correction here is eased or instant.
    _set_viewport(view, (0.0, target), False)


def _page_scroll(view, term, forward):
    """Move the viewport by one page, independent of caret position.

    PageUp/PageDown used to page via ST's native "move" command ("by":
    "pages"), which moves relative to the current CARET, not the current
    viewport position. The render loop unconditionally re-pins the sole
    caret to the live PTY cursor row (bottom of the buffer) on every frame
    -- keep_selection only skips this when both term._user_owns_caret and
    the (default-off) user_owns_caret_enabled setting are true, see
    AiTerminalRenderCommand -- so the caret sits at the bottom of the
    buffer regardless of where the user has scrolled to with the mouse
    wheel. "move by pages, forward=False" (PageUp) then pages backward
    from THAT caret position; if the user had scrolled up more than one
    page, the resulting caret position can still land BELOW their current
    scroll position, and Sublime auto-scrolls the viewport DOWN to reveal
    it -- PageUp visibly moves the view the wrong way. Confirmed live: mouse
    wheel up (scrolling well into scrollback), then PageUp, moves the view
    down instead of up. Computing and setting the target viewport position
    directly sidesteps the caret entirely, so it can never depend on
    caret/viewport having drifted apart.
    """
    lh = view.line_height() or 12.0
    ve = view.viewport_extent()
    cur = view.viewport_position()
    page = max(ve[1] - lh, lh)
    top = _host_rest_y(view)
    max_y = top + max(0.0, _real_content_height(view) - ve[1])
    new_y = min(max_y, cur[1] + page) if forward else max(top, cur[1] - page)
    _set_viewport(view, (cur[0], new_y), False)


def _resync_viewport_after_height_change(view, term, old_height=None,
                                         new_height=None):
    """Re-clamp/follow after this view's viewport_extent() height changes
    for any reason other than a PTY resize.

    Called from _clamp_vp_loop's own height-change detector, not from any
    specific command -- opening/closing a panel (console, find, ...), a
    window resize, a sidebar/minimap toggle, a tab-group sash drag all
    shrink or grow every view's viewport_extent(), and none of them is a
    keystroke or new PTY output -- the two things that normally drive the
    render loop's follow/pin logic (_settle_viewport).
    Enumerating specific window commands (an earlier version of this fix
    hooked on_post_window_command for "show_panel"/"hide_panel" only) misses
    every other cause; detecting the height change itself, generically,
    covers all of them. Left unhandled, a view that was scrolled/pinned to
    the tail at the OLD height keeps that same viewport y against the NEW
    height, so the most recently written lines end up below the visible
    area. Reported live: open the Sublime Python console, the last several
    lines of an ai_terminal tab go hidden below it. Re-running the same
    follow/pin decision the render loop already makes elsewhere fixes it
    without waiting for the next PTY byte or keypress.
    """
    if term is None or view is None or not term.pty.is_alive():
        return
    try:
        if term._auto_follow and old_height is not None and new_height is not None:
            # Keep the bottom anchored: move the view by exactly the height
            # change, so the last line stays the same distance from the
            # bottom edge. _scroll_to_bottom alone is not enough here: it
            # deliberately leaves the view alone while the last line is
            # visible anywhere, so closing a panel left the text floating
            # with blank lines below it (reported live 2026-09-22: console
            # open moved the text up, console close never moved it back).
            # Opening and then closing a panel now returns the view to
            # exactly where it was.
            cur = view.viewport_position()[1]
            target = max(_host_rest_y(view), cur + (old_height - new_height))
            _set_viewport(view, (0.0, target), False)
            _scroll_to_bottom(view)
            term._last_vp_y = view.viewport_position()[1]
            term._live_anchor_y = term._last_vp_y
        elif term._auto_follow:
            _scroll_to_bottom(view)
            term._last_vp_y = view.viewport_position()[1]
            term._live_anchor_y = term._last_vp_y
        elif _tui_like(term):
            _pin_viewport_rest_dip_only(view, None, term)
    except (RuntimeError, AttributeError):
        print("[ai_terminal] viewport height-change resync failed:\n%s"
              % traceback.format_exc())


def _place_auto_caret(view, term, pos):
    """Put the sole caret at `pos` on the render loop's behalf.

    Recorded on the terminal so on_selection_modified can tell this from a
    real user gesture (which then takes caret ownership).
    """
    sel = view.sel()
    sel.clear()
    sel.add(sublime.Region(pos, pos))
    if term is not None:
        term._last_auto_caret_pos = pos


def _settle_viewport(view, term, rest, tui_owns_scroll, do_follow, content_fits):
    """Where the viewport lands after a frame.

    App-owned TUIs used to hard-pin to rest every frame. That fought keypad /
    touchpad tab motion. Only correct a negative overshoot (dip); the user
    owns deliberate Sublime scroll.
    """
    if tui_owns_scroll:
        _pin_viewport_rest_dip_only(view, rest, term)
    elif do_follow and not content_fits:
        _scroll_to_bottom(view)
        if term is not None:
            term._last_vp_y = view.viewport_position()[1]
            term._live_anchor_y = term._last_vp_y


class AiTerminalToggleCopyModeCommand(sublime_plugin.TextCommand):
    """Toggle copy mode (see AiTerminalKeypressCommand.run).

    While on, plain arrow/page/home/end keys move or extend the ST caret
    instead of being forwarded to the PTY -- lets scrollback/response text be
    navigated and selected with the keyboard without triggering shell
    history recall or fighting a TUI's own cursor. Escape or toggling again
    exits copy mode and re-pins the viewport to the live prompt.

    The mouse too (2026-09-23): while on, clicks, drags and the wheel stay
    with Sublime even in an app that asked for the mouse (_mouse_goes_to_app)
    -- the one-tab, in-memory hand-back Ghostty's toggle_mouse_reporting
    gives, chosen by the owner over a separate mouse-only toggle.

    Reachable from the toolbar ("Text Edit Mode: On/Off", shows the state),
    Ai Terminal menu, Command Palette, and ctrl+alt+c inside an Ai terminal.
    """

    def is_enabled(self):
        return _Terminal.from_id(self.view.id()) is not None

    def run(self, edit):
        term = _Terminal.from_id(self.view.id())
        if term is None:
            return
        term.copy_mode = not term.copy_mode
        # Text Edit Mode is read/select/copy only -- there is no legitimate
        # reason to type into it, and letting typed characters through was
        # actively misleading: on_text_command already hands "insert" etc.
        # straight to ST as a plain buffer edit (never term.send_string), so
        # it visually looks like it went somewhere and then silently
        # vanishes on the next full-buffer repaint (confirmed live
        # 2026-09-11: typed into what looked like the command line, hit
        # Enter, toggled off, command line was blank -- the edit was never
        # real). Sublime's native read-only still permits move/select/copy,
        # it only blocks mutating commands, so this doesn't touch
        # navigation. AiTerminalRenderCommand.run re-asserts this every
        # frame (it must reset read_only=False first to paint), so it can't
        # be lost to a background render while the user is mid-navigation.
        self.view.set_read_only(term.copy_mode)
        if term.copy_mode:
            sublime.status_message("Ai terminal: Text Edit Mode ON (Esc to exit)")
        else:
            # Hand caret control back to the PTY cursor -- otherwise the
            # caret stays wherever copy-mode nav left it (outside the box)
            # and the very next keypress's render sees term._user_owns_caret
            # still True, freezing the caret and looking like copy mode
            # never really turned off.
            term._user_owns_caret = False
            _scroll_to_bottom(self.view)
            _set_auto_follow(term, True)
            sublime.status_message("Ai terminal: Text Edit Mode OFF")


class AiTerminalToggleTuneProfilePanelCommand(sublime_plugin.TextCommand):
    """Show/hide the in-tab profile-settings panel (toolbar "Settings" link).

    Expands/collapses inside this same view's phantom -- see
    _add_close_toolbar's panel_html branch -- rather than opening a
    separate view, so the live PTY tab is never covered or navigated away
    from.
    """

    def run(self, edit):
        term = _Terminal.from_id(self.view.id())
        if term is None:
            return
        term.tune_profile_open = not term.tune_profile_open
        _add_close_toolbar(term)

    def is_enabled(self):
        term = _Terminal.from_id(self.view.id())
        return term is not None and bool(_term_profile_name(term))


class AiTerminalTuneProfileSetCommand(sublime_plugin.TextCommand):
    """Flip one boolean setting for this tab's profile and write through
    immediately -- no separate Save step (the user explicitly does not want
    one here, unlike the mockup's dirty-state version). For a respawn-
    required key (_REATTACH_TUNABLE_PROFILE_KEYS), also respawns this tab
    right away, same as AiTerminalTuneProfileCommand's quick-panel on_pick.

    args: key (str) -- must appear in _LIVE_TUNABLE_PROFILE_KEYS or
    _REATTACH_TUNABLE_PROFILE_KEYS, else this is a no-op.
    """

    def run(self, edit, key):
        view = self.view
        term = _Terminal.from_id(view.id())
        if term is None:
            return
        profile_name = _term_profile_name(term)
        if not profile_name:
            return

        all_keys = dict(
            (k, d) for k, _desc, d in _LIVE_TUNABLE_PROFILE_KEYS
        )
        reattach_keys = dict(
            (k, d) for k, _desc, d in _REATTACH_TUNABLE_PROFILE_KEYS
        )
        if key in all_keys:
            new_value = _toggle_profile_bool(profile_name, key, all_keys[key])
            sublime.status_message(
                "Ai terminal: %s.%s = %s"
                % (profile_name, key, "on" if new_value else "off")
            )
            _add_close_toolbar(term)
        elif key in reattach_keys:
            _toggle_profile_bool(profile_name, key, reattach_keys[key])
            # Reuse the exact respawn machinery AiTerminalTuneProfileCommand
            # uses for these keys, rather than duplicating it here.
            window = view.window()
            if window is not None:
                AiTerminalTuneProfileCommand(window)._respawn(
                    view, term, profile_name
                )

    def is_enabled(self):
        term = _Terminal.from_id(self.view.id())
        return term is not None and bool(_term_profile_name(term))


class AiTerminalTogglePanelCommand(sublime_plugin.TextCommand):
    """Move the live terminal between a normal tab and the bottom output
    panel (like Sublime's own Find/Console), keeping the same PTY running.

    Bound to ctrl+alt+m inside an Ai terminal view. Also in the command
    palette ("Ai Terminal: Toggle Tab ⇄ Panel") -- is_enabled() below greys
    it out when the active view isn't a live terminal, same as any other
    context-dependent palette entry.
    """

    def is_enabled(self):
        return _Terminal.from_id(self.view.id()) is not None

    def run(self, edit):
        term = _Terminal.from_id(self.view.id())
        if term is None:
            return
        window = self.view.window()
        if window is None:
            return
        if term.panel_name:
            self._to_tab(window, term)
        else:
            self._to_panel(window, term)

    def _to_panel(self, window, term):
        # Reuse this terminal's own previous panel (same name -> same
        # underlying panel view) whenever it has one, rather than minting a
        # new name every round trip: window.get_output_panel() returns the
        # SAME view for a name that already exists, and that's what lets
        # Sublime remember the height the user last dragged it to. Only
        # generate a fresh name the first time this terminal ever goes to
        # panel mode (prefixed with the tab's own profile-specific name --
        # "Claude", "Codex 2", a raw DOS-profile name, etc -- rather than the
        # generic _VIEW_NAME default, so multiple agents/terminals don't all
        # collapse into indistinguishable "Ai" panels).
        panel_name = term._panel_home_name or _next_ai_panel_name(
            window, prefix=term.view.name()
        )
        term._panel_home_name = panel_name
        new_view = _terminal_panel_view(window, panel_name, profile_name=_term_profile_name(term))
        term.panel_name = panel_name
        _migrate_terminal_view(term, new_view)
        window.run_command("show_panel", {"panel": "output." + panel_name})
        window.focus_view(new_view)
        # Belt-and-suspenders: re-assert focus one tick later. Something in
        # ST's own post-command housekeeping (around show_panel and/or the
        # group-emptying side effects touched on in _migrate_terminal_view's
        # docstring) steals focus back to the editing area after this
        # command returns, leaving keystrokes to land nowhere useful (or
        # spawn a new tab) until the user clicks the panel by hand. A
        # second, deferred focus_view call is cheap insurance against that.
        sublime.set_timeout(lambda: window.focus_view(new_view), 0)
        sublime.status_message("Ai terminal: moved to panel")

    def _to_tab(self, window, term):
        panel_name = term.panel_name
        if window.active_panel() == "output." + panel_name:
            # Hide BEFORE migrating, not after: closing the panel's backing
            # view inside _migrate_terminal_view (below) re-triggers the
            # panel's visibility as a side effect if it's still shown at
            # that point, silently undoing a hide_panel called afterward.
            # Deliberately hide_panel, NOT destroy_output_panel (unlike an
            # earlier version): destroying it wipes Sublime's memory of the
            # height the user dragged it to, so the panel would reset to the
            # tiny default every single time this terminal goes back to
            # panel mode. But leaving it un-hidden means the terminal's old
            # panel stays visibly on screen -- showing stale, no-longer-
            # updating content and swallowing keystrokes -- right alongside
            # the new tab it just moved into.
            window.run_command("hide_panel", {"panel": "output." + panel_name})
        new_view = _terminal_view(window, name=panel_name, profile_name=_term_profile_name(term))
        term.panel_name = None
        _migrate_terminal_view(term, new_view)
        window.focus_view(new_view)
        sublime.status_message("Ai terminal: moved to tab")


class AiTerminalSwitchPanelCommand(sublime_plugin.WindowCommand):
    """List every open panel in this window (Console, Find, any Ai terminals
    parked in panel mode, etc.) and bring the picked one to front.

    Sublime has no built-in panel switcher -- reading the console or any
    other panel silently steals focus from whatever panel was showing
    before (e.g. an Ai terminal in panel mode), with no UI cue where it
    went. Command palette: "Ai Terminal: Switch Panel...".
    """

    _BUILTIN_LABELS = {
        "console": "Console",
        "find": "Find",
        "find_in_files": "Find in Files",
        "replace": "Replace",
        "incremental_find": "Incremental Find",
    }

    def run(self):
        window = self.window
        panels = window.panels()
        if not panels:
            sublime.status_message("Ai terminal: no panels open")
            return

        window_terms = [
            term for term in _term_registry().values() if term.view.window() == window
        ]
        ai_panel_names = {term.panel_name for term in window_terms if term.panel_name}
        # A terminal currently living in a TAB still owns its old panel (see
        # AiTerminalTogglePanelCommand._to_tab -- deliberately not destroyed,
        # so Sublime remembers its dragged height). That panel is real but
        # inert: nothing renders into it while the terminal lives elsewhere,
        # so merely show_panel-ing it just displays dead, stale content that
        # can't accept keystrokes. Track it separately so picking it can
        # actually reactivate the terminal instead of showing a corpse.
        home_terms = {
            term._panel_home_name: term
            for term in window_terms
            if term._panel_home_name and not term.panel_name
        }
        active = window.active_panel()

        rows = []
        for raw in panels:
            if raw.startswith("output."):
                name = raw[len("output."):]
                if name in ai_panel_names:
                    label, detail = "Ai Terminal — " + name, "Live PTY terminal"
                elif name in home_terms:
                    label = "Ai Terminal — " + name
                    detail = "Open as a tab -- selecting moves it back to panel"
                else:
                    label, detail = "Output — " + name, ""
            else:
                label = self._BUILTIN_LABELS.get(raw, raw.replace("_", " ").title())
                detail = ""
            annotation = "active" if raw == active else ""
            rows.append(_quick_panel_item(label, detail, annotation, sublime.KIND_AMBIGUOUS))

        def on_done(idx):
            if idx < 0:
                return
            raw = panels[idx]
            if raw.startswith("output."):
                name = raw[len("output."):]
                term = home_terms.get(name)
                if term is not None:
                    term.view.run_command("ai_terminal_toggle_panel")
                    return
            window.run_command("show_panel", {"panel": raw})
            if raw.startswith("output."):
                view = window.find_output_panel(name)
                if view is not None:
                    window.focus_view(view)

        window.show_quick_panel(
            rows, on_done, placeholder="Switch to panel", selected_index=0
        )


class AiTerminalKeypressCommand(sublime_plugin.TextCommand):
    """Forward a physical key to the PTY as the terminal byte sequence it expects.

    ST routes unbound printable keys through a direct text-input path that
    bypasses on_text_command, so the keymap binds them to this command
    instead. Every printable/special key is bound in Default.sublime-keymap
    (letters, digits, punctuation, arrows, enter, tab, space, backspace,
    insert/delete, pageup/pagedown, home/end, escape, and ctrl/alt/shift
    combinations of same), all gated by context setting.ai_terminal_view ==
    true; args carry the key name and modifier flags.

    No menu/palette entry.
    """

    def run(self, edit, key="", ctrl=False, alt=False, shift=False):
        if not key:
            return
        term = _Terminal.from_id(self.view.id())
        if term is None:
            # View still tagged as a terminal but PTY owner is gone (reload /
            # orphaned tab). Without this log, keys appear to "do nothing".
            if self.view.settings().get(_VIEW_SETTING):
                print(
                    f"[ai_terminal] keypress dropped: no PTY for view "
                    f"{self.view.id()} ({self.view.name()!r}) — tab is orphaned; "
                    f"close it and open a new terminal"
                )
            return

        if not term.pty.is_alive():
            print(
                f"[ai_terminal] keypress dropped: PTY dead for "
                f"{self.view.name()!r} (view {self.view.id()})"
            )
            return
        # Ctrl+C / Ctrl+X with an active text selection copies/cuts it instead
        # of sending SIGINT (\x03) / cut (\x18) to the PTY. No selection ->
        # forward to the PTY (interrupt / TUI cut) as before.
        if ctrl and not alt and not shift and key in ("c", "x"):
            if any(not s.empty() for s in self.view.sel()):
                self.view.run_command("copy" if key == "c" else "cut")
                return
        # Copy mode (explicit ctrl+alt+c toggle only -- see
        # AiTerminalToggleCopyModeCommand): while on, the view is pure ST
        # domain and nothing reaches the PTY except the nav keys handled
        # below and Escape to exit. This used to also auto-engage whenever
        # the ST caret merely didn't match term._last_auto_caret_pos (e.g.
        # after a click, or after any PTY-driven scrollback trim/redraw
        # shifted absolute buffer positions), which made it swallow *all*
        # keys -- including plain typing -- any time that passive signal
        # drifted, with no visible feedback. A real terminal forwards typed
        # input to the child process regardless of where the caret happens
        # to sit, so that auto-engage path was removed; only the explicit
        # toggle (or copy_mode already being on) gates this block now.
        last_auto = getattr(term, "_last_auto_caret_pos", None)
        if term.copy_mode:
            if key == "escape" and not ctrl and not alt and not shift:
                term.copy_mode = False
                self.view.set_read_only(False)
                term._user_owns_caret = False
                if last_auto is not None:
                    pos = min(last_auto, self.view.size())
                    # Guarded: on_selection_modified must not see this as a
                    # user gesture and re-latch _user_owns_caret via the
                    # _command_line_row_range(None) fallback -- see there.
                    prev_in_render = getattr(term, "_in_render", False)
                    term._in_render = True
                    try:
                        sel = self.view.sel()
                        sel.clear()
                        sel.add(sublime.Region(pos, pos))
                    finally:
                        term._in_render = prev_in_render
                _scroll_to_bottom(self.view)
                _set_auto_follow(term, True)
                sublime.status_message("Ai terminal: Text Edit Mode OFF")
                return
            if not ctrl and not alt and key in ("up", "down", "left", "right", "pageup", "pagedown", "home", "end"):
                if key in ("up", "down"):
                    self.view.run_command("move", {"by": "lines", "forward": key == "down", "extend": shift})
                elif key in ("left", "right"):
                    self.view.run_command("move", {"by": "characters", "forward": key == "right", "extend": shift})
                elif key in ("pageup", "pagedown"):
                    self.view.run_command("move", {"by": "pages", "forward": key == "pagedown", "extend": shift})
                else:
                    self.view.run_command("move_to", {"to": "bol" if key == "home" else "eol", "extend": shift})
                return
            # Any other key while detached: ST domain, so it must not reach
            # the PTY. Swallow it rather than falling through to the PTY
            # forward below.
            return
        # Shift+Arrow always extends the ST selection natively -- never
        # forwarded to the PTY, unconditionally (including while positioned
        # over the live command line). Plain arrows (no shift) are untouched
        # below and keep going to the PTY so editing a typed command still
        # works. Terminus itself never had this (it forwarded shift+arrow to
        # the PTY too, relying on mouse-drag as the only way to select) --
        # this is a deliberate improvement over that, not parity with it.
        # Trade-off: a fullscreen TUI that binds its own meaning to
        # shift+arrow (e.g. a text widget's own select mode) will not see it
        # anymore -- unconditional per explicit request rather than gated on
        # _tui_like().
        if not alt and not ctrl and shift and key in ("left", "right", "up", "down"):
            by = "characters" if key in ("left", "right") else "lines"
            self.view.run_command(
                "move", {"by": by, "forward": key in ("right", "down"), "extend": True}
            )
            return
        # Ctrl+Shift+Home/End always extends the ST selection to the start/
        # end of the buffer natively -- same unconditional treatment as
        # Shift+Arrow above, for the same reason (selecting must always
        # work). Plain Home/End and Shift+Home/Shift+End (no Ctrl) are NOT
        # covered here -- those still default to reaching the PTY (see
        # _home_end_native_enabled below) since a readline-style CLI has a
        # real use for them (jump to start/end of the typed command).
        if not alt and ctrl and shift and key in ("home", "end"):
            self.view.run_command(
                "move_to", {"to": "bof" if key == "home" else "eof", "extend": True}
            )
            return
        # Ctrl+Home/Ctrl+End (no Shift): same unconditional "jump to buffer
        # start/end" as Ctrl+Shift+Home/End above, minus extending a
        # selection. No readline-style CLI does anything meaningful with
        # plain Ctrl+Home/Ctrl+End -- unlike plain Home/End (real line-
        # editing use, see below), so this is a safe unconditional native
        # scroll, not an opt-in. (2026-08-27: added after a live report that
        # it silently did nothing -- fell through to the PTY forward below,
        # which no CLI interprets.)
        if not alt and ctrl and not shift and key in ("home", "end"):
            self.view.run_command(
                "move_to", {"to": "bof" if key == "home" else "eof", "extend": False}
            )
            return
        # Ctrl+PageUp/PageDown: always move the Sublime tab viewport. Bare
        # PageUp/Down may be routed to the TUI (page_keys_to_pty) so the app
        # can scroll its own one-frame history; Ctrl+ keeps a host escape
        # hatch so the toolbar/chrome can still be reached without flipping
        # the profile gate. Combinational on the key event -- no poll loop.
        if not alt and ctrl and not shift and key in ("pageup", "pagedown"):
            _page_scroll(self.view, term, key == "pagedown")
            _set_auto_follow(term, False)
            return
        # PageUp/PageDown: scroll ST's real scrollback like an ordinary
        # terminal emulator (same motion as dragging the minimap) -- unlike
        # Home/End, no primary-screen readline-style CLI has a legitimate use
        # for PageUp/PageDown reaching its own input line, so this is correct
        # default behavior, not an opt-in. Exceptions: a real alt-screen app
        # (vim, less, htop) via `_tui_like`, or a profile that paints in
        # place with no ST history (Grok) via `page_keys_to_pty`.
        if not alt and key in ("pageup", "pagedown") and not _page_keys_to_pty(term):
            if shift:
                # Shift+PageUp/Down extends a selection -- that has to move
                # the caret, so it keeps the old caret-relative native
                # command. Plain (no-shift) paging below does not need the
                # caret at all, so it no longer goes through this -- see
                # _page_scroll's docstring for why caret-relative paging is
                # wrong once the viewport has scrolled away from the caret.
                self.view.run_command(
                    "move", {"by": "pages", "forward": key == "pagedown", "extend": True}
                )
            else:
                _page_scroll(self.view, term, key == "pagedown")
            # 2026-08-27: this early return used to skip _set_auto_follow
            # entirely -- the *other* PageUp/PageDown branch below (reached
            # only by page_keys_to_pty profiles like Codex) already disengages
            # follow, but that code was dead for every profile that actually
            # takes *this* branch (Claude included). Net effect, live-reported:
            # the view scrolled up for one frame, then the next streaming
            # render's auto-follow snapped it right back down -- PageUp
            # looked broken and felt like the TUI was "yanking" the position.
            # Same intent as the mouse-wheel/click handlers, which already
            # disengage follow on any deliberate scroll-away gesture.
            _set_auto_follow(term, False)
            if _DEBUG:
                print(
                    f"[ai_terminal][debug] {key} -> native ST page-scroll, "
                    f"auto_follow=False (view={self.view.id()})"
                )
            return
        # Profiles that explicitly opt in (real scrollback, no line-editing,
        # e.g. gotui) get native ST paging/navigation for these keys instead
        # of the raw key code going to the PTY. Everyone else forwards to
        # the PTY so the app's own input-line cursor moves normally.
        if not alt and key in ("home", "end", "pageup", "pagedown") and _home_end_native_enabled(term):
            if key == "home":
                self.view.run_command("move_to", {"to": "bof" if ctrl else "bol", "extend": shift})
            elif key == "end":
                self.view.run_command("move_to", {"to": "eof" if ctrl else "eol", "extend": shift})
            else:
                self.view.run_command("move", {"by": "pages", "forward": key == "pagedown", "extend": shift})
            return
        # Win32-input-mode (DEC 9001): apps that enable it (confirmed: Qwen
        # Code) ignore plain xterm sequences entirely -- every key including
        # plain letters/backspace/arrows silently does nothing once it's on.
        #
        # Whole encoding decision -- mode checks, the libghostty-vt call
        # (which syncs from live native terminal state on every call), and
        # the legacy-table fallback -- happens under term._lock. This runs
        # on ST's main thread; the PTY reader thread can be mid-feed
        # (mutating private_modes / native terminal state) at the same
        # moment a key arrives, and encode_key's own internal terminal-state
        # sync race with that feed if unlocked. encode_key() is a pure FFI
        # call with no callback into Sublime or back into this lock, so
        # holding the lock here cannot deadlock against the render path.
        with term._lock:
            if 9001 in term.screen.private_modes:
                code = _encode_win32_key(key, ctrl=ctrl, alt=alt, shift=shift)
            else:
                # Try the libghostty-vt key encoder first. It syncs the live
                # terminal state (app-cursor mode, Kitty keyboard protocol
                # flags, modifyOtherKeys, alt-escape prefix) on every call,
                # so it automatically produces the correct sequence
                # regardless of what mode the child app has negotiated --
                # something the static _translate_key table cannot do.
                #
                # encode_key() returns:
                #   bytes  — success (may be b"" if the key has no output)
                #   None   — key not recognised; fall back to legacy table
                parser = term.parser if hasattr(term, "parser") else None
                ghostty_result = None
                if parser is not None and hasattr(parser, "encode_key"):
                    try:
                        ghostty_result = parser.encode_key(
                            key, ctrl=ctrl, alt=alt, shift=shift
                        )
                    except (RuntimeError, ValueError, TypeError, OSError):
                        ghostty_result = None

                if ghostty_result is not None:
                    code = ghostty_result.decode("utf-8", "surrogateescape") if ghostty_result else ""
                else:
                    # Legacy fallback: static escape-sequence tables.
                    code = _translate_key(
                        key,
                        ctrl=ctrl,
                        alt=alt,
                        shift=shift,
                        application_mode=1 in term.screen.private_modes,
                    )
        # TEMP DEBUG (ai/TODO.md "lost keystroke during permission prompt",
        # 2026-08-27): a key that should normally produce output coming back
        # empty here means BOTH the PTY write (term.send_string below) and
        # the scroll-to-bottom on the "if code:" branch are silently
        # skipped -- a plausible mechanism for "I typed text but only Enter
        # registered." Rate-limited (once per view per ~2s) so a legitimately
        # no-op key (e.g. a bare modifier) held down cannot spam the console.
        # Remove once root-caused or ruled out.
        if not code and (len(key) == 1 or key in ("enter", "return", "space", "tab", "backspace")):
            now = time.monotonic()
            last_log = getattr(term, "_empty_code_log_mono", 0.0) or 0.0
            if now - last_log > 2.0:
                term._empty_code_log_mono = now
                print(
                    "[ai_terminal] keypress produced EMPTY code — "
                    f"key={key!r} ctrl={ctrl} alt={alt} shift={shift} "
                    f"view={self.view.id()} name={self.view.name()!r} "
                    f"private_modes={sorted(term.screen.private_modes)} "
                    f"alt_screen={term.screen.alt_screen} "
                    f"mouse_tracking={term.screen.mouse_tracking}"
                )
        if code:
            # Viewport writes (scroll_to_bottom) must NOT run on keys that only
            # move within the TUI or scrollback. set_viewport_position on
            # Windows can recompute layout and, with the layout watcher, fire
            # PTY resizes that fight Claude's cursor (left/right feel "dead"
            # for a stroke or two). Printable input still re-engages follow.
            _NO_SCROLL_KEYS = frozenset((
                "pageup", "pagedown", "home", "end",
                "left", "right", "up", "down",
            ))
            kl = key.lower()
            # Terminus-style input: keys go to the PTY only. Do NOT advance a
            # host-side "optimistic" caret or force a paint before echo.
            # Pre-PTY caret/█ was the line-1 lag/flash path (July thrash); the
            # reference terminal waits on screen.cursor from the stream.
            # Bare PageUp/Down (when page_keys_to_pty) must reach the TUI
            # only -- do not move the Sublime viewport here. Ctrl+PageUp/Down
            # already owns host tab motion (early return above). Typing must
            # not yank a Ctrl+Page chrome/hunk peek either.
            tui = _tui_like(term)
            if kl in ("pageup", "pagedown"):
                _set_auto_follow(term, False)
            elif kl not in _NO_SCROLL_KEYS:
                if not tui:
                    was_following = bool(getattr(term, "_auto_follow", False))
                    _set_auto_follow(term, True)
                    if not was_following:
                        _scroll_to_bottom(self.view)
                        term._last_vp_y = self.view.viewport_position()[1]
                        term._live_anchor_y = term._last_vp_y
            term.send_string(code)


# In-memory-only ring buffer for diagnosing the rare (~daily) fast_caret
# splat bug (see memory ai-terminal-fast-caret-splat-bug): a wrong glyph
# briefly appears at the wrong position, then vanishes on the next full
# repaint. No disk writes — inspect live via eval_python (sublime-mcp) after
# noticing a glitch; deque drops oldest frames once full so it never grows.
# ~40Hz render cadence * 60s ≈ 2400 frames.
_RENDER_HISTORY = collections.deque(maxlen=2400)
_RENDER_HISTORY_FRAME_NO = [0]


def _record_render_history(view_id, patched, diffs, cur, text):
    """Record one AiTerminalRenderCommand frame for post-hoc glitch diagnosis.

    Every frame gets a cheap entry (patched flag + up to 4 diff triples).
    Every 20th frame also gets a full text snapshot so a diagnosis can see
    the surrounding buffer, not just the single changed characters.
    """
    n = _RENDER_HISTORY_FRAME_NO[0] = _RENDER_HISTORY_FRAME_NO[0] + 1
    entry = {
        "t": time.monotonic(),
        "view_id": view_id,
        "patched": patched,
        "diffs": [(i, cur[i] if i < len(cur) else None, text[i] if i < len(text) else None) for i in diffs] if diffs else [],
    }
    if n % 20 == 0:
        entry["snapshot"] = text
    _RENDER_HISTORY.append(entry)


class AiTerminalRenderCommand(sublime_plugin.TextCommand):
    """Replace the whole view with the current screen snapshot on the main thread.

    No key/menu/palette binding; invoked programmatically.

    fast_caret: when only the host cursor glyph moved (optimistic typing),
    patch the few changed characters instead of replacing the whole buffer and
    rebuild only the host-cursor region. Keeps ST responsive under burst keys.
    """

    def run(
        self,
        edit,
        text="",
        cursor=None,
        cursor_offset=-1,
        regions=None,
        fast_caret=False,
    ):
        view = self.view
        view.set_read_only(False)
        # Only re-pin to the bottom if the user is already near it, so scrolling
        # up to read scrollback isn't yanked back on the next 40ms render.
        vp = view.viewport_position()
        ve = view.viewport_extent()
        lh = view.line_height() or 20
        term = _Terminal.from_id(view.id())
        if term is not None:
            # Suppresses on_selection_modified's user-gesture detection for
            # the selection churn our own buffer patch/caret placement below
            # causes -- see on_selection_modified for why this replaced a
            # fragile offset comparison.
            term._in_render = True
        try:
            self._run(view, edit, term, vp, ve, lh, text, cursor, cursor_offset, regions, fast_caret)
        finally:
            if term is not None:
                term._in_render = False
            # set_read_only(False) above is required so _run can paint --
            # re-lock immediately after if Text Edit Mode is on, on every
            # exit path (including the early _selection_paint_blocked
            # return inside _run), so a background render triggered by new
            # PTY output can never leave the view writable behind the
            # user's back. See AiTerminalToggleCopyModeCommand for why
            # typing must not be possible in this mode.
            if term is not None and term.copy_mode:
                view.set_read_only(True)

    def _run(self, view, edit, term, vp, ve, lh, text, cursor, cursor_offset, regions, fast_caret):
        # Abort buffer mutation while the user is selecting text. Even a
        # single-char patch shifts offsets and kills a drag mid-response.
        # Critical: _do_render already cleared screen.dirty before invoking us.
        # If we return without painting, re-dirty + re-arm or the view freezes
        # on the empty pad frame until the next PTY byte (Grok often silent).
        if term is not None and _selection_paint_blocked(view, term):
            try:
                term.screen.dirty = True
            except AttributeError:
                print("[ai_terminal] render: mark screen dirty failed:\n%s"
                      % traceback.format_exc())
            try:
                term._render_pending = False
                _schedule_render(term)
            except (AttributeError, RuntimeError):
                print("[ai_terminal] render: re-arm after blocked paint failed:\n%s"
                      % traceback.format_exc())
            return

        patched = False
        cur = ""
        diffs = []
        if fast_caret and text and view.size() == len(text):
            # Diff live buffer vs new frame; host █ / reverse move is 0–2 cells.
            # Mid-line left/right: chars identical (only reverse attr moves) →
            # 0 diffs; still patch=True so we skip full view.replace.
            cur = view.substr(sublime.Region(0, view.size()))
            if len(cur) == len(text):
                for i, (a, b) in enumerate(zip(cur, text)):
                    if a != b:
                        diffs.append(i)
                        if len(diffs) > 4:
                            break
                if len(diffs) <= 4:
                    for i in diffs:
                        view.replace(edit, sublime.Region(i, i + 1), text[i])
                    patched = True
                    # Must re-apply *all* colour regions: host cursor is reverse
                    # / ai.fb.16.1 on the cell, not HOST_CURSOR_SCOPE alone.
                    # Old reverse/█ scopes left as artifacts if we only punched
                    # the permanent host key.
                    _apply_color_regions(view, regions or [])

        if not patched and text and _setting_bool("line_diff_render_enabled", True):
            # Terminus-style redraw: replace only the lines that changed
            # (Terminus rewrites dirty lines, never the whole buffer). A whole-
            # buffer replace makes Sublime lose its place and move the view on
            # its own, which the viewport fixers then fight -- the up-and-down
            # "jiggle".
            if not cur:
                cur = view.substr(sublime.Region(0, view.size()))
            old_lines = cur.split("\n")
            new_lines = text.split("\n")
            common = min(len(old_lines), len(new_lines))
            # Start offset of every old line.
            starts = []
            offset = 0
            for line in old_lines:
                starts.append(offset)
                offset += len(line) + 1
            # Lines added or removed at the end go first, at the very end of
            # the buffer, so the offsets of the lines above stay valid.
            end_of_common = starts[common - 1] + len(old_lines[common - 1])
            if len(new_lines) > common:
                view.insert(
                    edit, view.size(), "\n" + "\n".join(new_lines[common:])
                )
            elif len(old_lines) > common:
                view.erase(edit, sublime.Region(end_of_common, view.size()))
            # Then the changed lines, last line first, for the same reason.
            for i in range(common - 1, -1, -1):
                if old_lines[i] != new_lines[i]:
                    start = starts[i]
                    view.replace(
                        edit,
                        sublime.Region(start, start + len(old_lines[i])),
                        new_lines[i],
                    )
            patched = True
            _apply_color_regions(view, regions or [])

        if not patched:
            view.replace(edit, sublime.Region(0, view.size()), text)
            # Re-apply colour regions every frame: view.replace invalidates the old
            # regions, and add_regions with the same key replaces what was there.
            _apply_color_regions(view, regions or [])

        try:
            _record_render_history(view.id(), patched, diffs if patched else [], cur, text)
        except (TypeError, IndexError, RuntimeError, AttributeError):
            print("[ai_terminal] record render history failed:\n%s" % traceback.format_exc())

        vp = _compensate_trim_scroll(view, term, vp)

        rest = _host_rest_y(view)
        real_h = _real_content_height(view)
        near_bottom = (vp[1] + ve[1]) >= (rest + real_h - lh * 2)
        content_fits = real_h <= ve[1] + 0.5
        tui_owns_scroll = _tui_like(term)
        if term is not None and not tui_owns_scroll:
            # Terminus-style single anchor (ai/TODO.md, stage 2): compare
            # against _live_anchor_y, not _last_vp_y -- the latter is also
            # written by the rest-pin/settle machinery below with a
            # different meaning (rest=0.0), which corrupted this drift
            # check. _live_anchor_y has exactly one meaning: the y this
            # engine itself last actively placed the viewport at.
            # REVERTED 2026-09-07 (same day): shrank this to 2px, reasoning
            # that during active generation this render loop fires often
            # enough for _scroll_to_bottom to erase a gradual scroll's
            # per-render delta before it could accumulate past 1.5 line
            # -heights, so slow scrolling never won the race against the
            # render clock. That diagnosis was real, but the fix was wrong:
            # 2px is well within ordinary render-to-render layout jitter
            # during active typing/streaming (a few px of residual offset is
            # documented elsewhere in this file as normal ST behavior), so
            # it now misread that jitter as a deliberate scroll-away on
            # nearly every keystroke -- confirmed live as "worse jiggle
            # during typing," the exact hop-up-and-back symptom the
            # was_following check in AiTerminalKeypressCommand.run was
            # written to prevent (its own comment: "the command line
            # visibly hops up and back on every key"), because was_following
            # now read False almost every time even with no real scroll.
            # The actual race-condition fix belongs in _scroll_to_bottom
            # itself (see its range-check docstring, 2026-09-07): once it
            # tolerates the tail being visible ANYWHERE in the viewport
            # rather than forcing one exact pixel row, do_follow staying
            # true a little longer during a slow scroll no longer forces a
            # snap back on every render -- so this threshold does not need
            # to be hair-triggered to fix the race; back to 1.5 line-heights.
            if vp[1] < term._live_anchor_y - lh * 1.5:
                _set_auto_follow(term, False)
            # Deliberately no near_bottom-triggered re-engage call here
            # anymore. This render loop runs on every redraw, including
            # an idle spinner/timer redraw that changes no content and that
            # the user did nothing to trigger (Claude Code keeps redrawing
            # its footer roughly every half-second even while just sitting
            # at a permission prompt waiting for input). near_bottom was
            # being re-evaluated on every one of those redraws regardless of
            # whether the viewport had actually moved since the last check,
            # so scrolling up to review a prompt in full was fine outside a
            # ~2-line-tall zone near the true bottom, but landing inside
            # that zone got pulled the rest of the way down by the very
            # next idle redraw, with no further action from the user.
            # Reported live and confirmed explicitly unwanted: resting
            # anywhere the user chooses, including just above the tail,
            # must never auto-resume on its own. Typing already
            # unconditionally re-engages follow (see the printable-key
            # branch in AiTerminalKeypressCommand.run and the on_modified/
            # on_text_command "insert" handlers) -- that remains the way to
            # resume; proximity alone no longer does.
        do_follow = (
            (term is not None and term._auto_follow)
            if term is not None
            else near_bottom
        )
        # View rows: [top pad][TUI rows…][bottom pad]. Prefer absolute
        # cursor_offset (includes top pad) so mid-line ST selection matches
        # the block highlight and the next keystroke stays in place.
        pad = _HOST_SCROLL_PAD_LINES
        # User cursor control is the default: the render loop only auto-
        # positions the caret at the PTY's cursor (cursor_offset/cursor)
        # until term._user_owns_caret is set (by on_selection_modified,
        # a real click/nav outside our own render pass -- see there). A
        # CLI's own idea of where the cursor belongs must never override
        # what the user actually did.
        # Bisection gate (ai_terminal.sublime-settings): when disabled, the
        # render loop always re-syncs to the PTY cursor every frame,
        # unconditionally, Terminus-style -- see settings comment.
        #
        # Text Edit Mode (term.copy_mode) is an explicit, deliberate full
        # hand-off of the view to ST (see on_text_command's own comment to
        # that effect) and must win here unconditionally -- it must NOT
        # depend on the separate user_owns_caret_enabled profile setting,
        # which defaults off and governs a different, softer "let the user
        # nudge the caret during normal operation" behavior. Before this,
        # copy_mode changed keymap routing only; the render loop had no
        # idea it was on, so any render fired by new PTY output (streaming
        # agent replies keep rendering even while idle-looking) silently
        # relocated the caret back to the live PTY cursor out from under a
        # selection the user was actively building. Confirmed live
        # 2026-09-11: caret jumping to the command line unprompted, and
        # selections getting destroyed on the very next frame.
        in_copy_mode = bool(term is not None and term.copy_mode)
        keep_selection = in_copy_mode or bool(
            term is not None
            and getattr(term, "_user_owns_caret", False)
            and _setting_bool("user_owns_caret_enabled", False, profile_name=_term_profile_name(term))
        )
        if keep_selection and not in_copy_mode and term is not None and term._auto_follow:
            # Self-heal a false latch: on_selection_modified's on-command-line
            # check (_command_line_row_range / _live_cursor_row) can mis-fire
            # for TUIs with no drawn input box and a fast-changing footer
            # (e.g. Kiro's spinner during "Thinking...") -- two independent
            # row reads taken a frame apart drift by more than the ±1
            # tolerance, latching _user_owns_caret True even though the user
            # never touched anything. Once latched, the caret never gets
            # auto-positioned again (see below), so it stays frozen forever
            # and typed output appears to vanish -- reported live testing
            # Kiro (2026-08-11): "no command line... text types below
            # everything but is hidden". A single empty caret with
            # auto_follow still True (view not manually scrolled away) is
            # never a deliberate selection worth protecting, so clear the
            # latch and let normal auto-caret placement resume below.
            sel = view.sel()
            if len(sel) == 1 and sel[0].empty():
                term._user_owns_caret = False
                keep_selection = False
        if keep_selection:
            pass  # the user's own caret/selection stands; only scroll below
        elif cursor_offset is not None and int(cursor_offset) >= 0:
            _place_auto_caret(view, term, min(int(cursor_offset), view.size()))
        elif cursor is not None:
            last_real = max(0, view.rowcol(view.size())[0] - pad)
            row = min(int(cursor[0]) + pad, last_real)
            line_start = view.text_point(row, 0)
            line_end = view.line(line_start).b
            _place_auto_caret(
                view, term, min(line_start + int(cursor[1]), line_end)
            )
        elif not tui_owns_scroll:
            # No cursor at all: park on the last real row (an app-owned TUI
            # keeps whatever caret it has and is only re-pinned).
            last_real = max(0, view.rowcol(view.size())[0] - pad)
            _place_auto_caret(view, term, view.text_point(last_real, 0))
        _settle_viewport(view, term, rest, tui_owns_scroll, do_follow, content_fits)
        # A full-screen app (tui_owns_scroll) was already pinned inside
        # _settle_viewport; a second identical call here (removed 2026-09-22)
        # could never change anything.
        if content_fits and not tui_owns_scroll:
            _pin_viewport_rest_dip_only(view, rest, term)


def _revive_terminal_client(term, window):
    """Reconnect one tab to its broker without ending the agent process."""
    old_view = term.view
    pty = term.pty
    pipe_name = pty.pipe_name
    profile_name = getattr(term, "profile_name", None)
    cwd = getattr(pty, "_cwd", None)
    view_name = None
    view_is_usable = False
    try:
        view_is_usable = bool(old_view and old_view.is_valid() and old_view.window())
        if view_is_usable:
            view_name = old_view.name()
    except (RuntimeError, AttributeError):
        view_is_usable = False

    with _term_lock():
        for vid, candidate in list(_term_registry().items()):
            if candidate is term:
                _term_registry().pop(vid, None)

    # BrokerPty.kill is detach-only. It closes this plugin's pipe handles but
    # leaves the broker and its child process untouched.
    pty.kill()

    if view_is_usable:
        target = old_view
    else:
        target = window.new_file()
        target.set_scratch(True)
        if view_name:
            target.set_name(view_name)
        elif profile_name:
            target.set_name(profile_name)
        else:
            target.set_name("Recovered Ai Terminal")

    target.settings().set(_VIEW_SETTING, True)
    target.settings().set(_BROKER_PIPE_SETTING, pipe_name)
    if profile_name:
        target.settings().set(_BROKER_PROFILE_SETTING, profile_name)
    if cwd:
        target.settings().set(_BROKER_CWD_SETTING, cwd)

    sublime.status_message(f"Ai terminal: reviving {pipe_name}")
    # Give the broker's input/output server loops time to observe both closed
    # client handles and create fresh pipe instances.
    sublime.set_timeout(
        lambda v=target, p=pipe_name: _reattach_broker_view(v, p), 500
    )


class AiTerminalOpenInWindowsTerminalCommand(sublime_plugin.TextCommand):
    """Move this tab's live broker session to a real Windows Terminal window.

    tools/recover_console.py is a raw VT relay over the same named pipes
    _BrokerPty itself uses -- not a new agent process, the same running one.
    The broker accepts only one connected client at a time, so this is a
    move, not a copy: this tab closes once Windows Terminal's relay client
    connects. 'Recover Session...' (Command Palette or Tab Context on any
    other tab) brings the session back into Sublime into a fresh tab later;
    the agent process itself is untouched either way.
    """

    def run(self, edit):
        term = _Terminal.from_id(self.view.id())
        if term is None or not _is_broker_pty(term.pty):
            sublime.status_message("Ai terminal: this tab has no detachable session")
            return

        wt_exe, python_exe, error = _wt_handoff_prereqs()
        if error:
            sublime.error_message(error)
            return

        if not sublime.ok_cancel_dialog(
            "This tab will close as soon as Windows Terminal connects -- "
            "only one client can hold the session at a time. The agent "
            "keeps running either way; use 'Recover Session...' to bring "
            "it back into Sublime later.",
            "Move to Windows Terminal",
        ):
            return

        pipe_name, error = _handoff_term_to_windows_terminal(term, wt_exe, python_exe)
        if error:
            sublime.error_message(error)
            return
        sublime.status_message(
            f"Ai terminal: handed off {pipe_name} to Windows Terminal"
        )

    def is_enabled(self):
        term = _Terminal.from_id(self.view.id())
        return term is not None and _is_broker_pty(term.pty)

    def is_visible(self):
        return self.is_enabled()


def _wt_handoff_prereqs():
    """Resolve (wt_exe, python_exe, error_message_or_None) once, shared by
    the single-tab and detach-all-to-Windows-Terminal commands."""
    wt_exe = _windows_terminal_exe()
    if not wt_exe:
        return None, None, (
            "Ai Terminal: Windows Terminal (wt.exe) was not found on PATH. "
            "Set the 'windows_terminal_exe' setting if it is installed "
            "somewhere auto-detect cannot see."
        )
    python_exe = _broker_python_exe()
    if not python_exe:
        return None, None, (
            "Ai Terminal: no python.exe found on PATH to run the relay "
            "script. Set the 'broker_python' setting."
        )
    return wt_exe, python_exe, None


def _handoff_term_to_windows_terminal(term, wt_exe, python_exe):
    """Launch a WT window relaying `term`'s broker pipe, then close this tab.
    Returns (pipe_name, error_message_or_None).

    Only one client (this tab, or WT's relay) can hold a broker's pipe at a
    time, so this is a move, not a copy: closing the tab is the point, not
    an afterthought -- confirmed live (2026-09-09) that leaving it open as a
    frozen, unreadable husk (its old "detach = leave a frozen tab" behavior)
    just reads as broken, especially once the command itself says "Move".
    Safe to actually close now that on_close (AiTerminalViewListener) never
    calls explicit_kill for any reason -- a real kill can only happen via
    the dedicated Kill Session/End Session commands, so this close can never
    race a real KILL against the still-live session WT is attached to (see
    test_open_in_windows_terminal_closes_the_tab_without_killing_the_handoff_session).
    """
    pty = term.pty
    pipe_name = pty.pipe_name
    try:
        subprocess.Popen(
            [wt_exe, python_exe, _recover_console_script_path(),
             "--pipe-name", pipe_name],
        )
    except OSError as exc:
        print("[ai_terminal] Windows Terminal handoff launch failed:\n%s"
              % traceback.format_exc())
        return pipe_name, f"Ai Terminal: failed to launch Windows Terminal: {exc}"

    # Must be set before kill(): kill() closes this tab's read handle via
    # CancelIoEx, which makes the reader thread's read() return exactly the
    # way a real child death does -- this is the only thing that tells
    # _read_loop and on_close this wasn't one.
    term._expected_termination_reason = "handoff"
    pty.kill()
    # Deferred, not synchronous: this is called from
    # AiTerminalOpenInWindowsTerminalCommand's own run(self, edit) while that
    # TextCommand is still executing against this same view -- confirmed
    # live (2026-09-09) that a same-command, synchronous view.close() here
    # is silently ignored, same reason AiTerminalEndSessionCommand already
    # defers its own view.close() through set_timeout.
    view = term.view
    sublime.set_timeout(lambda: view.close() if view.is_valid() else None, 0)
    return pipe_name, None


class AiTerminalDetachAllToWindowsTerminalCommand(sublime_plugin.ApplicationCommand):
    """Hand off every detachable session, in every window, to its own
    Windows Terminal window in one shot.

    A precaution for before an intentionally risky edit -- e.g. saving
    changes to sublime-mcp's own plugin package, which can trigger
    Sublime's plugin auto-reload and, in the worst case seen live
    (2026-09-05), wedge Sublime Text itself with no in-app recovery
    possible at all. Getting every live agent session out to a real OS
    window *before* that happens means a hung/killed Sublime costs you
    nothing -- the sessions were never inside it to begin with. Bring them
    all back with 'Reattach All from Windows Terminal' once Sublime is
    healthy again.
    """

    def run(self):
        with _term_lock():
            terms = list(_term_registry().values())
        terms = [t for t in terms if _is_broker_pty(t.pty)]
        if not terms:
            sublime.status_message("Ai terminal: no detachable sessions to hand off")
            return

        wt_exe, python_exe, error = _wt_handoff_prereqs()
        if error:
            sublime.error_message(error)
            return

        handed_off, failed = 0, []
        for term in terms:
            pipe_name, error = _handoff_term_to_windows_terminal(term, wt_exe, python_exe)
            if error:
                failed.append(pipe_name)
                print(f"[ai_terminal] detach-all: {error}")
            else:
                handed_off += 1

        if failed:
            sublime.error_message(
                f"Ai Terminal: handed off {handed_off} session(s); failed to "
                f"launch Windows Terminal for {len(failed)}: {', '.join(failed)}"
            )
        else:
            sublime.status_message(
                f"Ai terminal: handed off {handed_off} session(s) to Windows Terminal"
            )


def _tab_menu_term(window, group, index):
    """The _Terminal a Tab Context menu command should act on, or None."""
    view = _tab_menu_target_view(window, group, index)
    return _Terminal.from_id(view.id()) if view else None


class AiTerminalKillSessionCommand(sublime_plugin.WindowCommand):
    """End this tab's agent/shell for real, right now -- but keep the tab
    open. Deliberately not the same as a plain tab close (which kills AND
    closes): sometimes the process is done but the transcript in the tab
    is still wanted -- to read, copy, or Save As -- before the tab itself
    goes away. The tab is left read-only-looking (nothing more will ever
    be written to it) but not literally closed; close it yourself whenever.

    A WindowCommand, not a TextCommand -- see _tab_menu_target_view for why
    Tab Context.sublime-menu needs that to reach the right-clicked tab
    rather than whichever one happens to be focused.
    """

    def run(self, group=-1, index=-1):
        term = _tab_menu_term(self.window, group, index)
        if term is None or not _is_broker_pty(term.pty):
            sublime.status_message("Ai terminal: this tab has no session to kill")
            return
        pty = term.pty
        # Set before explicit_kill() blocks (below) so a fast on_close --
        # this command doesn't close the view, but the user might -- won't
        # redundantly try to kill an already-dying/dead broker again.
        term._expected_termination_reason = "killed"

        def _do_kill():
            try:
                pty.explicit_kill()
            except Exception as e:
                print(f"[ai_terminal] explicit_kill (Kill Session) failed: {e}")
            sublime.set_timeout(
                lambda: sublime.status_message(
                    "Ai terminal: session killed, tab kept open"
                ),
                0,
            )

        # explicit_kill() reconnects over a named pipe with up to ~1s of
        # WaitNamedPipeW/connect retries -- run off the main thread so a
        # slow/stale broker can't freeze Sublime's UI for that long.
        threading.Thread(target=_do_kill, daemon=True).start()

    def is_enabled(self, group=-1, index=-1):
        term = _tab_menu_term(self.window, group, index)
        return term is not None and _is_broker_pty(term.pty)

    def is_visible(self, group=-1, index=-1):
        return self.is_enabled(group, index)


class AiTerminalCloseKeepAliveCommand(sublime_plugin.WindowCommand):
    """Close this tab without ending the session -- the opposite of Kill
    Session. Same detach _BrokerPty.kill() does on any other close, just
    without the KILL a plain tab close would otherwise send: the agent/shell
    keeps running in the background, same as a hand-off to Windows Terminal,
    just with nothing on screen holding it. Recoverable later via
    'Recover Session...'.

    A WindowCommand, not a TextCommand -- see _tab_menu_target_view for why
    Tab Context.sublime-menu needs that to reach the right-clicked tab
    rather than whichever one happens to be focused.
    """

    def run(self, group=-1, index=-1):
        view = _tab_menu_target_view(self.window, group, index)
        term = _Terminal.from_id(view.id()) if view else None
        if term is None or not _is_broker_pty(term.pty):
            sublime.status_message("Ai terminal: this tab has no detachable session")
            return
        pty = term.pty
        pipe_name = pty.pipe_name
        # Must be set before kill()/close(): on_close (fired by close()
        # below) must see this and not send a real KILL -- see
        # _expected_termination_reason's definition on _Terminal.
        term._expected_termination_reason = "closed"
        pty.kill()
        view.close()
        sublime.status_message(
            f"Ai terminal: closed -- {pipe_name} still running in the background"
        )

    def is_enabled(self, group=-1, index=-1):
        term = _tab_menu_term(self.window, group, index)
        return term is not None and _is_broker_pty(term.pty)

    def is_visible(self, group=-1, index=-1):
        return self.is_enabled(group, index)


class AiTerminalEndSessionCommand(sublime_plugin.WindowCommand):
    """End a detachable session's underlying agent/shell for real, then
    close the tab -- kill and close together, in one action. A plain tab
    close already does both; this is the explicit, discoverable form of the
    same combination, reachable from Tab Context/the palette without
    needing to physically close the tab. Kill Session and Close Tab (Keep
    Session Alive) are these same two effects split apart, for when only
    one of them is wanted.

    A WindowCommand, not a TextCommand -- see _tab_menu_target_view for why
    Tab Context.sublime-menu needs that to reach the right-clicked tab
    rather than whichever one happens to be focused.
    """

    def run(self, group=-1, index=-1):
        view = _tab_menu_target_view(self.window, group, index)
        term = _Terminal.from_id(view.id()) if view else None
        if term is None or not _is_broker_pty(term.pty):
            sublime.status_message("Ai terminal: this tab has no session to end")
            return
        pty = term.pty
        # Set before explicit_kill() blocks (below): closing the view once
        # the kill completes fires on_close, which must not redundantly
        # try to kill an already-dead broker again.
        term._expected_termination_reason = "killed"

        def _do_end():
            try:
                pty.explicit_kill()
            except Exception as e:
                print(f"[ai_terminal] explicit_kill (End Session) failed: {e}")
            sublime.set_timeout(
                lambda: view.close() if view.is_valid() else None, 0
            )

        # explicit_kill() reconnects over a named pipe with up to ~1s of
        # WaitNamedPipeW/connect retries -- run off the main thread so a
        # slow/stale broker can't freeze Sublime's UI for that long.
        threading.Thread(target=_do_end, daemon=True).start()

    def is_enabled(self, group=-1, index=-1):
        term = _tab_menu_term(self.window, group, index)
        return term is not None and _is_broker_pty(term.pty)

    def is_visible(self, group=-1, index=-1):
        return self.is_enabled(group, index)


class AiTerminalRelaunchCommand(sublime_plugin.WindowCommand):
    """New Session: end this tab's current agent/shell for real, then
    launch a fresh one of the same profile in the same working directory --
    reusing this tab rather than opening a new one. The toolbar's escape
    hatch for "this session is stuck/confused, start clean" without losing
    the tab's place in the window or its group/index.

    A WindowCommand, not a TextCommand -- see _tab_menu_target_view for why
    Tab Context.sublime-menu needs that to reach the right-clicked tab
    rather than whichever one happens to be focused.
    """

    def run(self, group=-1, index=-1):
        view = _tab_menu_target_view(self.window, group, index)
        term = _Terminal.from_id(view.id()) if view else None
        if term is None:
            sublime.status_message("Ai terminal: this tab has no session to relaunch")
            return
        profile_name = getattr(term, "profile_name", None)
        path = (
            view.settings().get(_BROKER_CWD_SETTING)
            or _get_working_dir(self.window)
            or os.path.expanduser("~")
        )
        pty = term.pty
        # Set before explicit_kill() blocks (below), same reasoning as Kill
        # Session/End Session: on_close (if the view happens to close for
        # some unrelated reason mid-relaunch) must not redundantly try to
        # kill an already-dead broker again.
        term._expected_termination_reason = "killed"

        def _do_relaunch():
            try:
                pty.explicit_kill()
            except Exception as e:
                print(f"[ai_terminal] explicit_kill (Relaunch) failed: {e}")

            def _restart():
                if not view.is_valid():
                    return
                with _term_lock():
                    _term_registry().pop(view.id(), None)
                if term._watcher is not None:
                    term._watcher.dispose()
                    term._watcher = None
                view.erase_phantoms(_CLOSE_TOOLBAR_PHANTOM_KEY)
                view.run_command("ai_terminal_nuke")
                for setting_name in (
                    _BROKER_PIPE_SETTING, _BROKER_PROFILE_SETTING, _BROKER_CWD_SETTING,
                ):
                    view.settings().erase(setting_name)
                s = _settings_obj()
                resolved_profile, argv, extra_env = _resolve_profile_launch(profile_name, s)
                _spawn_into_view(view, path, resolved_profile, argv, extra_env)

            # Tear down + respawn on the main thread -- everything touched
            # here (registry, phantoms, view content/settings, PTY bring-up)
            # is main-thread-only, same as _spawn's own callers.
            sublime.set_timeout(_restart, 0)

        # explicit_kill() reconnects over a named pipe with up to ~1s of
        # WaitNamedPipeW/connect retries -- run off the main thread so a
        # slow/stale broker can't freeze Sublime's UI for that long.
        threading.Thread(target=_do_relaunch, daemon=True).start()

    def is_enabled(self, group=-1, index=-1):
        return _tab_menu_term(self.window, group, index) is not None

    def is_visible(self, group=-1, index=-1):
        return self.is_enabled(group, index)


# Every profile-overridable boolean that resolves via _profile_bool/
# _setting_bool with a genuine per-event (not spawn-cached) read site --
# audited against every _profile_bool/_setting_bool/_profile_settings call
# in this file AND every key documented in ai_terminal.sublime-settings,
# not just the ones that looked TUI-relevant at a glance (that earlier,
# narrower pass wrongly dropped the "bisection gate" caret/render settings
# as internal-only; they're genuinely live and genuinely tunable, just also
# used for engineering bisection -- both true at once). Each entry is
# (key, description, true_default) -- true_default is what the setting
# resolves to when neither the profile nor the global key is set, i.e. what
# _setting_bool's own `default` argument is at that call site; most are
# False but a few (close_tab_on_exit, log_tab_text) default True.
#
# Deliberately excluded: drag_forwards_by_default -- its one call site
# (AiTerminalKeypressCommand's drag_select handling) reads
# `sublime.load_settings(_SETTINGS_NAME).get(...)` directly, global-only,
# no profile_name passed -- a per-profile override here would write
# correctly but the running code never checks it, so offering it as a tab
# toggle would lie about what it does. Separate bug, out of scope for this
# panel to paper over.
_LIVE_TUNABLE_PROFILE_KEYS = (
    ("mouse_handling", "Send clicks and drags to apps that ask for the mouse, in the mode they ask for; apps that do not ask keep Sublime's clicks and selection", False),
    ("page_keys_to_pty", "Send PageUp/PageDown to the PTY instead of native Sublime scroll", False),
    ("wheel_to_pty", "Send the mouse wheel to apps that ask for the mouse, as wheel events (never keys); otherwise Sublime scrolls (defaults to following mouse_handling)", False),
    ("home_end_native", "Home/End go to native Sublime line/buffer navigation instead of the PTY", False),
    ("pin_viewport", "Hard-pin the viewport to the bottom whenever mouse-tracking is on (_tui_like)", True),
    ("force_tui_like", "Treat this app as a fullscreen TUI (pin viewport) even with no alt-screen/mouse-tracking DECSET", False),
    ("osc_title_updates_tab", "Let the app's own OSC 0/2 title-change sequences rename this tab", False),
    ("caret_footer_pinning_enabled", "Remap the display caret when the app parks its hardware cursor off the live prompt row (Claude footer-flicker fix)", False),
    ("click_to_cursor_fallback_enabled", "Synthesize arrow keys so a click repositions the app's real cursor on agents with no DEC mouse-tracking receiver", False),
    ("debug_status_bar_enabled", "Show a live follow/tui/cols×rows/vp_y status-bar readout on this tab", False),
    ("fast_caret_patch_enabled", "Diff-patch only changed cells per frame instead of a full-buffer replace (cosmetic risk: character splatter)", False),
    ("host_cursor_paint_enabled", "Paint a synthetic block cursor when the app hides its own (DECTCEM off)", False),
    ("user_owns_caret_enabled", "Let the user's own ST caret placement (not just the app's) drive rendering", False),
    ("close_tab_on_exit", "Close this tab automatically ~1.5s after its process exits", True),
    ("log_tab_text", "Keep a session text log for this tab (also gates asciicast recording upstream)", True),
)

# Settings baked in once at view/parser/_Terminal bring-up rather than
# re-read per event -- a plain live toggle would write the override
# correctly but have no visible effect until this tab's session is rebuilt.
# So unlike _LIVE_TUNABLE_PROFILE_KEYS, picking one of these in the panel
# saves the value AND immediately respawns in the same action -- see
# on_pick's `idx >= n_live` branch. Same (key, description, true_default)
# shape. Only boolean settings belong here (the panel is toggle-only);
# font_face/font_size are real spawn-fixed settings too (also refreshed by
# Respawn's reattach, see _reattach_broker_view / _apply_terminal_view_settings)
# but need a string/number editor this panel doesn't have yet, so they're
# not offered here.
_REATTACH_TUNABLE_PROFILE_KEYS = (
    ("record_asciicast", "Record this session as an asciicast -- set once when the _Terminal/SessionTextLog is constructed", True),
)
# NOT fixed by Respawn at all -- these belong to the real child process,
# fixed at its creation; only a genuine new process ("New Session
# (Relaunch)", which also discards the conversation) can change them. Not
# offered as toggles here for that reason -- listed so the "why doesn't
# Respawn pick this up" question has an answer in one place.
_RELAUNCH_REQUIRED_PROFILE_KEYS = (
    "spawn_env",
    "launch_command",
    "detachable",  # controls how *future* spawns of this profile behave, not this one
)


def _write_profile_key_to_package_file(profile_name, key, value):
    """Set one top-level boolean key of one profile in the shipped
    ai_terminal.sublime-settings by editing its text in place, so every comment
    (the VERIFIED / UNVERIFIED findings) survives; VS Code edits settings the
    same way (jsonEdit.setProperty). Never parse and re-dump the file: that
    would delete all of them. Returns True when the file was changed, False
    (nothing written) when the profile header is not found exactly once or the
    file is not a loose file on disk.

    The file's layout is fixed: a profile header sits at 8 spaces of
    indentation and its own keys at 12, so nested keys such as spawn_env
    entries (16 spaces) are never touched.
    """
    path = os.path.join(sublime.packages_path(), "GhostShell", _SETTINGS_NAME)
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines(keepends=True)

    header = '        "%s": {' % profile_name
    starts = [i for i, line in enumerate(lines) if line.rstrip() == header]
    if len(starts) != 1:
        return False
    start = starts[0]
    end = start + 1
    while end < len(lines) and lines[end].rstrip() not in ("        },", "        }"):
        end += 1
    if end >= len(lines):
        return False

    literal = "true" if value else "false"
    key_prefix = '            "%s"' % key
    for i in range(start + 1, end):
        if lines[i].startswith(key_prefix):
            body = lines[i].rstrip("\r\n")
            eol = lines[i][len(body):]
            head, colon, _old_value = body.partition(":")
            comma = "," if body.rstrip().endswith(",") else ""
            lines[i] = "%s%s %s%s%s" % (head, colon, literal, comma, eol)
            break
    else:
        eol = "\r\n" if lines[start].endswith("\r\n") else "\n"
        lines.insert(start + 1, '            "%s": %s,%s' % (key, literal, eol))

    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(lines))
    return True


def _toggle_profile_bool(profile_name, key, default):
    """Flip one boolean of profile_name and persist it. Returns the value now in
    effect (unchanged if it could not be saved).

    Where it is saved depends on "profile_tuner_writes_package_file":
    - true (the developer's own setting, in Packages/User): edit the shipped
      ai_terminal.sublime-settings, so the finding ships with the package.
      Nothing is written to Packages/User.
    - false (every end user): write ONLY this profile's changed key to the
      "profiles" block of Packages/User. _all_profiles layers that over the
      shipped profile key by key, so the block never shadows the other profiles.
    """
    global _profiles_cache
    base = _profile_settings(profile_name) or {}
    new_value = not bool(base.get(key, default))
    settings = sublime.load_settings(_SETTINGS_NAME)

    if settings.get("profile_tuner_writes_package_file", False):
        if _write_profile_key_to_package_file(profile_name, key, new_value):
            return new_value
        sublime.status_message(
            "Ai terminal: could not edit the shipped settings file for %s.%s"
            % (profile_name, key)
        )
        return not new_value

    user_profiles = _read_profiles_layer("Packages/User/" + _SETTINGS_NAME)
    one_profile = dict(user_profiles.get(profile_name) or {})
    one_profile[key] = new_value
    user_profiles[profile_name] = one_profile
    settings.set("profiles", user_profiles)
    sublime.save_settings(_SETTINGS_NAME)
    # Sublime writes the file a moment later; show the new value right away.
    # The cache is rebuilt from disk once the file's modification time moves.
    if _profiles_cache is not None and profile_name in _profiles_cache[1]:
        _profiles_cache[1][profile_name] = dict(_profiles_cache[1][profile_name], **{key: new_value})
    return new_value


class AiTerminalTuneProfileCommand(sublime_plugin.WindowCommand):
    """Tune this tab's profile settings live, in place.

    Two kinds of row, no separate "apply"/"Respawn" step to remember:

    - _LIVE_TUNABLE_PROFILE_KEYS: _mouse_handling_enabled/_page_keys_to_pty/
      _tui_like/etc. all resolve these fresh on every event rather than
      caching at spawn, so picking one just flips it -- takes effect on the
      tab's very next keypress, wheel, or redraw.
    - _REATTACH_TUNABLE_PROFILE_KEYS: baked in
      once at view/parser bring-up, so a plain toggle would save correctly
      but do nothing visible. Picking one of these saves the value AND
      immediately respawns in the same action (see on_pick's `idx >= n_live`
      branch and _respawn below) -- a fresh tab, SAME underlying agent
      process, not a new one. _respawn detaches this tab's session (not a
      kill -- the exact reattach mechanism "Recover Session..."/"Close Tab
      (Keep Session Alive)" already use) and immediately reattaches a
      brand-new view/parser to that same broker pipe; rebuilding the view
      from scratch is what picks up the just-saved setting, since
      _reattach_broker_view calls _make_parser(screen) fresh. spawn_env and
      launch_command are the genuine exception -- those
      belong to the real child process, fixed at its creation, and no amount
      of rebuilding the Sublime-side tab can change them; that really does
      need "New Session (Relaunch)" instead (which also throws the
      conversation away, unlike this).

    Saving: see _toggle_profile_bool. An end user's change puts only that
    profile's changed key in Packages/User's "profiles" block, which
    _all_profiles() layers over the shipped profile key by key. With
    "profile_tuner_writes_package_file" on (the developer) the shipped file is
    edited instead and Packages/User is not touched.

    A WindowCommand, not a TextCommand -- see _tab_menu_target_view for why
    Tab Context.sublime-menu needs that to reach the right-clicked tab
    rather than whichever one happens to be focused.
    """

    def run(self, group=-1, index=-1):
        view = _tab_menu_target_view(self.window, group, index)
        term = _Terminal.from_id(view.id()) if view else None
        if term is None:
            sublime.status_message("Ai terminal: this tab has no session to tune")
            return
        profile_name = _term_profile_name(term)
        if not profile_name:
            sublime.status_message("Ai terminal: this tab has no named profile")
            return

        current = _profile_settings(profile_name) or {}
        rows = []
        for key, desc, default in _LIVE_TUNABLE_PROFILE_KEYS:
            value = bool(current.get(key, default))
            rows.append(["%s: %s" % (key, "on" if value else "off"), desc])
        n_live = len(rows)
        # Respawn-required settings live in the same panel, not a separate
        # step to remember -- picking one saves the value AND immediately
        # respawns, since it cannot do anything until the session is
        # rebuilt anyway.
        for key, desc, default in _REATTACH_TUNABLE_PROFILE_KEYS:
            value = bool(current.get(key, default))
            rows.append([
                "%s: %s (respawns on change)" % (key, "on" if value else "off"), desc,
            ])

        def on_pick(idx):
            if idx < 0:
                return
            if idx >= n_live:
                key, _desc, default = _REATTACH_TUNABLE_PROFILE_KEYS[idx - n_live]
                self._toggle(profile_name, key, default)
                self._respawn(view, term, profile_name)
                return
            key, _desc, default = _LIVE_TUNABLE_PROFILE_KEYS[idx]
            self._toggle(profile_name, key, default)

        self.window.show_quick_panel(
            rows, on_pick, placeholder="Tune %s -- which setting?" % profile_name
        )

    def _toggle(self, profile_name, key, default):
        new_value = _toggle_profile_bool(profile_name, key, default)
        sublime.status_message(
            "Ai terminal: %s.%s = %s -- live now, no respawn needed"
            % (profile_name, key, "on" if new_value else "off")
        )

    def _respawn(self, view, term, profile_name):
        # Fresh Sublime-side tab, SAME underlying agent process -- not a new
        # spawn (that's what Relaunch already does, and it throws the
        # conversation away). This only re-does the tab/view/parser bring-up
        # (_reattach_broker_view -> _make_parser(screen) etc.), which is
        # exactly what picks up a just-saved _RESPAWN_REQUIRED_PROFILE_KEYS
        # setting like font_face/font_size. The real child process and its environment
        # are untouched -- spawn_env genuinely can't change without a real
        # new process (see _RESPAWN_REQUIRED_PROFILE_KEYS's own comment).
        window = self.window
        path = (
            view.settings().get(_BROKER_CWD_SETTING)
            or _get_working_dir(window)
            or os.path.expanduser("~")
        )
        pty = term.pty
        detachable = _is_broker_pty(pty)
        if not detachable:
            sublime.status_message(
                "Ai terminal: %s isn't a detachable session -- nothing to "
                "reattach, use New Session (Relaunch) instead" % profile_name
            )
            return
        pipe_name = pty.pipe_name
        # Same reasoning as Close Tab (Keep Session Alive): set before
        # kill() (which fires on_close) so on_close doesn't also try to
        # explicit_kill an already-detached broker.
        term._expected_termination_reason = "closed"

        def _do_respawn():
            try:
                pty.kill()  # detach only -- same process keeps running
            except Exception as e:
                print(f"[ai_terminal] kill (Respawn) failed: {e}")

            def _finish():
                if view.is_valid():
                    view.close()
                _attach_recovered_session(
                    window, pipe_name, {"profile_name": profile_name, "cwd": path}
                )

            # A short delay, not 0: kill() above closes this tab's end of
            # the pipe from a background thread (see explicit_kill's own
            # WaitNamedPipeW-retry reasoning) -- reattaching immediately on
            # the very next main-thread tick risked finding the pipe still
            # torn down, same race Recover Session's own pipe_free check
            # exists to avoid.
            sublime.set_timeout(_finish, 150)

        # Off the main thread: pty.kill()'s pipe teardown shouldn't block
        # Sublime's UI thread, same reasoning as Kill/Close Session.
        threading.Thread(target=_do_respawn, daemon=True).start()
        sublime.status_message(
            "Ai terminal: respawning %s -- same process, fresh tab" % profile_name
        )

    def is_enabled(self, group=-1, index=-1):
        return _tab_menu_term(self.window, group, index) is not None

    def is_visible(self, group=-1, index=-1):
        return self.is_enabled(group, index)


def _sublime_view_info_lines(view):
    """View/sheet/window identifiers for Session Info's Details footer --
    Sublime-internal plumbing (same category as pipe name/broker PID), not
    session state, so kept separate and always best-effort: view.sheet()
    isn't guaranteed present for every view kind, and this must never break
    the rest of the report over one field Sublime didn't provide.
    """
    lines = []
    try:
        lines.append("View ID: %s" % view.id())
    except (RuntimeError, AttributeError):
        print("[ai_terminal] view info: view.id() failed:\n%s" % traceback.format_exc())
    try:
        lines.append("Buffer ID: %s" % view.buffer_id())
    except (RuntimeError, AttributeError):
        print("[ai_terminal] view info: buffer_id() failed:\n%s" % traceback.format_exc())
    try:
        sheet = view.sheet()
        if sheet is not None:
            lines.append("Sheet ID: %s" % sheet.id())
    except (RuntimeError, AttributeError):
        print("[ai_terminal] view info: sheet() failed:\n%s" % traceback.format_exc())
    window = view.window()
    if window is not None:
        try:
            lines.append("Window ID: %s" % window.id())
        except (RuntimeError, AttributeError):
            print("[ai_terminal] view info: window.id() failed:\n%s" % traceback.format_exc())
        try:
            group, index = window.get_view_index(view)
            lines.append("Group/Index: %d/%d" % (group, index))
        except (RuntimeError, AttributeError, TypeError):
            print("[ai_terminal] view info: get_view_index() failed:\n%s" % traceback.format_exc())
    return lines


def _human_ago(seconds):
    """"3 minutes ago"-style relative time. `seconds` is elapsed, not a
    timestamp. Pure -- no I/O, so directly unit-testable."""
    seconds = max(0, int(seconds))
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return "%d seconds ago" % seconds
    minutes = seconds // 60
    if minutes < 60:
        return "%d minute%s ago" % (minutes, "" if minutes == 1 else "s")
    hours = minutes // 60
    if hours < 24:
        return "%d hour%s ago" % (hours, "" if hours == 1 else "s")
    days = hours // 24
    return "%d day%s ago" % (days, "" if days == 1 else "s")


class AiTerminalSessionInfoCommand(sublime_plugin.WindowCommand):
    """Show everything known about this tab's detachable session -- the
    non-destructive fourth option alongside Kill/Close/End: the kill x
    close matrix has one combination (neither) that needs no command since
    it's just "don't click anything", so this fills that slot with
    something actually useful instead of leaving it empty.

    Printed inline into the session's own tab via _vwrite (the same path
    used for "[process exited]"-style notices), like a TUI's own /status or
    /usage command answering in the conversation it was asked in -- not a
    separate scratch tab (tried 2026-09-02, then rejected 2026-09-10: a
    second view to close is worse than reading a dialog's small font).

    Human-relevant facts (what is this, is it still doing something) lead;
    plumbing (pipe name, broker PID) is demoted to a details footer -- also
    2026-09-02 feedback: the original ordering led with exactly the two
    fields least useful for deciding what to do with a tab.

    A WindowCommand, not a TextCommand -- see _tab_menu_target_view for why
    Tab Context.sublime-menu needs that to reach the right-clicked tab
    rather than whichever one happens to be focused.
    """

    def run(self, group=-1, index=-1):
        view = _tab_menu_target_view(self.window, group, index)
        term = _Terminal.from_id(view.id()) if view else None
        if term is None or not _is_broker_pty(term.pty):
            sublime.status_message("Ai terminal: this tab has no detachable session")
            return
        pty = term.pty
        alive = pty.is_alive()
        record = _read_broker_registry_record(pty.pipe_name)

        profile_name = getattr(term, "profile_name", None)
        lines = [
            "Profile: %s" % (profile_name or "?"),
            "Status: %s" % ("running" if alive else "frozen (disconnected)"),
        ]
        reason = getattr(term, "_expected_termination_reason", None)
        if reason:
            lines.append("Detached reason: %s" % reason)
        last_output_at = getattr(term, "_last_output_at", None)
        if last_output_at is not None:
            lines.append("Last output: %s" % _human_ago(time.time() - last_output_at))
        cwd = getattr(pty, "_cwd", None) or (record or {}).get("cwd")
        if cwd:
            lines.append("Working directory: %s" % cwd)
        if record:
            child = record.get("child_argv") or []
            if isinstance(child, list):
                child = " ".join(str(part) for part in child)
            if child:
                lines.append("Child command: %s" % child)
        elif alive:
            lines.append("(broker registry record not found -- may have exited)")

        banner = "\n--- Session Info ---\n" + "\n".join(lines) + "\n---\n"
        _vwrite(view, banner)

    def is_enabled(self, group=-1, index=-1):
        term = _tab_menu_term(self.window, group, index)
        return term is not None and _is_broker_pty(term.pty)

    def is_visible(self, group=-1, index=-1):
        return self.is_enabled(group, index)


def _attach_recovered_session(window, pipe_name, broker):
    """Build a new terminal tab bound to an already-running broker session.

    Shared by AiTerminalRecoverSessionCommand (one session, user-picked from
    a quick panel) and AiTerminalReattachAllFromWindowsTerminalCommand (every
    WT-held/orphaned session at once) -- both just need "make a tab for this
    pipe_name," nothing else differs between them.
    """
    # Use the normal terminal-view constructor so recovered tabs receive
    # the dedicated ANSI colour scheme and every input/layout setting.
    profile = broker.get("profile_name") or "Recovered"
    target = _terminal_view(window, name=profile, profile_name=profile)
    target.settings().set(_BROKER_PIPE_SETTING, pipe_name)
    target.settings().set(_BROKER_PROFILE_SETTING, profile)
    cwd = broker.get("cwd")
    if cwd:
        target.settings().set(_BROKER_CWD_SETTING, cwd)
    sublime.status_message(f"Ai terminal: reconnecting {pipe_name}")
    _reattach_broker_view(target, pipe_name)


class AiTerminalRecoverSessionCommand(sublime_plugin.WindowCommand):
    """Recover a detachable session -- one command instead of the two this
    replaces (Revive Frozen Tab / Recover Orphaned Session), which required
    knowing which applied: whether the old tab was still open (just
    disconnected) or already gone. That's plumbing, not a decision a user
    should have to make -- so this makes it itself:

    If the focused tab is itself a frozen detachable session (still open,
    just disconnected -- e.g. right after a hand-off elsewhere, or a dropped
    connection), revives it in place, exactly what Revive Frozen Tab did.
    Otherwise searches for and offers any orphaned broker not already
    attached to a usable tab -- exactly what Recover Orphaned Session did
    alone before. Closes only local named-pipe handles and never sends KILL
    to the broker either way -- deliberately different from Kill Session /
    End Session.
    """

    def run(self):
        view = self.window.active_view()
        term = _Terminal.from_id(view.id()) if view is not None else None
        if (
            term is not None
            and _is_broker_pty(term.pty)
            and not term.pty.is_alive()
        ):
            _revive_terminal_client(term, self.window)
            return
        # A broken/closed view can remove its terminal from the registry while
        # the standalone broker (and Codex child) remain alive.  Search both
        # the registry and old-generation Terminal objects retained by their
        # reader threads, then merge those with the authoritative broker
        # process list.  Process discovery can be slow on Windows, so never do
        # it on Sublime's UI thread.
        threading.Thread(target=self._discover, daemon=True).start()

    @staticmethod
    def _local_terms():
        with _term_lock():
            terms = list(_term_registry().values())
        seen = {id(term) for term in terms}
        # A terminal whose view crashed may no longer be registered, but its
        # reader/writer daemon still retains the object and its open handles.
        # Finding it lets recovery close those exact stale handles safely.
        for obj in gc.get_objects():
            try:
                if (
                    id(obj) not in seen
                    and obj.__class__.__name__ == "_Terminal"
                    and getattr(getattr(obj, "pty", None), "pipe_name", None)
                ):
                    terms.append(obj)
                    seen.add(id(obj))
            except (AttributeError, RuntimeError, ReferenceError):
                print("[ai_terminal] gc scan for stale terminals: skipped one object:\n%s"
                      % traceback.format_exc())
                continue
        return terms

    @staticmethod
    def _running_brokers():
        command = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or "
            "Name='pythonw.exe'\" | Select-Object -ExpandProperty CommandLine"
        )
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", command],
            text=True,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=8,
        )
        pattern = re.compile(
            r"agent_broker\.py.*?--pipe-name\s+(ghostshell_[0-9a-f]+)"
            r".*?--cwd\s+(\S+).*?--\s+(.+)$",
            re.IGNORECASE,
        )
        brokers = []
        for line in output.splitlines():
            match = pattern.search(line)
            if match:
                brokers.append({
                    "pipe_name": match.group(1),
                    "cwd": match.group(2),
                    "child": match.group(3).strip(),
                })
        return brokers

    def _discover(self):
        terms = self._local_terms()
        brokers = []
        error = None
        # Production brokers start with --launch-file, so their command line
        # never contains --pipe-name. The on-disk registry is authoritative.
        try:
            for record in _registered_brokers():
                pipe_name = record.get("pipe_name")
                if not pipe_name:
                    continue
                child = record.get("child_argv") or []
                if isinstance(child, list):
                    child = " ".join(str(part) for part in child)
                brokers.append({
                    "pipe_name": pipe_name,
                    "cwd": record.get("cwd") or "",
                    "child": child or "",
                    "profile_name": record.get("profile_name"),
                    "child_pid": record.get("child_pid"),
                })
        except Exception as exc:
            error = str(exc)
        if not brokers:
            try:
                brokers = self._running_brokers()
            except Exception as exc:
                if error is None:
                    error = str(exc)
        # Off the main thread (WaitNamedPipeW can block up to ~150ms each):
        # a broker being alive doesn't mean its pipe is free -- something
        # else (a WT hand-off) may already hold the one connection slot.
        # Confirmed live 2026-09-06: this command listed such a session as
        # a plain "orphaned broker" with no indication it was still busy.
        for broker in brokers:
            broker["pipe_free"] = _pipe_instance_free(broker["pipe_name"])
        sublime.set_timeout(
            lambda: self._show_choices(terms, brokers, error), 0
        )

    def _show_choices(self, terms, brokers, error=None):
        def _has_usable_view(term):
            if term is None:
                return False
            view = getattr(term, "view", None)
            try:
                return bool(view and view.is_valid() and view.window())
            except (RuntimeError, AttributeError):
                print("[ai_terminal] recover session: view usability check failed:\n%s"
                      % traceback.format_exc())
                return False

        terms_by_pipe = {
            getattr(getattr(term, "pty", None), "pipe_name", None): term
            for term in terms
            if getattr(getattr(term, "pty", None), "pipe_name", None)
        }
        sessions = []
        seen_pipes = set()
        for broker in brokers:
            pipe_name = broker["pipe_name"]
            if pipe_name in seen_pipes:
                continue
            seen_pipes.add(pipe_name)
            term = terms_by_pipe.get(pipe_name)
            # This command repairs sessions whose tab/client was lost.  A
            # broker already attached to any valid tab needs no recovery;
            # offering it here would disconnect and rebuild a healthy view.
            if not _has_usable_view(term):
                sessions.append((pipe_name, term, broker))
        # GC can retain old _Terminal objects after their brokers have exited
        # (reader/writer daemon references are enough).  When process
        # discovery succeeded, its broker list is authoritative: surfacing
        # those dead local objects as "stale/attached client" gives the user
        # sessions that cannot actually be reattached.  Fall back to local
        # terms only when Windows process discovery itself failed.
        if error is not None:
            for pipe_name, term in terms_by_pipe.items():
                if pipe_name not in seen_pipes and not _has_usable_view(term):
                    sessions.append((pipe_name, term, None))

        if not sessions:
            detail = f" ({error})" if error else ""
            sublime.status_message(
                "Ai terminal: no orphaned sessions found" + detail
            )
            return

        rows = []
        active = self.window.active_view()
        for pipe_name, term, broker in sessions:
            view = getattr(term, "view", None) if term is not None else None
            try:
                name = view.name() if view is not None and view.is_valid() else None
            except (RuntimeError, AttributeError):
                print("[ai_terminal] recover session: view.name() failed:\n%s"
                      % traceback.format_exc())
                name = None
            pipe_free = broker.get("pipe_free", True) if broker else True
            if term is None:
                state = "orphaned broker" if pipe_free else "busy — close its other window first"
            elif _same_view(view, active):
                state = "active tab"
            else:
                state = "stale/attached client"
            cwd = broker.get("cwd") if broker else getattr(
                getattr(term, "pty", None), "_cwd", ""
            )
            profile = (broker or {}).get("profile_name") if broker else None
            child_pid = (broker or {}).get("child_pid")
            pipe_detail = f"{pipe_name}  ·  pid {child_pid}" if child_pid else pipe_name
            rows.append([
                f"{name or profile or pipe_name} — {state}",
                pipe_detail,
                cwd or "",
            ])

        def _picked(index):
            if index < 0 or index >= len(sessions):
                return
            pipe_name, term, broker = sessions[index]
            if term is not None:
                self._reattach(term)
                return
            if broker and not broker.get("pipe_free", True):
                sublime.error_message(
                    "Ai Terminal: {} is still held by another client "
                    "(e.g. a Windows Terminal hand-off) -- close that "
                    "window first, then Recover Session again.".format(pipe_name)
                )
                return
            self._attach_orphan(pipe_name, broker or {})

        self.window.show_quick_panel(rows, _picked)

    def _attach_orphan(self, pipe_name, broker):
        _attach_recovered_session(self.window, pipe_name, broker)

    def _reattach(self, term):
        _revive_terminal_client(term, self.window)


class AiTerminalListSessionsCommand(sublime_plugin.WindowCommand):
    """Show every detachable ai_terminal session right now -- attached to a
    live tab in this window, attached elsewhere, frozen, or fully orphaned
    -- in one quick panel. Shares all its discovery machinery with
    AiTerminalRecoverSessionCommand, which deliberately excludes sessions
    that already have a usable tab (that command's job is repairing broken
    ones specifically); this one is the plain inventory view instead: "what
    is actually running right now, orphaned or not" (the Sessions main menu
    entry this exists for, added 2026-09-15, was specifically asked for as
    something broader than Recover Session's narrower scope).

    Picking a row focuses its tab if it already has a usable one in this
    window, otherwise reattaches/revives it exactly like Recover Session
    does for the same states.
    """

    def run(self):
        threading.Thread(target=self._discover, daemon=True).start()

    def _discover(self):
        terms = AiTerminalRecoverSessionCommand._local_terms()
        brokers = []
        error = None
        try:
            for record in _registered_brokers():
                pipe_name = record.get("pipe_name")
                if not pipe_name:
                    continue
                child = record.get("child_argv") or []
                if isinstance(child, list):
                    child = " ".join(str(part) for part in child)
                brokers.append({
                    "pipe_name": pipe_name,
                    "cwd": record.get("cwd") or "",
                    "child": child or "",
                    "profile_name": record.get("profile_name"),
                    "child_pid": record.get("child_pid"),
                })
        except Exception as exc:
            error = str(exc)
        if not brokers:
            try:
                brokers = AiTerminalRecoverSessionCommand._running_brokers()
            except Exception as exc:
                if error is None:
                    error = str(exc)
        terms_by_pipe = {
            getattr(getattr(term, "pty", None), "pipe_name", None): term
            for term in terms
            if getattr(getattr(term, "pty", None), "pipe_name", None)
        }
        pipe_free_by_pipe = {}
        for broker in brokers:
            pipe_free_by_pipe[broker["pipe_name"]] = _pipe_instance_free(broker["pipe_name"])
        # A local term with no matching broker record still needs its own
        # pipe_free check -- e.g. a just-spawned session that hasn't hit the
        # on-disk registry yet, or a stale gc-retained _Terminal object left
        # behind by a completed Windows Terminal hand-off (its view is
        # already closed, but the object lingers until GC clears it -- see
        # AiTerminalRecoverSessionCommand._local_terms). Checked here, off
        # the main thread, same reason broker pipes are (WaitNamedPipeW can
        # block up to ~150ms each).
        for pipe_name in terms_by_pipe:
            if pipe_name not in pipe_free_by_pipe:
                pipe_free_by_pipe[pipe_name] = _pipe_instance_free(pipe_name)
        sublime.set_timeout(
            lambda: self._show(terms_by_pipe, brokers, pipe_free_by_pipe, error), 0
        )

    def _show(self, terms_by_pipe, brokers, pipe_free_by_pipe, error=None):
        def _open_view(term):
            """The term's view, only if it's still a real, valid, windowed
            tab -- not just a non-None attribute. A Windows Terminal
            hand-off (_handoff_term_to_windows_terminal) closes this tab's
            view once the broker's pipe is handed to WT's relay; a stale
            gc-retained _Terminal object can outlive that close, so
            `term.view` being non-None alone does NOT mean there is
            anything left here to revive into -- confirmed live 2026-09-15:
            without this check, such a session showed as "frozen tab" and
            clicking it built a broken, unresponsive empty tab, because the
            broker's one connection slot was already held by WT.
            """
            if term is None:
                return None
            view = getattr(term, "view", None)
            try:
                if view is not None and view.is_valid():
                    return view
            except (RuntimeError, AttributeError):
                print("[ai_terminal] list sessions: view usability check failed:\n%s"
                      % traceback.format_exc())
            return None

        sessions = []
        seen_pipes = set()
        for broker in brokers:
            pipe_name = broker["pipe_name"]
            if pipe_name in seen_pipes:
                continue
            seen_pipes.add(pipe_name)
            sessions.append((pipe_name, terms_by_pipe.get(pipe_name), broker))
        # Local terms with no matching broker record (process discovery
        # failed, or a just-spawned session hasn't hit the on-disk registry
        # yet) still belong in a full inventory, unlike Recover Session
        # which only cares about broker-confirmed orphans.
        for pipe_name, term in terms_by_pipe.items():
            if pipe_name not in seen_pipes:
                sessions.append((pipe_name, term, None))
                seen_pipes.add(pipe_name)

        if not sessions:
            detail = f" ({error})" if error else ""
            sublime.status_message("Ai terminal: no sessions running" + detail)
            return

        active = self.window.active_view()
        rows = []
        for pipe_name, term, broker in sessions:
            # open_view: a genuinely still-existing view object for this
            # term, regardless of window() -- distinct from "no view object
            # at all" (a fully-stale term, or no term). windowed further
            # narrows that to "actually placed in a window right now."
            # Keeping these separate is what fixes the 2026-09-15 bug: a
            # stale gc-retained term left behind by a completed Windows
            # Terminal hand-off has no view object at all, so it must fall
            # through to the pipe_free check below (busy elsewhere vs.
            # orphaned) instead of being mislabeled "frozen tab" (which
            # implies a real view exists to revive into -- it doesn't).
            open_view = _open_view(term)
            windowed = bool(open_view and open_view.window())
            pipe_free = pipe_free_by_pipe.get(pipe_name, True)
            try:
                name = open_view.name() if open_view is not None else None
            except (RuntimeError, AttributeError):
                print("[ai_terminal] list sessions: view.name() failed:\n%s"
                      % traceback.format_exc())
                name = None
            if windowed:
                state = "active tab, this window" if _same_view(open_view, active) else "open tab, elsewhere"
            elif open_view is not None:
                # A real view object, just not in a window at this instant
                # (e.g. mid-close) -- still worth revival, unlike a fully
                # stale term with no view object at all.
                state = "frozen tab"
            elif not pipe_free:
                state = "detached -- open elsewhere (e.g. Windows Terminal)"
            else:
                state = "orphaned"
            cwd = broker.get("cwd") if broker else getattr(
                getattr(term, "pty", None), "_cwd", ""
            )
            profile = (broker or {}).get("profile_name") if broker else None
            child_pid = (broker or {}).get("child_pid")
            pipe_detail = f"{pipe_name}  ·  pid {child_pid}" if child_pid else pipe_name
            rows.append([
                f"{name or profile or pipe_name} — {state}",
                pipe_detail,
                cwd or "",
            ])

        def _picked(index):
            if index < 0 or index >= len(sessions):
                return
            pipe_name, term, broker = sessions[index]
            open_view = _open_view(term)
            if open_view is not None and open_view.window():
                if not _same_view(open_view, active):
                    self.window.focus_view(open_view)
                return
            if not pipe_free_by_pipe.get(pipe_name, True):
                # Busy elsewhere (e.g. a Windows Terminal hand-off) --
                # inform, don't touch anything: attaching/reviving here
                # would just build a second, broken client fighting for the
                # broker's one connection slot (confirmed live 2026-09-15).
                sublime.status_message(
                    "Ai Terminal: %s is open elsewhere (e.g. a Windows "
                    "Terminal hand-off) -- close that window first if you "
                    "want it back here." % pipe_name
                )
                return
            if open_view is not None:
                # A real, still-valid view with a dead pty: revive in place
                # rather than building a second tab for the same pipe.
                _revive_terminal_client(term, self.window)
                return
            _attach_recovered_session(self.window, pipe_name, broker or {})

        self.window.show_quick_panel(rows, _picked)


class AiTerminalReattachAllFromWindowsTerminalCommand(sublime_plugin.ApplicationCommand):
    """Pull every detachable session not currently in an ST tab back into
    this window, in one shot -- the mirror of 'Detach All to Windows
    Terminal'.

    Deliberately a separate command from 'Recover Session...' rather than a
    branch inside it: that command's job is picking ONE named session off a
    quick panel when the two of them are genuinely ambiguous (a frozen tab
    vs. a fully orphaned broker). This command's job is different -- there is
    nothing to pick, just "get everything back" -- and unlike Recover
    Session it also does not give up when a session is still held by a WT
    window: it sends that window's relay a DISCONNECT over the broker's
    control pipe first (see _ControlServer/_InputServer.force_disconnect in
    agent_broker.py), which both frees the pipe and makes the relay process
    exit on its own (closing that WT window/tab), then attaches here --
    no more close-the-window-then-find-Recover-Session two-step.
    """

    def run(self):
        threading.Thread(target=self._discover_and_reattach, daemon=True).start()

    def _discover_and_reattach(self):
        with _term_lock():
            terms = list(_term_registry().values())
        attached_pipes = set()
        # Frozen local terms (view still open, pty already dead -- the state
        # a WT hand-off or a crash both leave behind) get reused in place by
        # _finish instead of building a second, duplicate tab.
        frozen_terms_by_pipe = {}
        for term in terms:
            pty = getattr(term, "pty", None)
            pipe_name = getattr(pty, "pipe_name", None)
            if not pipe_name:
                continue
            # A view staying open after a WT hand-off (pty.kill() detaches
            # without closing the tab -- same "frozen tab" state as a crash)
            # must NOT count as "still attached", or a just-detached session
            # is silently excluded from targets and this command no-ops.
            try:
                if pty is not None and not pty.is_alive():
                    frozen_terms_by_pipe[pipe_name] = term
                    continue
            except (RuntimeError, AttributeError):
                pass
            view = getattr(term, "view", None)
            try:
                if view is not None and view.is_valid() and view.window():
                    attached_pipes.add(pipe_name)
            except (RuntimeError, AttributeError):
                print("[ai_terminal] reattach-all: view usability check failed:\n%s"
                      % traceback.format_exc())

        try:
            brokers = _registered_brokers()
        except Exception:
            print("[ai_terminal] reattach-all: registry read failed:\n%s"
                  % traceback.format_exc())
            brokers = []

        targets = [b for b in brokers if b.get("pipe_name") not in attached_pipes]
        if not targets:
            sublime.set_timeout(
                lambda: sublime.status_message(
                    "Ai terminal: no sessions outside Sublime to reattach"
                ), 0,
            )
            return

        reattached, still_busy = [], []
        for broker in targets:
            pipe_name = broker.get("pipe_name")
            if not pipe_name:
                continue
            if not _pipe_instance_free(pipe_name):
                _send_broker_ctl_line(pipe_name, b"DISCONNECT\n")
                # DISCONNECT only requests the cancel; the relay's own
                # ReadFile has to actually fail and its process exit before
                # the pipe instance is truly free again -- poll briefly
                # rather than assuming it happened synchronously.
                freed = False
                for _ in range(20):
                    time.sleep(0.1)
                    if _pipe_instance_free(pipe_name):
                        freed = True
                        break
                if not freed:
                    still_busy.append(pipe_name)
                    continue
            reattached.append(broker)

        sublime.set_timeout(
            lambda: self._finish(reattached, still_busy, frozen_terms_by_pipe), 0
        )

    def _finish(self, reattached, still_busy, frozen_terms_by_pipe):
        window = sublime.active_window()
        for broker in reattached:
            pipe_name = broker.get("pipe_name")
            term = frozen_terms_by_pipe.get(pipe_name)
            if term is not None:
                _revive_terminal_client(term, window)
            else:
                _attach_recovered_session(window, pipe_name, broker)
        noun = "session" if len(reattached) == 1 else "sessions"
        if still_busy:
            sublime.error_message(
                "Ai Terminal: reattached {} {}; still busy after asking to "
                "disconnect: {}".format(
                    len(reattached), noun, ", ".join(still_busy)
                )
            )
        elif reattached:
            sublime.status_message(
                f"Ai terminal: reattached {len(reattached)} {noun} from Windows Terminal"
            )


class AiTerminalNukeCommand(sublime_plugin.TextCommand):
    """Clear the view and reset the terminal screen (terminus_nuke equivalent).

    Key binding: ctrl+alt+k (context: setting.ai_terminal_view == true).
    Menu: Main.sublime-menu → Tools → Ai Utilities — "Nuke Ai Terminal".
    Command palette: "Ai: Nuke Ai Terminal".
    """

    def is_enabled(self):
        # Gate so the menu item greys out outside an ai_terminal view —
        # run() would otherwise blank any active file view.
        return bool(self.view.settings().get("ai_terminal_view"))

    def run(self, edit):
        view = self.view
        term = _Terminal.from_id(view.id())
        # Save what the tab shows into the session text log first; clearing
        # it would otherwise drop those lines from the log (see
        # SessionTextLog.keep_current_tab).
        log = getattr(term, "_text_log", None) if term else None
        if log is not None and hasattr(log, "keep_current_tab"):
            try:
                log.keep_current_tab()
            except OSError:
                print("[ai_terminal] nuke: saving tab to text log failed:\n%s"
                      % traceback.format_exc())
        view.set_read_only(False)
        view.replace(edit, sublime.Region(0, view.size()), "")
        if term:
            with term._lock:
                if hasattr(term.parser, "reset"):
                    term.parser.reset()
                else:
                    term.screen.reset()


class AiTerminalNoopCommand(sublime_plugin.TextCommand):
    """Do nothing (placeholder no-op command).

    No key/menu/palette binding; invoked programmatically.
    """

    def run(self, edit):
        pass


class AiTerminalTrackpadScrollCommand(sublime_plugin.TextCommand):
    """Receive mouse-wheel / two-finger trackpad via the mousemap.

    ST's Default (Windows) mousemap does not bind bare scroll_up/scroll_down;
    the core pans the view instead, and scroll_lines is often never fired (or
    fires once then dies when content fits). User mousemap routes those buttons
    here. On a terminal view: forward to the PTY and pin the viewport. On any
    other view: fall through to native scroll_lines so normal editors still
    scroll.
    """

    def run(self, edit, direction="up", amount=3.0):
        view = self.view
        try:
            amt = abs(float(amount))
        except (TypeError, ValueError):
            amt = 3.0
        if amt <= 0:
            amt = 1.0
        # ST scroll_lines: positive = content moves down = "scroll up"
        signed = amt if direction == "up" else -amt

        if not view.settings().get(_VIEW_SETTING):
            view.run_command("scroll_lines", {"amount": signed})
            return

        term = _Terminal.from_id(view.id())
        if term is None or not (_wheel_to_pty_enabled(term) and _mouse_goes_to_app(term)):
            view.run_command("scroll_lines", {"amount": signed})
            return

        _route_mouse_wheel(view, term, signed)
        _pin_terminal_viewport(view, term)
        sublime.set_timeout(
            lambda v=view, t=term: _pin_terminal_viewport(v, t), 0
        )


class AiTerminalDumpScreenCommand(sublime_plugin.TextCommand):
    """Print the current screen grid and cursor to the ST console for debugging.

    No key/menu/palette binding; invoked programmatically (debug).
    """

    def run(self, edit):
        term = _Terminal.from_id(self.view.id())
        if not term:
            print("[ai_terminal] no terminal for this view")
            return
        with term._lock:
            print(f"[ai_terminal] cursor=({term.screen.x},{term.screen.y}) "
                  f"size=({term.screen.cols}x{term.screen.rows}) "
                  f"alt={term.screen.alt_screen} "
                  f"sgr=fg={term.parser._fg} bg={term.parser._bg} "
                  f"flags={term.parser._flags}")
            for r, row in enumerate(term.screen.grid):
                ar = term.screen.attrs[r]
                marks = "".join("*" if a else " " for a in ar)
                print(f"  {r:2d}|{''.join(row)}|")
                print(f"     {marks}|  (attrs: * = non-default)")


# ─── hover-motion polling (mode 1003 "any-event" mouse tracking) ──────────────
#
# ST's plugin API has no continuous mouse-move event. EventListener.on_hover
# is the only mouse-position hook plugins get outside of click/drag/scroll
# commands, and it is debounced/settle-based, not a live stream -- confirmed
# empirically (2026-08-05): continuous mouse movement over an ST view produced
# only 4 on_hover calls in 10s (~every 2.4-5.1s), nowhere near real-time. Apps
# that enable xterm mode 1003 (Textual's hover-highlight, e.g. pybackup's TUI)
# need a report for every cell the cursor crosses, which on_hover cannot give.
#
# plugin_host is a real, unsandboxed Python process though, so instead of
# waiting on ST's event system this polls the actual OS cursor position via
# user32.GetCursorPos on a fast timer and forwards synthetic xterm motion
# reports directly -- independent of anything ST chooses to deliver. Windows-
# only (ctypes user32); no-ops elsewhere since ai_terminal targets Windows.
#
# Scope/known limitation: only forwards hover for window.active_view() of the
# OS foreground ST window. A terminal visible in a background pane/window
# while another view has focus will not receive hover motion -- acceptable
# because a user can only be pointing at what has focus/foreground in practice
# for this use case (hover-driven TUI widget highlighting).

# Was 33ms (~30Hz). That burned CPU for continuous DEC any-motion hover.
# 500ms is enough for sparse hover highlighting; early-exits still skip work
# when the cell has not changed or tracking is off.
_HOVER_POLL_MS = 500
_hover_poll_token = None
_hover_last_cell = {}  # view_id -> (col, row) last cell a motion report was sent for


class _POINT(ctypes.Structure):
    """Win32 POINT (windef.h): a plain (x, y) pixel pair. Used here for
    GetCursorPos (screen coordinates) and ScreenToClient's in/out conversion
    to Sublime window-client coordinates -- see _hover_poll_tick below."""
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _hover_st_hwnd():
    """HWND of the OS foreground window, if it's a Sublime Text window."""
    try:
        u32 = ctypes.windll.user32
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return None
        buf = ctypes.create_unicode_buffer(64)
        u32.GetClassNameW(hwnd, buf, 64)
        if buf.value != "PX_WINDOW_CLASS":
            return None
        return hwnd
    except (OSError, AttributeError):
        print("[ai_terminal] hover: foreground-window lookup failed:\n%s"
              % traceback.format_exc())
        return None


def _hover_poll_tick():
    """Poll the real OS cursor position (GetCursorPos, screen coords) and
    convert it to this Sublime window's client coordinates (ScreenToClient),
    to synthesize DEC any-motion mouse reports for a TUI that requested
    them -- Sublime's own mouse events do not fire on hover with no button
    held, only on click/drag, so this is the only way to get motion."""
    hwnd = _hover_st_hwnd()
    if hwnd is None:
        return
    win = sublime.active_window()
    view = win.active_view() if win else None
    if view is None:
        return
    term = _Terminal.from_id(view.id())
    if term is None:
        return
    if not _mouse_handling_enabled(term):
        return
    if getattr(term, "copy_mode", False):
        return  # Text Edit Mode: the mouse belongs to Sublime
    if int(term.screen.mouse_tracking or 0) < 1003:
        return  # clicks/drag already routed via _route_mouse_click; only "any-event" needs hover

    u32 = ctypes.windll.user32
    pt = _POINT()
    if not u32.GetCursorPos(ctypes.byref(pt)):
        return
    client = _POINT(pt.x, pt.y)
    if not u32.ScreenToClient(hwnd, ctypes.byref(client)):
        return
    try:
        text_pt = view.window_to_text((client.x, client.y))
        row, col = view.rowcol(text_pt)
    except (RuntimeError, AttributeError, TypeError):
        print("[ai_terminal] hover: point-to-text lookup failed:\n%s"
              % traceback.format_exc())
        return
    cell = _view_point_to_cell(
        row, col,
        hist_len=_mouse_hist_len(term),
        screen_rows=term.screen.rows,
        screen_cols=term.screen.cols,
    )
    if cell is None:
        return
    vid = view.id()
    if _hover_last_cell.get(vid) == cell:
        return
    _hover_last_cell[vid] = cell
    col, row = cell
    try:
        seq = _encode_pty_mouse(
            term, _BTN_RELEASE_X10, col, row, press=True, motion=True
        )
        if seq:
            term.send_string(seq)
    except Exception as e:
        print(f"[ai_terminal] hover motion send failed: {e}")


def _hover_poll_loop():
    global _hover_poll_token
    try:
        if os.name == "nt":
            _hover_poll_tick()
    except Exception as e:
        print(f"[ai_terminal] hover poll error: {e}")
    _hover_poll_token = sublime.set_timeout(_hover_poll_loop, _HOVER_POLL_MS)


# ─── viewport clamp ───────────────────────────────────────────────────────────
#
# ST's view.show() overshoots to a NEGATIVE viewport y (e.g. vp[1]=-20) when
# content fits the viewport -- it tries to "nicely" position the caret and
# overshoots because there's nothing to scroll. Our own render clamps this, but
# ST ALSO calls view.show internally on view focus/hover -- mouse entering the
# view bbox triggers it BETWEEN renders. Idle TUIs may leave the dip until the
# next frame (~500ms). This loop only corrects that NEGATIVE overshoot (and
# horizontal drift); it no longer hard-pins or converts pan into PTY scroll.
# Interval is half a second -- not 8ms -- so it does not burn main-thread
# bandwidth policing a tab the user is allowed to move.

_CLAMP_POLL_MS = 500
_clamp_token = None


def _clamp_vp_loop():
    global _clamp_token
    try:
        for _vid, term in list(_term_registry().items()):
            v = term.view
            if not v or not v.is_valid():
                continue
            # Every ai_terminal view: trackpad = core pan. Convert + pin.
            # (Previously gated on alt/mouse only; a missed mouse mode left
            # pure pan with no PTY traffic — matches empty wheel casts.)
            try:
                v.settings().set("scroll_past_end", True)
            except (RuntimeError, AttributeError):
                print("[ai_terminal] clamp-vp: scroll_past_end set failed:\n%s"
                      % traceback.format_exc())
            # Viewport HEIGHT change detector: catches show_panel/hide_panel
            # (console, find, ...), a window resize, sidebar/minimap toggle,
            # a tab-group sash drag -- anything that shrinks or grows this
            # view's viewport_extent(), not just an enumerable list of
            # commands. Nothing else in this plugin polls for this, and none
            # of it is a keystroke or new PTY output (the two things that
            # normally trigger the render loop's follow/pin recompute), so a
            # view that was scrolled/pinned to the tail at the OLD height
            # keeps that same viewport y against the new height -- reported
            # live as the console panel hiding the last several lines of a
            # tab that was following. Already-running 8ms loop, so this adds
            # no new timer.
            try:
                ve_now_h = float(v.viewport_extent()[1])
            except (RuntimeError, AttributeError, TypeError):
                ve_now_h = None
            if ve_now_h is not None:
                last_ve_h = getattr(term, "_last_ve_h", None)
                if last_ve_h is None:
                    term._last_ve_h = ve_now_h
                elif abs(ve_now_h - last_ve_h) <= 0.5:
                    # Matches the confirmed height -- also resets a candidate
                    # that didn't pan out (one noisy tick, not a real change).
                    term._ve_h_candidate = None
                    term._ve_h_candidate_count = 0
                else:
                    # Differs from the confirmed height. Requires the SAME
                    # new value on 2 consecutive clamp ticks before acting --
                    # mirrors _LayoutWatcher's own debounce/confirm pattern
                    # for the identical class of bug (that one guards PTY
                    # resize against transient content-width/scrollbar
                    # jitter; this guards a viewport write against the same
                    # kind of single-tick noise on the height axis). Without
                    # this, a real single-frame layout jitter during active
                    # typing/streaming was misread as a genuine panel/layout
                    # change on every such frame, each one forcing a
                    # _scroll_to_bottom viewport write -- reported live as
                    # jiggling and slow key response while typing.
                    candidate = getattr(term, "_ve_h_candidate", None)
                    if candidate is not None and abs(ve_now_h - candidate) <= 0.5:
                        term._ve_h_candidate_count = (
                            int(getattr(term, "_ve_h_candidate_count", 0)) + 1
                        )
                    else:
                        term._ve_h_candidate = ve_now_h
                        term._ve_h_candidate_count = 1
                    if term._ve_h_candidate_count >= 2:
                        old_ve_h = term._last_ve_h
                        term._last_ve_h = ve_now_h
                        term._ve_h_candidate = None
                        term._ve_h_candidate_count = 0
                        _resync_viewport_after_height_change(
                            v, term, old_ve_h, ve_now_h
                        )
                        continue
            try:
                vp = v.viewport_position()
                rest = _host_rest_y(v)
                dy_rest = float(vp[1]) - rest
                dx = float(vp[0])
            except (RuntimeError, AttributeError, TypeError):
                print("[ai_terminal] clamp-vp: viewport read failed:\n%s"
                      % traceback.format_exc())
                continue

            tui_like = _tui_like(term)
            try:
                le = v.layout_extent()
                ve = v.viewport_extent()
                lh = v.line_height() or 12.0
                # Near-fit relative to real content (pads always add height).
                near_fit = _real_content_height(v) <= ve[1] + lh * 2
            except (RuntimeError, AttributeError, TypeError):
                print("[ai_terminal] clamp-vp: layout measurement failed:\n%s"
                      % traceback.format_exc())
                near_fit = False
                lh = 12.0

            if tui_like:
                # No pan→PTY and no hard-pin. Users own Sublime tab motion
                # (keypad / touchpad). Only fix the negative overshoot glitch.
                if dy_rest < -0.5 or abs(dx) >= 0.5:
                    _set_viewport(v, (0.0, rest), False)
                continue

            if near_fit:
                # Short picker/command menus are usually keyboard-driven. Keep
                # them pinned, but do not reinterpret their tiny viewport drift
                # as PTY arrow input.
                #
                # dy_rest < 0 only: this branch exists to kill the specific
                # NEGATIVE-overshoot glitch documented at the top of this
                # section (view.show() briefly parking vp[1] below rest, e.g.
                # -20, when content fits the viewport) -- never a deliberate
                # user scroll, since content that fits has nothing below rest
                # to legitimately scroll INTO. dy_rest > 0 is the opposite
                # direction: the user scrolled the OTHER way, past rest,
                # toward/past the tail -- e.g. pushing a short conversation's
                # permission prompt up to read all of it. Correcting that too
                # (the old `abs(dy_rest) >= 0.5` here) undid that scroll on
                # the very next 8ms tick regardless of typing, reported live
                # as "pushing the text up during a permission prompt gets
                # overridden." Only the true glitch direction is corrected now.
                if dy_rest < -0.5 or abs(dx) >= 0.5:
                    _set_viewport(v, (0.0, rest), False)
                continue

            # Tall scrollback shell: only kill tiny overflow dips, don't steal
            # real user scrollback browsing. Same dy_rest < 0 reasoning as
            # the near_fit branch above -- a positive drift here is the user
            # scrolling toward the tail within a shell whose content is only
            # marginally taller than the viewport, not the overshoot glitch.
            if le[1] - ve[1] <= lh and (dx != 0.0 or dy_rest < -0.5):
                _set_viewport(v, (0.0, rest), False)
    except Exception as e:
        print(f"[ai_terminal] clamp loop error: {e}")
    # Was 8ms (catch pan before paint / hard-pin TUIs). Now only dip-corrects
    # and must not burn a main-thread tick that often; 500ms matches the
    # idle overshoot window documented above.
    _clamp_token = sublime.set_timeout(_clamp_vp_loop, _CLAMP_POLL_MS)


def plugin_loaded():
    if not _PTY_OK:
        print("[ai_terminal] no PTY backend available; commands will report the error.")
    global _clamp_token, _settings
    # Bind the settings object and live-apply edits (the callback fires on the
    # main thread right after a settings file write).
    _settings = sublime.load_settings(_SETTINGS_NAME)
    _apply_log_root_setting(_settings)
    _init_dynamic_color_scheme()
    _settings.add_on_change("ai_terminal", _on_settings_change)
    _report_profile_validation(_settings)
    # The registry deliberately survives module reloads so active ConPTY
    # sessions are not killed. Upgrade those objects to this generation of the
    # class as well; otherwise an existing tab keeps the old synchronous
    # send_string method and its first keypress can block Sublime in WriteFile.
    with _term_lock():
        live_terms = list(_term_registry().values())
    for term in live_terms:
        try:
            if term.__class__ is not _Terminal:
                # The existing daemon is still executing the previous class's
                # bound _write_loop.  That loop consumed encoded bytes, while
                # this generation queues text so recording and encoding happen
                # off Sublime's main thread.  Reusing the old daemon therefore
                # makes every key fail with pty.write(str), including Up/Down.
                # Retire its queue before rebinding the instance, then start a
                # writer whose loop and queue item format belong together.
                old_queue = getattr(term, "_write_queue", None)
                if old_queue is not None:
                    old_queue.put(None)
                term.__class__ = _Terminal
                term._write_queue = queue.Queue()
                term._writer = None
                old_cast_queue = getattr(term, "_input_cast_queue", None)
                if old_cast_queue is not None:
                    old_cast_queue.put(None)
                term._input_cast_queue = queue.Queue()
                term._input_cast_writer = None
            term._ensure_writer()
        except Exception as e:
            print(f"[ai_terminal] live terminal writer upgrade failed: {e}")
    if _clamp_token:
        try:
            sublime.cancel_timeout(_clamp_token)
        except (RuntimeError, AttributeError):
            print("[ai_terminal] plugin_loaded: cancel clamp timer failed:\n%s"
                  % traceback.format_exc())
    _clamp_token = sublime.set_timeout(_clamp_vp_loop, 8)
    global _hover_poll_token
    if _hover_poll_token:
        try:
            sublime.cancel_timeout(_hover_poll_token)
        except (RuntimeError, AttributeError):
            print("[ai_terminal] plugin_loaded: cancel hover timer failed:\n%s"
                  % traceback.format_exc())
    _hover_poll_token = sublime.set_timeout(_hover_poll_loop, _HOVER_POLL_MS)
    _start_layout_watcher()
    # Reconnect any detachable-profile tabs Sublime just restored from its
    # workspace session -- their agent_broker.py session may have survived
    # the restart even though this plugin instance is brand new.
    for window in sublime.windows():
        for view in window.views():
            if view.settings().get(_VIEW_SETTING):
                _apply_terminal_view_settings(view)
            _maybe_reattach_broker(view)
    print("[ai_terminal] loaded (trackpad pan→TUI scroll armed)")


def plugin_unloaded():
    global _clamp_token, _settings, _hover_poll_token
    if _settings is not None:
        try:
            _settings.clear_on_change("ai_terminal")
        except (RuntimeError, AttributeError):
            print("[ai_terminal] plugin_unloaded: clear_on_change failed:\n%s"
                  % traceback.format_exc())
    if _clamp_token:
        try:
            sublime.cancel_timeout(_clamp_token)
        except (RuntimeError, AttributeError):
            print("[ai_terminal] plugin_unloaded: cancel clamp timer failed:\n%s"
                  % traceback.format_exc())
        _clamp_token = None
    if _hover_poll_token:
        try:
            sublime.cancel_timeout(_hover_poll_token)
        except (RuntimeError, AttributeError):
            print("[ai_terminal] plugin_unloaded: cancel hover timer failed:\n%s"
                  % traceback.format_exc())
        _hover_poll_token = None
    _stop_layout_watcher()
    # Deliberately do NOT kill ConPTY children on unload.  The terminal
    # process may be opencode itself (or another long-running CLI agent);
    # killing it here means a plugin reload triggered by the agent's own
    # file deployment will murder the agent mid-session — an unrecoverable
    # crash with no error log.  The children are owned by this ST instance
    # and will be cleaned up when ST itself exits.
