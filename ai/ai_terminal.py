"""ai_terminal.py -- bare-bones owned terminal for the Claude CLI.

Replaces the Terminus dependency for AI launch. No third-party packages: pure
ctypes against the Windows ConPTY (Pseudoconsole) API, plus a small cursor-aware
ANSI renderer tailored to the subset Claude's ratatui TUI emits. Because all of
the rendering/state code is ours, every bug is fixable here.

Architecture:
  ai/terminal/  -- pure core (Screen, Parser, colours, keys, render) — unit-testable
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
import json
import os
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

# ─── ctypes ConPTY binding (guarded: a failure must not crash PluginLoader.py) ─────

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
        from ctypes.wintypes import HANDLE, DWORD, WORD, BOOL, LPCWSTR, LPBYTE, SHORT

        # wintypes does not export HRESULT; it is a signed LONG.
        HRESULT = ctypes.c_long

        _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
        _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
        _CREATE_UNICODE_ENVIRONMENT = 0x00000400
        _STARTF_USESTDHANDLES = 0x00000100

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

        # use_last_error=True so ctypes.get_last_error() works after CreateProcessW.
        # windll.kernel32 does not preserve last-error (reports 0 on failure).
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Set argtypes/restype on EVERY function -- without these ctypes truncates
        # 64-bit HANDLEs to c_int and ConPTY silently corrupts.
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
        # Buffer arg must match the read buffer type. The reader uses a
        # (c_char * N) array, so the param is POINTER(c_char) -- LPBYTE
        # (POINTER(c_ubyte)) raises "expected LP_c_byte instance instead of
        # c_char_Array_N" on the first ReadFile and kills the reader thread.
        _k32.ReadFile.argtypes = [HANDLE, POINTER(c_char), DWORD, POINTER(DWORD), c_void_p]
        _k32.ReadFile.restype = BOOL
        # write() passes a `bytes` object; c_char_p accepts bytes directly.
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
        # Named-pipe client calls, used by _BrokerPty to connect to a
        # detachable session's agent_broker.py instead of owning a ConPTY
        # directly -- see _BrokerPty below.
        _k32.CreateFileW.argtypes = [LPCWSTR, DWORD, DWORD, c_void_p, DWORD, DWORD, HANDLE]
        _k32.CreateFileW.restype = HANDLE
        _k32.WaitNamedPipeW.argtypes = [LPCWSTR, DWORD]
        _k32.WaitNamedPipeW.restype = BOOL
        _k32.CancelIoEx.argtypes = [HANDLE, c_void_p]
        _k32.CancelIoEx.restype = BOOL
        _INVALID_HANDLE_VALUE = HANDLE(-1).value

        _STILL_ACTIVE = 259
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
        except Exception:
            # Losing the wait would park the reader in ReadFile forever, so still
            # close the pseudoconsole and say why the watcher gave up early.
            print("[ai_terminal] exit watcher failed:\n%s" % traceback.format_exc())
        self._close_pc()

    def _close_pc(self):
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
        # ClosePseudoConsole emits a final frame to hOutRead; the reader drains it
        # then sees EOF. Order matters -- see plan's ConPTY pitfalls. Routed
        # through _close_pc() so this can't race the exit watcher double-closing.
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


# ─── _BrokerPty: client of a standalone tools/agent_broker.py session ────────
#
# Same interface as _Pty (start/read/write/resize/is_alive/kill, .pid) so it's
# a drop-in replacement wherever _Pty is constructed. Instead of owning a
# ConPTY directly, it connects to a named pipe served by a separate
# agent_broker.py process. That process is spawned with
# DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB so it survives Sublime Text
# closing (verified live on this machine: a process spawned this way from
# inside ST's own process tree stayed running, and its pipe stayed answering,
# across a real ST restart). kill() here only disconnects this client -- the
# whole point of a detachable session is that closing the tab must NOT kill
# the agent; ending it for real goes through explicit_kill().

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_ERROR_PIPE_BUSY = 231
_ERROR_NO_DATA = 232
_ERROR_FILE_NOT_FOUND = 2
_ERROR_OPERATION_ABORTED = 995
_DETACHED_PROCESS = 0x00000008
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _broker_pipe_path(name):
    return "\\\\.\\pipe\\" + name


def _broker_script_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "agent_broker.py")


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
    --pipe-name; only if that fails does it spawn a new detached broker and
    retry. This makes the same code path serve both a fresh session and
    reattaching to one that survived a Sublime restart.
    """

    def __init__(self, pipe_name, argv, cwd, cols, rows, env):
        self.pipe_name = pipe_name
        self.argv = list(argv)
        self.pid = 0
        self._cwd = cwd or None
        self._env = env
        self._cols = cols
        self._rows = rows
        self._h_out = None
        self._h_in = None
        self._h_ctl = None
        self._alive = False
        self._io_lock = threading.Lock()

    def _try_connect(self, path, access, timeout_s):
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

        h_out = self._try_connect(out_path, _GENERIC_READ, 0.3)
        if h_out is None:
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
        python_exe = _broker_python_exe()
        if not python_exe:
            raise OSError(
                "no Python interpreter found to run tools/agent_broker.py -- "
                "install Python and ensure it's on PATH, or set the "
                "`broker_python` setting to its full path"
            )
        cmd = [
            python_exe, _broker_script_path(),
            "--pipe-name", self.pipe_name,
            "--cols", str(self._cols), "--rows", str(self._rows),
        ]
        if self._cwd:
            cmd += ["--cwd", self._cwd]
        cmd += ["--"] + self.argv
        # The child's full environment (including any resolved API keys) is
        # passed via Popen's env= -- NOT as --env KEY=VALUE command-line
        # arguments. A process's command line is readable by any other
        # process on the machine (tasklist, wmic, Task Manager's "Command
        # line" column) -- passing secrets that way leaks them locally.
        # agent_broker.py's own --env flag still exists for its own small,
        # explicit manual overrides; it's just unused here.
        subprocess.Popen(
            cmd,
            env=self._env,
            creationflags=(_DETACHED_PROCESS | _CREATE_BREAKAWAY_FROM_JOB
                           | _CREATE_NEW_PROCESS_GROUP),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            close_fds=True,
        )

    def read(self, on_data):
        buf = (c_char * 8192)()
        n = DWORD(0)
        while self._alive:
            ok = _k32.ReadFile(self._h_out, buf, 8192, byref(n), None)
            if not ok:
                err = ctypes.get_last_error()
                self._alive = False
                if err in (0, _ERROR_HANDLE_EOF, _ERROR_BROKEN_PIPE, _ERROR_NO_DATA,
                           _ERROR_OPERATION_ABORTED):
                    return
                raise OSError("ReadFile on broker output pipe failed (GetLastError %d)" % err)
            if n.value == 0:
                break
            on_data(bytes(buf[: n.value]))
        self._alive = False

    def write(self, data):
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
        self._cols, self._rows = cols, rows
        if self._h_ctl is None:
            return False
        line = ("RESIZE %d %d\n" % (cols, rows)).encode("utf-8")
        written = DWORD(0)
        with self._io_lock:
            ok = _k32.WriteFile(self._h_ctl, line, len(line), byref(written), None)
        return bool(ok)

    def is_alive(self):
        return self._alive

    def kill(self):
        """Disconnect this client only. The broker and its agent process keep
        running -- that's the point of a detachable session. See
        explicit_kill() to actually end the session."""
        self._alive = False
        for h in (self._h_out, self._h_in, self._h_ctl):
            if h is not None:
                # CancelIoEx unblocks the reader thread's pending ReadFile on
                # this handle from here (a different thread). Without this,
                # CloseHandle can hang the calling thread indefinitely if a
                # blocking ReadFile on the same handle is still pending on
                # another thread -- reproduced live: froze Sublime's main
                # thread solid when called there directly.
                _k32.CancelIoEx(h, None)
                _k32.CloseHandle(h)
        self._h_out = self._h_in = self._h_ctl = None

    def explicit_kill(self):
        """End the underlying agent/shell for real (not just disconnect)."""
        h = self._h_ctl or self._try_connect(
            _broker_pipe_path(self.pipe_name + "-ctl"), _GENERIC_WRITE, 1.0)
        if h is not None:
            line = b"KILL\n"
            written = DWORD(0)
            _k32.WriteFile(h, line, len(line), byref(written), None)
        self.kill()


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
                pass
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                pass
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1


# ─── pure terminal core (testable without Sublime) ───────────────────────────
# Screen, Parser, colours, keys, and text layout live in ai/terminal/*.
# This file is the Sublime adapter: ConPTY, view I/O, commands, color-scheme.
# Session recordings (cast, text transcript, debug logs) live in ai/terminal/.

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
        reset_update_from_text as _reset_update_from_text,
        usage_update_from_text as _usage_update_from_text,
    )
    from .terminal.agent_catalog import (
        CATALOG as _AGENT_CATALOG,
        profile_from_entry as _agent_profile_from_entry,
    )
    from .terminal.usage_scan import (
        gather_usage as _gather_usage,
        provider_for_profile as _provider_for_profile,
    )
    from .terminal import launcher as _launcher
    from .terminal import history_scan as _history_scan
    from .terminal.layout import accepted_cols as _accepted_cols, gutter_digit_delta as _gutter_digit_delta
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
        encode_click as _encode_click,
        encode_mouse as _encode_mouse,
        encode_wheel as _encode_wheel,
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
        from ai.terminal.colors import (
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
        from ai.terminal.screen import Screen as _Screen, BLANK as _BLANK
        from ai.terminal.ghostty_engine import GhosttyParser as _GhosttyParser
        from ai.terminal.keys import (
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
        from ai.terminal.pty_env import sanitize_pty_env as _sanitize_pty_env
        from ai.terminal.profile_availability import (
            command_exists as _command_exists,
            menu_caption as _menu_caption_pure,
            profile_is_available as _profile_is_available_pure,
            reset_update_from_text as _reset_update_from_text,
            usage_update_from_text as _usage_update_from_text,
        )
        from ai.terminal.agent_catalog import (
            CATALOG as _AGENT_CATALOG,
            profile_from_entry as _agent_profile_from_entry,
        )
        from ai.terminal.usage_scan import (
            gather_usage as _gather_usage,
            provider_for_profile as _provider_for_profile,
        )
        from ai.terminal import launcher as _launcher
        from ai.terminal import history_scan as _history_scan
        from ai.terminal.layout import accepted_cols as _accepted_cols, gutter_digit_delta as _gutter_digit_delta
        from ai.terminal.render import (
            HOST_CURSOR_SCOPE as _HOST_CURSOR_SCOPE,
            build_text_and_regions as _build_text_and_regions_pure,
            cursor_text_offset as _cursor_text_offset,
            paint_host_cursor as _paint_host_cursor,
            punch_host_cursor_region as _punch_host_cursor_region,
            trim_display_rows as _trim_display_rows,
        )
        from ai.terminal.caret import (
            adjust_display_caret as _adjust_display_caret,
            pad_row_for_caret as _pad_row_for_caret,
            find_prompt_row as _find_prompt_row,
            input_start_col as _input_start_col,
            field_right_limit as _field_right_limit,
        )
        from ai.terminal.mouse import (
            BTN_RELEASE_X10 as _BTN_RELEASE_X10,
            encode_click as _encode_click,
            encode_mouse as _encode_mouse,
            encode_wheel as _encode_wheel,
            st_button_to_proto as _st_button_to_proto,
            view_point_to_cell as _view_point_to_cell,
        )
        from ai.terminal.log_paths import DEBUG as _DEBUG
        from ai.terminal.color_scheme_log import color_scheme_log as _color_scheme_log
        from ai.terminal.settings_debug_log import settings_debug_log as _settings_debug_log
        from ai.terminal.raw_debug_log import debug_log as _debug_log
        from ai.terminal.cast_recorder import CastRecorder
        from ai.terminal.session_text_log import SessionTextLog
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
    except Exception:
        pass
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
    """Keep a dated snapshot under ~/data/logs/ai_terminal/scheme_backups/."""
    try:
        n = len(scheme_data.get("rules") or [])
        if n < 100:
            return
        bdir = os.path.expanduser("~/data/logs/ai_terminal/scheme_backups")
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
            except Exception:
                pass
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
                except Exception:
                    pass
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
    except Exception:
        pass

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
        from ai.terminal.colors import scope_name_for as _pure_scope
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

# Fully machine-generated, never hand-edited: rewritten wholesale by
# "Ai Terminal: Sync Detected Agent Profiles" (AiTerminalSyncAgentProfilesCommand)
# from agent_catalog.CATALOG + local PATH detection. Kept separate from
# ai_terminal.sublime-settings so a sync can never clobber a hand-tuned
# profile or the settings file's extensive comments (Settings.save() would
# silently drop them). See _all_profiles() below for the merge order.
_GENERATED_SETTINGS_NAME = "ai_terminal_agents.sublime-settings"
_generated_settings = None  # sublime.Settings; (re)bound in plugin_loaded, same as _settings


def _all_profiles(s):
    """Merge auto-detected catalog profiles under hand-tuned ones.

    Anything explicitly configured in ai_terminal.sublime-settings -- including
    a profile sharing a name with a generated one -- always wins, so a sync
    (or a re-sync after a CLI updates) never clobbers manual customization
    (a full shim path, extra spawn_env, mouse_handling overrides, etc).

    Uses the cached _generated_settings global rather than calling
    sublime.load_settings() here directly -- this runs on every keypress/
    render/mouse-event path via _mouse_handling_enabled and friends, and
    hitting the Settings API uncached on every call (potentially off the
    main thread) is what took the plugin down before this was cached.
    """
    generated = (_generated_settings or sublime.load_settings(_GENERATED_SETTINGS_NAME)).get(
        "profiles", {}
    ) or {}
    explicit = s.get("profiles", {}) or {}
    if not isinstance(generated, dict):
        generated = {}
    if not isinstance(explicit, dict):
        explicit = {}
    merged = dict(generated)
    merged.update(explicit)
    return merged


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


def _setting_number(key, default, cast=int, profile_name=None, settings=None):
    """Numeric knob resolved profile-override first, then the global settings
    key of the same name, then `default`.

    A hand-edited settings file is the only source here, so a bad value must
    never propagate as an exception into a render/resize tick.
    """
    s = _settings_obj(settings)
    profile = _profile_settings(profile_name, s)
    try:
        if profile is not None and key in profile:
            return cast(profile[key])
        return cast(s.get(key, default))
    except (TypeError, ValueError):
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
# machinery -- _scroll_to_bottom, _pin_terminal_viewport, _pin_viewport_rest,
# the render loop's do_follow write, and _clamp_vp_loop's several rest-pin
# branches -- had accumulated enough interacting special cases (footer-size
# assumptions, TUI-vs-shell branches, pan/latch state machines) that every
# targeted fix broke a different case live. Rather than keep patching that
# pile, every viewport write in this file now goes through the single
# _set_viewport choke point below, gated on this flag. False = Sublime's
# native viewport/scroll behavior applies everywhere; nothing in this engine
# ever calls set_viewport_position. Flip True to restore the old behavior
# once it's been redesigned, not patched further.
_SCROLL_MANIPULATION_ENABLED = False


def _set_viewport(view, pos, animate=False):
    """Single choke point for every view.set_viewport_position() call in this
    file -- see _SCROLL_MANIPULATION_ENABLED. No-op while disabled so the
    user's own scroll position (wheel, drag, keyboard) is never overwritten."""
    if not _SCROLL_MANIPULATION_ENABLED:
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
    (ai/terminal/screen.py, pure/testable), not here -- this just keeps the
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
    the global kill switch above when the profile doesn't set one."""
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


def _page_keys_to_pty(term):
    """Whether PageUp/PageDown should reach the PTY instead of paging the
    Sublime view.

    Default matches today's steal path: native ST page-scroll unless the
    session is `_tui_like` (real alt-screen -- vim/less/htop own pagination).
    `force_main_screen` keeps `screen.alt_screen` false even when the child
    sent DECSET 1049 (ghostty strips those sequences), so a Grok-style TUI
    is classified as a shell. Native page then moves a one-frame buffer and
    the key looks dead. Profiles that paint in place and handle their own
    scrollback (Grok Build) set "page_keys_to_pty": true.
    """
    if _tui_like(term):
        return True
    return _profile_bool(_term_profile_name(term), "page_keys_to_pty", False)


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
    return (
        bool(term.screen.mouse_tracking)
        and _mouse_handling_enabled(term)
        and _pin_viewport_enabled(term)
    )
_DEFAULT_LAUNCH_COMMAND = ["cmd.exe"] if os.name == "nt" else [
    os.environ.get("SHELL") or "/bin/bash"
]
_DEFAULT_SPAWN_ENV = {
    "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
    "CLAUDE_CODE_AI_TERMINAL_SENTINEL": "propagated",
}


def _scrollback_size(profile_name=None):
    """Scrollback line cap: per-profile override (e.g. pybackup's launcher
    profile logging past the global default) else the global setting."""
    return max(
        0,
        _setting_number(
            "scrollback_history_size", _DEFAULT_SCROLLBACK, profile_name=profile_name
        ),
    )


def _force_main_screen(profile_name=None):
    """Whether to ignore DECSET 1049 (alt screen) for a terminal.

    Global default is true (keep ST scrollback for agent CLIs). Fullscreen
    Textual apps need the real alt-screen buffer; a profile may set
    ``"force_main_screen": false`` to opt out.
    """
    return _setting_bool("force_main_screen", True, profile_name=profile_name)


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
    """Whether a plain-text, agent-readable transcript of this tab should be
    kept alongside the .cast recording. After each tab paint, lines that
    are newly visible (were not on the previous paint) are appended.
    Source is the text just written to the Sublime tab. Default true, same posture as
    record_asciicast. A profile may set ``"log_tab_text": false`` to opt out.
    """
    return _setting_bool("log_tab_text", True, profile_name=profile_name)


def _make_parser(screen, force_main_screen):
    """libghostty-vt is the sole VT engine. See ai/terminal/ghostty_engine.py."""
    return _GhosttyParser(screen, force_main_screen=force_main_screen)


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
        pass
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


def _observed_usage(profile_name):
    """(remaining percent, reset label) learned from this profile's own output.

    Both live in sys attributes so they survive a plugin reload; either half
    is None when nothing has been observed yet.
    """
    usage = getattr(sys, "_stext_ai_profile_usage", {})
    resets = getattr(sys, "_stext_ai_profile_resets", {})
    return (
        usage.get(profile_name) if isinstance(usage, dict) else None,
        resets.get(profile_name) if isinstance(resets, dict) else None,
    )


def _profile_is_exhausted(name):
    return _observed_usage(name)[0] == 0.0


def _profile_is_available(profile_name, settings=None):
    """Quota-free menu availability for a configured terminal profile.

    Never launches the CLI, contacts a provider, refreshes OAuth, or spends
    inference quota. Executable detection prevents stale menu entries from
    launching, while actual terminal output can mark any profile exhausted.
    """
    s = _settings_obj(settings)
    if not profile_name:
        profile_name = s.get("default_profile")
    profile = _profile_settings(profile_name, s)
    path = os.environ.get("Path") or os.environ.get("PATH")
    if _profile_is_exhausted(profile_name):
        return False
    return _profile_is_available_pure(profile_name, profile, path=path)


def _ensure_usage_scanner(force=False):
    """Run the usage sweep once, in the background, at plugin load.

    ``gather_usage`` asks each provider's own usage endpoint (using the OAuth
    tokens their CLIs persisted) so the menus show every rate-limit window
    (5h, weekly, ...) with exact reset times, straight from the source. No
    inference quota is spent. The sweep can take minutes when providers are
    slow/offline, hence the thread; it runs once per plugin load, not on a
    timer. Results land in sys._stext_ai_profile_scan and menus refresh
    lazily the next time they are opened.
    """
    thread = getattr(sys, "_stext_ai_usage_scan_thread", None)
    if thread is not None and thread.is_alive():
        return
    # Without force, the once-per-load contract stands: a second call (e.g. a
    # menu opening) must not re-hit every provider endpoint.
    if not force and getattr(sys, "_stext_ai_profile_scan_at", None):
        return

    def run_once():
        try:
            sys._stext_ai_profile_scan = _gather_usage()
            sys._stext_ai_profile_scan_at = time.time()
            scan = sys._stext_ai_profile_scan
            print("[ai_terminal] usage sweep done: %s" % {
                k: v.get("summary") or v.get("error") for k, v in scan.items()
            })
            for provider, data in scan.items():
                # e.g. rotated OAuth tokens that could not be written back:
                # the sweep still produced usage, but the CLI is now at risk
                # of being logged out, which the caption alone would not say.
                if data.get("warning"):
                    print("[ai_terminal] %s: %s" % (provider, data["warning"]))
        except Exception as e:
            # A sweep that dies wholesale (not one provider failing, which
            # gather_usage already reports per provider) leaves the menus
            # captioned from stale or absent data, so record the failure.
            # `e` is unbound once the except block exits, so the status text
            # has to be built here rather than inside the timeout's lambda.
            message = "ai_terminal: usage sweep failed: %s" % e
            sys._stext_ai_usage_scan_error = str(e)
            print("[ai_terminal] usage sweep failed:\n%s" % traceback.format_exc())
            sublime.set_timeout(lambda: sublime.status_message(message), 0)
        else:
            sys._stext_ai_usage_scan_error = None

    thread = threading.Thread(
        target=run_once, name="ai_terminal_usage_sweep", daemon=True
    )
    sys._stext_ai_usage_scan_thread = thread
    thread.start()


# Periodic re-sweep. Quota that was accurate at startup is misleading three
# hours into a session, which is exactly when you want to know whether to
# switch agents. The interval is a setting because the sweep hits real provider
# endpoints; 0 disables it and falls back to load-time + manual refresh only.
_DEFAULT_USAGE_REFRESH_MINUTES = 20
_usage_refresh_token = None


def _usage_refresh_interval_ms():
    minutes = _setting_number(
        "usage_refresh_minutes", _DEFAULT_USAGE_REFRESH_MINUTES, cast=float
    )
    if minutes <= 0:
        return 0
    # Floor at a minute: a tighter loop would hammer provider endpoints for no
    # useful gain, since quota windows move on the order of hours.
    return int(max(60.0, minutes * 60.0) * 1000)


def _usage_refresh_tick():
    """Re-arm and re-sweep. Runs on the main thread; the sweep itself threads."""
    global _usage_refresh_token
    interval = _usage_refresh_interval_ms()
    if not interval:
        _usage_refresh_token = None
        return
    try:
        _ensure_usage_scanner(force=True)
    except Exception as e:
        print("[ai_terminal] periodic usage sweep failed: %s" % e)
    _usage_refresh_token = sublime.set_timeout(_usage_refresh_tick, interval)


def _start_usage_refresh():
    """(Re)arm the periodic sweep, cancelling any timer from a previous load."""
    global _usage_refresh_token
    _stop_usage_refresh()
    interval = _usage_refresh_interval_ms()
    if interval:
        _usage_refresh_token = sublime.set_timeout(_usage_refresh_tick, interval)


def _stop_usage_refresh():
    global _usage_refresh_token
    if _usage_refresh_token:
        try:
            sublime.cancel_timeout(_usage_refresh_token)
        except Exception:
            pass
        _usage_refresh_token = None


def _scanned_usage_for_profile(profile_name, settings=None):
    """Background-scanned usage dict for one profile, or None."""
    scan = getattr(sys, "_stext_ai_profile_scan", None)
    if not isinstance(scan, dict) or not scan:
        return None
    profile = _profile_settings(profile_name, settings)
    provider = _provider_for_profile(profile)
    return scan.get(provider) if provider else None


def _with_reset(label, reset):
    return label + (" | resets " + reset if reset else "")


def _profile_availability_label(profile_name, settings=None):
    """Explain the locally known state without spending provider quota."""
    remaining, reset = _observed_usage(profile_name)
    scanned = _scanned_usage_for_profile(profile_name, settings)
    if remaining == 0.0:
        return _with_reset("Quota exhausted", reset)
    if not _profile_is_available(profile_name, settings):
        return "Executable unavailable"
    if isinstance(remaining, (int, float)):
        return _with_reset("%g%% remaining" % remaining, reset)
    if scanned:
        if scanned.get("summary"):
            return scanned["summary"]
        if scanned.get("error"):
            return scanned["error"]
        if isinstance(scanned.get("remaining"), (int, float)):
            return _with_reset(
                "%g%% remaining" % scanned["remaining"], scanned.get("reset")
            )
    if reset:
        return "Usage unknown | resets " + reset
    return "Installed — no usage data"


def _profile_menu_caption(profile_name, settings=None):
    """Menu caption with live-observed usage/reset status for a profile.

    Feeds `description()` on the launcher commands, so Main.sublime-menu
    entries that omit "caption" render e.g. "Claude — 64% left, resets 3h"
    or "Gemini — quota exhausted, resets Aug 5". Purely local state.
    """
    if not profile_name:
        profile_name = (
            _settings_obj(settings).get("default_profile") or "Default Profile"
        )
    remaining, reset = _observed_usage(profile_name)
    executable_ok = _profile_is_available(profile_name, settings) or remaining == 0.0
    if remaining is None and executable_ok:
        # No live-observed terminal signal yet: use the startup sweep's
        # from-the-source summary (all windows), e.g.
        # "Codex — 5h 100% left · weekly 47% left (resets in 6d 3h)".
        scanned = _scanned_usage_for_profile(profile_name, settings)
        if scanned:
            detail = scanned.get("summary") or scanned.get("error")
            if detail:
                return "%s — %s" % (profile_name, detail)
            if isinstance(scanned.get("remaining"), (int, float)):
                remaining = scanned["remaining"]
                reset = reset or scanned.get("reset")
    return _menu_caption_pure(
        profile_name, remaining=remaining, reset=reset, executable_ok=executable_ok
    )


def _record_profile_usage(profile_name, text):
    """Learn current availability from real provider output, never a probe."""
    if not profile_name:
        return
    buffers = getattr(sys, "_stext_ai_profile_usage_text", None)
    if not isinstance(buffers, dict):
        buffers = {}
        sys._stext_ai_profile_usage_text = buffers
    recent = (buffers.get(profile_name, "") + (text or ""))[-4096:]
    buffers[profile_name] = recent
    remaining = _usage_update_from_text(recent)
    if remaining is not None:
        usage = getattr(sys, "_stext_ai_profile_usage", None)
        if not isinstance(usage, dict):
            usage = {}
            sys._stext_ai_profile_usage = usage
        usage[profile_name] = remaining
    reset = _reset_update_from_text(recent)
    if reset is not None:
        resets = getattr(sys, "_stext_ai_profile_resets", None)
        if not isinstance(resets, dict):
            resets = {}
            sys._stext_ai_profile_resets = resets
        resets[profile_name] = reset


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
    except Exception:
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
# Implementations live in ai/terminal/{log_paths,color_scheme_log,
# settings_debug_log,raw_debug_log,cast_recorder,session_text_log}.py.
# Same filenames, messages, and failure handling as the former inlined copies.


def _on_settings_change():
    """Live-apply a settings edit: swap each live terminal's history deque to
    the new cap. Column bounds are picked up by the resize poller's next
    _measure (~750ms), so nothing to do here for cols."""
    _settings_debug_log(">>> _on_settings_change CALLED")

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
    # Re-arm the periodic usage sweep so a changed interval (or disabling it
    # with 0) applies without a reload.
    try:
        _start_usage_refresh()
    except Exception as e:
        _settings_debug_log(f"ERROR: usage refresh re-arm failed: {e}")
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
        self._last_cols = screen.cols
        self._last_rows = screen.rows
        # Copy mode (ctrl+alt+c / AiTerminalToggleCopyModeCommand): while
        # True, plain navigation keys move the ST caret instead of reaching
        # the PTY. See AiTerminalKeypressCommand.run for the routing.
        self.copy_mode = False
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
        # Geometry watcher for standard terminal resize behavior.
        self._watcher = _LayoutWatcher(self)

    @classmethod
    def from_id(cls, view_id):
        with _term_lock():
            return _term_registry().get(view_id)

    def prepare(self):
        """Open session logs and start the writer. Call before pty.start().

        Grok (and other TUIs) emit capability probes the instant the child
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
            log_on = bool(
                sublime.load_settings(_SETTINGS_NAME).get("record_asciicast", True)
            )
        except Exception:
            log_on = True
        log_on = log_on or _LOG_LINES
        if not log_on:
            try:
                log_on = bool((self._spawn_env or {}).get("AI_TERMINAL_LOG_LINES"))
            except Exception:
                pass
        if log_on:
            try:
                argv = self.pty.argv if hasattr(self.pty, "argv") else []
                self._cast_recorder.open(self.screen.cols, self.screen.rows, argv)
            except Exception:
                print("[ai_terminal] cast open failed:\n%s" % traceback.format_exc())
                self._cast_recorder = CastRecorder(notify=self._notify)
                self._notify("recording disabled: could not open the .cast file")
        if _log_tab_text(self.profile_name):
            try:
                self._text_log.open(time.strftime("%Y-%m-%d_%H%M%S"))
            except Exception:
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
        """Observe the final live screen, write anything still held, close.
        Safe to call more than once or before start()."""
        log = getattr(self, "_text_log", None)
        if log is None or log.file is None:
            return
        try:
            with self._lock:
                lines = self.screen.live_lines_text()
            log.flush_live_lines(lines)
        except Exception as e:
            print(f"[ai_terminal] text log final flush failed: {e}")
        finally:
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
            self.pty.read(self._on_data)
        except Exception as e:
            error = e
            print(f"[ai_terminal] reader error: {e}\n{traceback.format_exc()}")
        finally:
            self._close_text_log()
            # A reader that died must not be reported as an ordinary exit, and
            # the tab must stay open so the reason remains readable.
            notice = (
                "\n[process exited]\n"
                if error is None
                else "\n[terminal read failed: %s]\n" % error
            )
            sublime.set_timeout(lambda: _vwrite(self.view, notice), 0)
            if error is None:
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
        text = self._decoder.decode(data)
        _record_profile_usage(getattr(self, "profile_name", None), text)
        if getattr(self, "_resize_desynced", False):
            # Bytes can still drain while pty.kill() closes the handles, but
            # they cannot safely be interpreted against stale parser geometry.
            return
        with self._lock:
            try:
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

    def send_string(self, s, record=True):
        # A key command must do no I/O and acquire no recording locks on
        # Sublime's main plugin thread.  The ordered writer records and writes
        # this text in sequence. record=False for text nobody actually typed
        # (see _on_parser_write_pty) -- it still gets written to the pty, just
        # not logged as an input event.
        self._write_queue.put((s, record))

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
        if getattr(self.parser, "force_main_screen", False) and self._last_rows is not None:
            # Main-screen (no-alt-screen) mode has no real viewport height --
            # vertical space is indefinite scrollback, not a fixed fullscreen
            # page. Forwarding a row change still reaches the child as a
            # normal resize/SIGWINCH, so a fullscreen TUI (which thinks it's
            # on the alt screen) repaints its whole frame; with no alt-screen
            # erase, the old frame just scrolls into history instead of being
            # cleared -- visible as a duplicate/garbled banner. Only column
            # changes (real reflow) are ever forwarded; rows stay pinned to
            # whatever we last told the child.
            rows = self._last_rows
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
        except Exception:
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

# Persisted (view.settings() survives an ST restart via the workspace session
# file) so a detachable profile's tab can reconnect to its still-running
# agent_broker.py session after Sublime restarts -- see _BrokerPty and
# _reattach_broker_view.
_BROKER_PIPE_SETTING = "ai_terminal_broker_pipe"
_BROKER_PROFILE_SETTING = "ai_terminal_broker_profile"
_BROKER_CWD_SETTING = "ai_terminal_broker_cwd"


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
    _POLL_MS = 250

    def __init__(self, term):
        self.term = term
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
        except Exception:
            pass
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
            return
        self._candidate = None
        self._candidate_count = 0
        cols, rows = size
        # 32↔33 (and 99→100 gutter) attractor: never grow by exactly one
        # column. See ai.terminal.layout.accepted_cols and ai/TODO.md
        # "Status-line resize/rewrap loop".
        cols = _accepted_cols(self.term._last_cols, cols)
        # In main-screen mode term.resize() pins rows and only ever forwards
        # column changes (see its docstring) -- so a pure row fluctuation
        # (e.g. dragging the pane shorter) must not count as "changed" here
        # either, or every poll re-triggers resize()/re-render for a
        # dimension the child is never actually told about.
        if getattr(self.term.parser, "force_main_screen", False):
            changed = cols != self.term._last_cols
        else:
            changed = (cols, rows) != (self.term._last_cols, self.term._last_rows)
        if changed:
            self.term.resize(cols, rows)
            print(f"[ai_terminal] resized PTY to {self.term._last_cols}x{self.term._last_rows}")

    def dispose(self):
        if self._token is not None:
            try:
                sublime.cancel_timeout(self._token)
            except Exception:
                pass
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
        except Exception:
            pass
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


def _apply_terminal_view_settings(v):
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
    try:
        ts = sublime.load_settings(_SETTINGS_NAME)
        font = ts.get("terminal_font")
        if font:
            v.settings().set("font_face", font)
    except Exception:
        pass
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
    except Exception:
        v.settings().set("color_scheme",
                         "Packages/GhostShell/ai_terminal.sublime-color-scheme")
    # NOT read-only: on_text_command swallows insert/left_delete/right_delete/
    # move and forwards them to the PTY. Making the view read-only suppresses
    # keyboard `insert` before the listener fires, so real typing would do
    # nothing (only programmatic run_command("insert") bypasses the block).


def _terminal_view(window, name=None):
    v = window.new_file()
    v.set_name(name or _next_ai_name(window))
    v.set_scratch(True)
    _apply_terminal_view_settings(v)
    return v


def _terminal_panel_view(window, panel_name):
    """Panel-mode counterpart to _terminal_view. get_output_panel returns a
    cached view keyed by name -- reused as-is across toggles (the caller
    forces a full render right after, so stale panel content never shows)."""
    v = window.get_output_panel(panel_name)
    _apply_terminal_view_settings(v)
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
_RENDER_MS = 30
_RENDER_MIN_INTERVAL_MS = 30

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
    if term.screen.dirty or getattr(term, "_render_coalesce", False):
        term._render_coalesce = False
        if term.screen.dirty:
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
    except Exception:
        pass
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
    except Exception:
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
    except Exception:
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
        except Exception:
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
    except Exception:
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
        log.observe((text or "").splitlines())
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
    except Exception:
        pass


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
    if not term.screen.dirty:
        return
    # Host cursor: ST caret stays invisible when the app paints reverse-video
    # (Claude). When it does not (Grok / shells), paint_host_cursor puts a
    # white █ on the blank insertion cell (display-only). Caret row must come
    # from adjust_display_caret — Grok's live `│ >` row, not a history `>`.

    with term._lock:
        rows, cy, cx = term.screen.render_cells()
        # Bisection gate (ai_terminal.sublime-settings): raw hardware
        # position when disabled, Terminus-style -- see settings comment.
        if _setting_bool("caret_footer_pinning_enabled", False, profile_name=_term_profile_name(term)):
            cy, cx = _adjust_display_caret(term.screen, cy, cx)
        rows = _pad_row_for_caret(rows, cy, cx)
        # The Screen holds the tab's full pinned row count (force_main_screen
        # pins rows -- see _LayoutWatcher._run -- so a plain shell's mostly-
        # blank grid isn't reflowed just because there's less real content
        # yet). Rendering all of it puts a wall of blank lines below the
        # cursor. Trim trailing blanks; a cursor parked two or more rows
        # below content (Claude last-row CUP + overflow \\n) is not kept.
        # Empty prompt on the next line is. See trim_display_rows.
        rows = _trim_display_rows(rows, cy)
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
    except Exception:
        pass


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
# Raw ANSI debug log: ai/terminal/raw_debug_log.py (gated on _DEBUG).
# Asciicast v3 recording: ai/terminal/cast_recorder.py. On if
# AI_TERMINAL_LOG_LINES is set in spawn_env OR in ST's process env, or if
# the record_asciicast setting is true (default). Per stext-settings-json-strict
# the env toggle is NOT a top-level setting key; it lives in spawn_env.
# Session text logs: ai/terminal/session_text_log.py -- see _log_tab_text().
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
    except Exception:
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
    except Exception:
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
                # Enter in ST is an insert of "\n"; TUIs expect CR.
                term.send_string("\r" if chars == "\n" else chars)
            return ("ai_terminal_noop", {})
        if command == "left_delete":
            _set_auto_follow(term, True)
            _scroll_to_bottom(self.view)
            term._last_vp_y = self.view.viewport_position()[1]
            term.send_string("\x7f")
            return ("ai_terminal_noop", {})
        if command == "right_delete":
            _set_auto_follow(term, True)
            _scroll_to_bottom(self.view)
            term._last_vp_y = self.view.viewport_position()[1]
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
        except Exception:
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
            if term.copy_mode:
                term.copy_mode = False
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

        def _do_close():
            if isinstance(term.pty, _BrokerPty):
                # Closing the tab is a deliberate "end this session" action,
                # unlike Sublime quitting: hot-exit does NOT fire on_close
                # per view (confirmed live -- reattach across a real ST
                # restart works), so this is the only place a detachable
                # session should actually be ended rather than just
                # disconnected.
                try:
                    term.pty.explicit_kill()
                except Exception as e:
                    print(f"[ai_terminal] explicit_kill on tab close failed: {e}")
            term.kill()

        threading.Thread(target=_do_close, daemon=True).start()

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
    def _preclamp_vp(self):
        v = self.view
        try:
            if not v or not v.is_valid():
                return
            le = v.layout_extent()
            ve = v.viewport_extent()
            vp = v.viewport_position()
            lh = v.line_height() or 12.0
            if le[1] - ve[1] <= lh and (vp[0] != 0.0 or vp[1] != 0.0):
                _set_viewport(v, (0.0, 0.0), False)
        except Exception:
            pass

    def on_hover(self, point, hover_zone):
        self._preclamp_vp()

    def on_activated(self):
        self._preclamp_vp()
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
    except Exception:
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
        sgr = term.screen.mouse_sgr
        term.send_string(
            _encode_mouse(btn, col, row, press=False, sgr=sgr)
        )
        _MOUSE_LAST_CLICK[view_id] = (col, row, time.time())
    except Exception:
        pass


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


def _send_full_click(term, view_id, proto, col, row, sgr):
    """Press+release one click and remember it for double-tap detection."""
    if not _mouse_handling_enabled(term):
        return
    _mouse_force_release(term, view_id)
    term.send_string(_encode_click(proto, col, row, sgr=sgr))
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
    except Exception:
        pass


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
            if not t.screen.mouse_tracking or not t.pty.is_alive():
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
    if not _mouse_handling_enabled(term):
        return False
    mode = term.screen.mouse_tracking
    if not mode:
        return False
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
        seq = _encode_mouse(proto, col, row, press=True, sgr=sgr)
        gen = 1
        _MOUSE_HOLD[vid] = (proto, col, row, gen, now, False)
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
    seq = _encode_mouse(proto, col, row, press=True, motion=True, sgr=sgr)
    gen = gen_prev + 1
    _MOUSE_HOLD[vid] = (proto, col, row, gen, t0, True)
    term.send_string(seq)
    _schedule_mouse_release(view, gen, _MOUSE_DRAG_RELEASE_MS)
    return True


def _scroll_tick_count(amount):
    """How many fine scroll steps for one gesture (feel, not max throughput).

    Keep this low: stacking PageUp×N + arrows×N + dual wheel felt jumpy even
    when the *direction* was correct.
    """
    try:
        a = abs(float(amount))
    except (TypeError, ValueError):
        a = 1.0
    if a <= 0:
        return 1
    if a < 1.0:
        return 1
    if a < 2.5:
        return 2
    return min(3, int(round(a)))


def _route_mouse_wheel(view, term, amount):
    """Forward trackpad scroll to the PTY using *content-grab* semantics.

    The user drags the *text* (like grabbing the buffer), not the scroll
    thumb. That is the opposite of the TUI scroll-button, which moves the
    *view*:

      amount > 0 → text dragged downward on screen → reveal older (above)
      amount < 0 → text dragged upward on screen   → reveal newer (below)

    Internally we still emit Page/arrow/wheel "up" for older and "down" for
    newer; only the sign of `amount` vs finger motion is content-grab.

    Feel: fine steps (wheel + arrows); one Page only on a fling.
    """
    if not _mouse_handling_enabled(term):
        # Single choke point: gating every caller individually missed
        # _clamp_vp_loop's near_fit branch (fires independent of tui_like),
        # which kept sending mouse/arrow sequences to the PTY even after the
        # input-command interceptors were disabled. Confirmed live: SGR mouse
        # click/release sequences ("\x1b[<0;N;M" / "m") were still present in
        # the recorded 'i' stream for a Qwen session after that first fix.
        return True
    try:
        # amount > 0 → older history (keys/wheel "up")
        see_older = float(amount) > 0
    except (TypeError, ValueError):
        see_older = True
    n = _scroll_tick_count(amount)
    _set_auto_follow(term, False)
    term._last_scroll_send_t = time.time()

    win32 = 9001 in term.screen.private_modes
    parts = []
    if win32:
        arrow = _encode_win32_key("up" if see_older else "down")
    else:
        try:
            arrow = _get_key_code("up" if see_older else "down")
        except Exception:
            arrow = "\x1b[A" if see_older else "\x1b[B"
    parts.append(arrow * n)
    if term.screen.mouse_tracking:
        col, row = _wheel_locus(view, term)
        sgr = term.screen.mouse_sgr
        parts.append(
            "".join(
                _encode_wheel(see_older, col, row, sgr=sgr) for _ in range(n)
            )
        )
    if n >= 3:
        if win32:
            page = _encode_win32_key("pageup" if see_older else "pagedown")
        else:
            try:
                page = _get_key_code("pageup" if see_older else "pagedown")
            except Exception:
                page = "\x1b[5~" if see_older else "\x1b[6~"
        parts.append(page)
    term.send_string("".join(parts))
    return True


def _pin_terminal_viewport(view, term):
    """Snap viewport back after a trackpad pan (kill visible jiggle).

    TUI / mouse-tracking: rest at top of real content (below top pad) so
    both up and down pans still have headroom. Not y=0 — that blocked
    down-drag forever.
    """
    try:
        view.settings().set("scroll_past_end", True)
        if _tui_like(term):
            rest = _host_rest_y(view)
            _set_viewport(view, (0.0, rest), False)
            return
        le = view.layout_extent()
        ve = view.viewport_extent()
        lh = view.line_height() or 12.0
        # Near-fit including pads: still use rest position
        if le[1] - ve[1] <= lh * (2 * _HOST_SCROLL_PAD_LINES + 1):
            _set_viewport(view, (0.0, _host_rest_y(view)), False)
        elif term is not None and getattr(term, "_auto_follow", False):
            _scroll_to_bottom(view)
    except Exception:
        pass


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
            tracked = _mouse_handling_enabled(term) and bool(term.screen.mouse_tracking)
            # Bisection gate (ai_terminal.sublime-settings): off by default --
            # see settings comment.
            if (
                not modified
                and not multi
                and not tracked
                and _setting_bool("click_to_cursor_fallback_enabled", False, profile_name=_term_profile_name(term))
            ):
                _route_click_to_cursor_fallback(view, term, event)
            if not _mouse_handling_enabled(term):
                return None
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
                if term.screen.mouse_tracking:
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
                else:
                    _cancel_copyfirst_tap(term)
                    _arm_st_select_guard(term)
                return None
            # Mouse-first: Shift/Ctrl-drag select; plain drag → PTY when tracking.
            if modified:
                _mouse_force_release(term, view.id())
                _arm_st_select_guard(term)
                return None
            if _route_mouse_click(view, term, event, discrete_click=multi):
                return ("ai_terminal_noop", {})
            return None
        # Two-finger trackpad / mouse wheel: ALWAYS swallow for terminal views.
        # Returning None lets ST pan the view a few px; our clamp loop then
        # snaps it back — the "tab jumps then restores" glitch.
        # Always forward vertical scroll to the PTY (wheel if mouse tracking,
        # else PageUp/Down) and pin the viewport so the tab never visibly pans.
        if command_name in ("scroll_lines", "scroll_horizontally") and not _wheel_to_pty_enabled(term):
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
        if key != "ai_terminal_copy_mode":
            return None
        if not view.settings().get(_VIEW_SETTING):
            return None
        term = _Terminal.from_id(view.id())
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


def _profile_names(settings=None):
    profiles = _all_profiles(_settings_obj(settings))
    return list(profiles.keys()) if isinstance(profiles, dict) else []


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


# Sticky per-window cwd override, set explicitly via the sidebar's "Set Ai
# Terminal Working Directory" command (TermMate/GeminiCLI convention: pick
# once, reuse silently after that — never re-ask). In-memory only, keyed by
# window id; resets on restart rather than persisting like the old launch
# history did, since this is one explicit choice, not a passive log.
_working_dirs = {}


def _get_working_dir(window):
    path = _working_dirs.get(window.id()) if window else None
    return path if path and os.path.isdir(path) else None


def _set_working_dir(window, path):
    _working_dirs[window.id()] = path
    sublime.status_message("Ai terminal: working directory set to %s" % path)


def _clear_working_dir(window):
    if _working_dirs.pop(window.id(), None):
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
        on_path(sticky)
        return

    folders = list(window.folders() or []) if window else []
    sole = _sole_auto_cwd(folders)
    if sole:
        on_path(sole)
        return

    candidates = _cwd_candidates(window)
    if not candidates:
        # No sidebar projects: last resort is active-file / sole resolve.
        path = _resolve_here_path(window, [])
        if path:
            on_path(path)
            return
        sublime.status_message("Ai terminal: no folder resolved")
        return
    if len(candidates) == 1:
        on_path(candidates[0])
        return

    sublime.status_message(
        "Ai terminal: multiple project folders open — right-click one in the "
        "sidebar and choose 'Set Ai Terminal Working Directory'"
    )


class AiTerminalSetWorkingDirectoryCommand(sublime_plugin.WindowCommand):
    """Sidebar: pin one folder as this window's Ai Terminal cwd.

    Command palette / sidebar right-click: "Set Ai Terminal Working
    Directory". Every subsequent launch that would otherwise need to guess or
    ask uses this folder silently, until cleared or reset to a different one.
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


def _spawn(window, path, profile=None):
    if not _PTY_OK:
        sublime.error_message("ai_terminal: no PTY backend available (ConPTY ctypes binding failed).")
        return

    s = _settings_obj()
    profile_name = profile or s.get("default_profile")
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

    view = _terminal_view(window, name=tab_name)
    window.focus_view(view)
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
        parser = _make_parser(screen, _force_main_screen(profile_name))
    except Exception as e:
        print("[ai_terminal] VT engine init failed:\n%s" % traceback.format_exc())
        sublime.error_message(f"ai_terminal: failed to initialize the VT engine:\n{e}")
        view.close()
        return

    detachable = bool(profile_data.get("detachable")) if profile_data else False

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
        pty = _BrokerPty(pipe_name, argv, path, cols, rows, env)
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
        except Exception:
            pass
        sublime.error_message(f"ai_terminal: failed to start PTY:\n{e}")
        view.close()
        return
    if detachable and isinstance(pty, _BrokerPty):
        view.settings().set(_BROKER_PIPE_SETTING, pty.pipe_name)
        view.settings().set(_BROKER_PROFILE_SETTING, profile_name)
        view.settings().set(_BROKER_CWD_SETTING, path)
    with _term_lock():
        _term_registry()[view.id()] = term
    term.start_reader()


def _maybe_reattach_broker(view):
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
        _reattach_broker_view(view, pipe_name)
    except Exception:
        print("[ai_terminal] reattach check failed:\n%s" % traceback.format_exc())


def _reattach_broker_view(view, pipe_name):
    if not _PTY_OK or os.name != "nt":
        return
    profile_name = view.settings().get(_BROKER_PROFILE_SETTING)
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
        print(f"[ai_terminal] reattach: {e}")
        return

    cols, rows = _measure(view, profile_name=profile_name)
    try:
        screen = _Screen(cols, rows, history_cap=_scrollback_size(profile_name))
        parser = _make_parser(screen, _force_main_screen(profile_name))
    except Exception:
        print("[ai_terminal] reattach: VT engine init failed:\n%s" % traceback.format_exc())
        return

    pty = _BrokerPty(pipe_name, argv, path, cols, rows, env)
    term = _Terminal(view, pty, screen, parser, spawn_env=extra_env, profile_name=profile_name)
    term.prepare()
    try:
        pty.start()
    except Exception as e:
        print("[ai_terminal] reattach: broker connect failed:\n%s" % traceback.format_exc())
        sublime.status_message(f"Ai terminal: could not reattach ({e})")
        return
    with _term_lock():
        _term_registry()[view.id()] = term
    term.start_reader()
    print(f"[ai_terminal] reattached view {view.id()} to pipe {pipe_name!r}")


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

    def is_visible(self, paths=None):
        return True

    def is_enabled(self, paths=None, profile=None):
        return _profile_is_available(profile)

    def description(self, paths=None, profile=None):
        # Menu entries without an explicit "caption" render this live label,
        # e.g. "Claude — 64% left, resets 3h 42m" after real output was seen.
        return _profile_menu_caption(profile)


class AiTerminalOpenInEditorCommand(sublime_plugin.TextCommand):
    """Open a Claude TUI terminal in the active file's project root.

    Uses the nearest project root containing the file (not the bare
    file directory, and not the umbrella window folder).

    Menu: Context.sublime-menu / Tab Context.sublime-menu — "Open Ai Terminal here..."
    Command palette: "Ai: Open Terminal in Editor"
    """

    def run(self, edit, profile=None):
        window = self.view.window()
        path = _resolve_editor_path(self.view)
        if path and window:
            _spawn(window, path, profile=profile)
            return
        if not window:
            sublime.status_message("Ai terminal: no folder resolved")
            return

        def on_path(p):
            _spawn(window, p, profile=profile)

        _pick_cwd_then(window, on_path)

    def is_enabled(self, profile=None):
        return _profile_is_available(profile)

    def description(self, profile=None):
        return _profile_menu_caption(profile)


def _usage_annotation(name, s):
    """Short right-aligned availability text, e.g. '82% remaining · 3m ago'.

    The age matters as much as the number: a quota figure from an hour ago is
    worth acting on, one from last session is not, and silently showing a stale
    percentage as if it were live is exactly the failure mode to avoid. A sweep
    still in flight says so rather than showing nothing.
    """
    try:
        label = (_profile_availability_label(name, s) or "").strip()
    except Exception:
        label = ""
    at = getattr(sys, "_stext_ai_profile_scan_at", None)
    if at:
        return "%s · %s" % (label, _launcher.relative_age(at)) if label else \
            _launcher.relative_age(at)
    thread = getattr(sys, "_stext_ai_usage_scan_thread", None)
    if thread is not None and thread.is_alive():
        return "%s · checking…" % label if label else "checking…"
    return label


class AiTerminalRefreshUsageCommand(sublime_plugin.WindowCommand):
    """Re-run the provider usage sweep now.

    Command palette: "Ai: Refresh Usage & Quota". The sweep is otherwise
    once-per-load, so this is the way to get fresh numbers after burning
    through quota without restarting Sublime.
    """

    def run(self):
        _ensure_usage_scanner(force=True)
        sublime.status_message("Ai terminal: refreshing usage…")


class AiTerminalSyncAgentProfilesCommand(sublime_plugin.ApplicationCommand):
    """Clear and rebuild the auto-detected agent profiles from scratch.

    Command palette: "Ai: Sync Detected Agent Profiles". Re-runs local PATH
    detection against agent_catalog.CATALOG and overwrites
    ai_terminal_agents.sublime-settings wholesale with what it finds --
    nothing else is touched. A profile in ai_terminal.sublime-settings with
    the same name always overrides its generated counterpart (see
    _all_profiles), so hand customization (a full shim path, extra
    spawn_env, mouse_handling) survives a re-sync even if the bare command
    momentarily fails detection (e.g. a shim not yet on PATH).
    """

    def run(self):
        # Same reasoning as _spawn: a long-lived ST process inherited PATH at
        # launch, so a CLI installed since then (setx / installer PATH edit)
        # is invisible to os.environ until refreshed from the registry --
        # otherwise a sync run right after installing an agent still misses it.
        refreshed = _refresh_path_env(dict(os.environ))
        path = refreshed.get("Path") or refreshed.get("PATH")
        detected = {}
        for entry in _AGENT_CATALOG.values():
            if not _command_exists(entry["launch_command"], path=path):
                continue
            detected[entry["display_name"]] = _agent_profile_from_entry(entry)

        gs = _generated_settings or sublime.load_settings(_GENERATED_SETTINGS_NAME)
        gs.set("profiles", detected)
        sublime.save_settings(_GENERATED_SETTINGS_NAME)
        sublime.status_message(
            "Ai terminal: synced %d detected agent profile(s)" % len(detected)
        )


def _profile_items(names, s, context_dir=None):
    """Quick-panel rows for profiles, alphabetical. Unavailable profiles are
    not hidden (hiding breaks muscle memory and hides the reason) but are
    marked via their kind glyph.
    """
    ordered = sorted(names, key=lambda n: n.lower())
    rows = [
        _quick_panel_item(
            name,
            "",
            _usage_annotation(name, s),
            _launcher.profile_kind(
                name,
                available=_profile_is_available(name, s),
                exhausted=_profile_is_exhausted(name),
            ),
        )
        for name in ordered
    ]
    return ordered, rows


def _dir_items(window, context_profile=None):
    """Quick-panel rows for directories: open sidebar folders, then a Browse…
    escape hatch so the picker is never a dead end.
    """
    folders = list(window.folders() or []) if window else []
    rows = [
        _quick_panel_item(
            os.path.basename(path.rstrip("\\/")) or path,
            _launcher.shorten_path(path),
            "",
            _launcher.dir_kind(is_git=os.path.isdir(os.path.join(path, ".git"))),
        )
        for path in folders
    ]
    rows.append(_quick_panel_item("Browse…", "Pick any folder", "", _launcher.BROWSE_KIND))
    return folders, rows


class AiTerminalLauncherCommand(sublime_plugin.WindowCommand):
    """Two-step launcher: pick an agent, then pick where to run it.

    Command palette: "Ai: Launch Agent…". Both steps are frecency-ranked and
    cross-conditioned (the directory list is re-ranked for the agent you just
    chose), so the pair you use most is Enter-Enter. Going back from step two
    reopens step one rather than dropping the whole flow.
    """

    def run(self, paths=None, profile=None):
        s = sublime.load_settings(_SETTINGS_NAME)
        names = _profile_names(s)
        if not names:
            self.window.run_command("ai_terminal_open_here", {"paths": paths})
            return

        # Sidebar right-click already answers "where"; skip straight to launch.
        preset_dir = None
        if paths:
            preset_dir = _resolve_here_path(self.window, paths)

        context_dir = preset_dir or _resolve_here_path(self.window, [])
        if profile:
            self._pick_dir(s, profile, preset_dir)
            return

        ordered, rows = _profile_items(names, s, context_dir=context_dir)

        def on_profile(idx):
            if idx < 0:
                return
            self._pick_dir(s, ordered[idx], preset_dir)

        self.window.show_quick_panel(
            rows, on_profile, placeholder="Which agent?", selected_index=0
        )

    def _pick_dir(self, s, profile, preset_dir):
        if preset_dir:
            self._launch(profile, preset_dir)
            return

        ranked, rows = _dir_items(self.window, context_profile=profile)

        def on_dir(idx):
            if idx < 0:
                # Re-open the agent step so an accidental Esc is one key, not a
                # restart of the whole flow.
                sublime.set_timeout(
                    lambda: self.window.run_command("ai_terminal_launcher"), 10
                )
                return
            if idx >= len(ranked):
                self._browse(profile)
                return
            self._launch(profile, ranked[idx])

        self.window.show_quick_panel(
            rows,
            on_dir,
            placeholder="Run %s where?" % profile,
            selected_index=0,
        )

    def _browse(self, profile):
        """Free-text path entry; ST has no native folder dialog for plugins."""
        initial = os.path.expanduser("~")

        def on_done(text):
            path = os.path.expanduser((text or "").strip().strip('"'))
            if not os.path.isdir(path):
                sublime.error_message("ai_terminal: not a directory:\n%s" % path)
                return
            self._launch(profile, path)

        self.window.show_input_panel(
            "Folder for %s:" % profile, initial, on_done, None, None
        )

    def _launch(self, profile, path):
        self.window.run_command(
            "ai_terminal_open_here", {"profile": profile, "paths": [path]}
        )


def _ollama_chat_transcript(db_path, chat_id):
    """Best-effort plain-text dump of one Ollama chat's messages, newest last."""
    import sqlite3

    conn = sqlite3.connect(_history_scan.read_only_uri(db_path), uri=True)
    try:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY created_at",
            (chat_id,),
        ).fetchall()
    finally:
        conn.close()
    lines = []
    for role, content in rows:
        if content:
            lines.append("--- %s ---\n%s\n" % (role or "?", content))
    return "\n".join(lines) or "(no message content stored for this chat)"


def _t3_thread_transcript(db_path, thread_id):
    """Best-effort plain-text dump of one T3 Code thread's messages."""
    import sqlite3

    conn = sqlite3.connect(_history_scan.read_only_uri(db_path), uri=True)
    try:
        rows = conn.execute(
            "SELECT role, text FROM projection_thread_messages "
            "WHERE thread_id = ? ORDER BY created_at",
            (thread_id,),
        ).fetchall()
    finally:
        conn.close()
    lines = []
    for role, text in rows:
        if text:
            lines.append("--- %s ---\n%s\n" % (role or "?", text))
    return "\n".join(lines) or "(no message content stored for this thread)"


# Agent-prefix -> transcript reader, for sqlite sources that ai_terminal knows
# how to read. Anything not listed here (an unrecognized sqlite db, e.g. a
# Gemini conversation) falls back to a generic "here's the file" message.
_TRANSCRIPT_READERS = {
    "Ollama": _ollama_chat_transcript,
    "T3": _t3_thread_transcript,
}


class AiTerminalHistoryCommand(sublime_plugin.WindowCommand):
    """Sweep every local AI agent's history and let you open one.

    Command palette: "Ai Terminal: All Agent History…". This reads live off
    disk on every invoke (Claude Code's ~/.claude/projects/*.jsonl, Codex's
    ~/.codex/sessions/**/*.jsonl, Gemini/Antigravity's conversation dbs,
    Ollama's local chat db, T3 Code's state.sqlite) — nothing is cached or
    written back, so there is no persisted record for this command itself
    to leak.
    """

    def run(self):
        sessions = _history_scan.scan_all()
        if not sessions:
            sublime.status_message("Ai terminal: no agent history found")
            return

        rows = [
            _quick_panel_item(
                "%s — %s" % (sess["agent"], sess["title"]),
                sess.get("detail", ""),
                _launcher.relative_age(sess.get("mtime")),
                (_launcher.KIND_ID_NAVIGATION, "H", sess["agent"]),
            )
            for sess in sessions
        ]

        def on_done(idx):
            if idx < 0:
                return
            self._open(sessions[idx])

        self.window.show_quick_panel(
            rows, on_done, placeholder="Agent history (all agents)", selected_index=0
        )

    def _open(self, sess):
        if sess["kind"] == "text":
            self.window.open_file(sess["path"])
            return
        reader = next(
            (fn for prefix, fn in _TRANSCRIPT_READERS.items()
             if sess["agent"].startswith(prefix)),
            None,
        )
        if reader is not None:
            try:
                text = reader(sess["path"], sess["detail"])
            except Exception as e:
                sublime.error_message(
                    "ai_terminal: could not read %s history:\n%s" % (sess["agent"], e)
                )
                return
        else:
            text = (
                "%s\n\n%s is a SQLite database; ai_terminal does not know its "
                "schema, so this just points at the file on disk.\n\nPath: %s"
                % (sess["title"], sess["agent"], sess["path"])
            )
        view = self.window.new_file()
        view.set_scratch(True)
        view.set_name("%s — %s" % (sess["agent"], sess["title"]))
        view.run_command("append", {"characters": text})
        view.set_read_only(True)


class AiTerminalSelectProfileCommand(sublime_plugin.WindowCommand):
    """Pick a profile and open it in the resolved cwd.

    Command palette: "Ai: Open Terminal Profile...". Kept as the one-step form
    (cwd resolved from context) for users who never want the directory step;
    the two-step flow is ai_terminal_launcher.
    """

    def run(self, paths=None):
        s = sublime.load_settings(_SETTINGS_NAME)
        profile_names = _profile_names(s)

        if not profile_names:
            # Fall back to launching default terminal
            self.window.run_command("ai_terminal_open_here", {"paths": paths})
            return

        context_dir = _resolve_here_path(self.window, paths or [])
        ordered, items = _profile_items(profile_names, s, context_dir=context_dir)

        def on_done(idx):
            if idx == -1:
                return
            name = ordered[idx]
            if not _profile_is_available(name, s):
                sublime.status_message(
                    "Ai terminal: "
                    + name
                    + " is unavailable ("
                    + _profile_availability_label(name, s).lower()
                    + ")"
                )
                return
            self.window.run_command(
                "ai_terminal_open_here", {"profile": name, "paths": paths}
            )

        self.window.show_quick_panel(
            items, on_done, placeholder="Open terminal profile", selected_index=0
        )


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


def _pin_viewport_rest(view, rest=None, term=None):
    """Pin to host rest_y only if the viewport drifted (avoids per-frame thrash)."""
    if rest is None:
        rest = _host_rest_y(view)
    try:
        cur = view.viewport_position()[1]
        if abs(cur - rest) > 1.0:
            _set_viewport(view, (0.0, rest), False)
        if term is not None:
            term._last_vp_y = rest
    except Exception:
        pass


def _real_content_height(view):
    """layout height of real terminal content (excludes both host pads)."""
    le = view.layout_extent()
    lh = view.line_height() or 12.0
    return max(0.0, float(le[1]) - 2 * _HOST_SCROLL_PAD_LINES * lh)


def _follow_ignore_trailing_lines(term):
    """How many trailing rows snap-to-bottom should skip. Default 0."""
    if term is None:
        return 0
    n = _setting_number(
        "follow_ignore_trailing_lines",
        0,
        profile_name=_term_profile_name(term),
    )
    try:
        return max(0, int(n or 0))
    except (TypeError, ValueError):
        return 0


def _follow_content_height(view, ignore_trailing=0):
    """Content height snap-to-bottom should chase.

    Subtracts follow_ignore_trailing_lines (a profile setting, default 0)
    so a TUI whose last N rows wobble does not move the follow target.
    """
    lh = view.line_height() or 12.0
    drop = max(0, int(ignore_trailing or 0))
    return max(0.0, _real_content_height(view) - drop * lh)


def _compensate_trim_scroll(view, term, vp):
    """Undo the visual shift caused by the history deque evicting old lines.

    Not a follow/snap heuristic like the machinery gated behind
    _SCROLL_MANIPULATION_ENABLED above -- that decides where the viewport
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
    lh = view.line_height() or 20
    new_y = max(0.0, vp[1] - evicted * lh)
    if new_y != vp[1]:
        view.set_viewport_position((vp[0], new_y), False)
        vp = (vp[0], new_y)
        # Without this, the render loop's own "vp[1] < term._last_vp_y -
        # lh*1.5" user-scroll detector (a few lines below this call site)
        # reads this write as the user having scrolled up, disengages
        # _auto_follow, which now also latches screen.trim_paused True --
        # and with _SCROLL_MANIPULATION_ENABLED off, nothing ever moves the
        # viewport back to "near bottom" to re-engage it, so one real
        # eviction permanently stops all future trimming (confirmed live
        # 2026-08-18: unbounded growth, 300 -> 1000+ lines in seconds).
        if term is not None:
            term._last_vp_y = new_y
    return vp


def _scroll_to_bottom(view):
    """Jump the viewport to the bottom of real terminal content (not the pad).

    Called on user input so typing brings the user back to the prompt after
    scrolling up to read scrollback. No-op when real content fits the viewport.
    A profile may set follow_ignore_trailing_lines to leave the last N
    rows out of the snap target.
    """
    ve = view.viewport_extent()
    lh = view.line_height() or 12.0
    top = _host_rest_y(view)
    term = _Terminal.from_id(view.id()) if view is not None else None
    real_h = _follow_content_height(view, _follow_ignore_trailing_lines(term))
    if real_h > ve[1]:
        # Bottom of real content = top pad + real_h
        _set_viewport(view, (0.0, top + real_h - ve[1]), False)
    else:
        _set_viewport(view, (0.0, top), False)


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
    """Where the viewport lands after a frame: pinned to rest for an app-owned
    TUI, else following the tail while the user hasn't scrolled away and there
    is real content below the fold."""
    if tui_owns_scroll:
        _pin_viewport_rest(view, rest, term)
    elif do_follow and not content_fits:
        _scroll_to_bottom(view)
        if term is not None:
            term._last_vp_y = view.viewport_position()[1]


class AiTerminalToggleCopyModeCommand(sublime_plugin.TextCommand):
    """Toggle copy mode (see AiTerminalKeypressCommand.run).

    While on, plain arrow/page/home/end keys move or extend the ST caret
    instead of being forwarded to the PTY -- lets scrollback/response text be
    navigated and selected with the keyboard without triggering shell
    history recall or fighting a TUI's own cursor. Escape or toggling again
    exits copy mode and re-pins the viewport to the live prompt.

    Bound to ctrl+alt+c inside an Ai terminal view. No menu/palette entry.
    """

    def run(self, edit):
        term = _Terminal.from_id(self.view.id())
        if term is None:
            return
        term.copy_mode = not term.copy_mode
        if term.copy_mode:
            # TEMP DEBUG: this command is only reachable via the ctrl+alt+c
            # keybinding (no menu/palette entry, no other run_command call
            # anywhere in the codebase) -- yet it's been reported engaging
            # without a deliberate ctrl+alt+c press (Kiro profile, plain
            # ASCII typing, non-US layout ruled out). Logging the call stack
            # to nail down the real trigger next time it reproduces; remove
            # once root-caused (see ai/TODO.md).
            print(
                "[ai_terminal] copy_mode ON via toggle command — "
                f"view={self.view.id()} name={self.view.name()!r}\n"
                + "".join(traceback.format_stack()[-6:])
            )
            sublime.status_message("Ai terminal: copy mode ON (Esc to exit)")
        else:
            # Hand caret control back to the PTY cursor -- otherwise the
            # caret stays wherever copy-mode nav left it (outside the box)
            # and the very next keypress's render sees term._user_owns_caret
            # still True, freezing the caret and looking like copy mode
            # never really turned off.
            term._user_owns_caret = False
            _scroll_to_bottom(self.view)
            _set_auto_follow(term, True)
            sublime.status_message("Ai terminal: copy mode OFF")


class AiTerminalTogglePanelCommand(sublime_plugin.TextCommand):
    """Move the live terminal between a normal tab and the bottom output
    panel (like Sublime's own Find/Console), keeping the same PTY running.

    Bound to ctrl+alt+p inside an Ai terminal view. No menu/palette entry --
    the command only makes sense from inside the terminal it targets.
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
        new_view = _terminal_panel_view(window, panel_name)
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
        new_view = _terminal_view(window, name=panel_name)
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
                sublime.status_message("Ai terminal: copy mode OFF")
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
        # PageUp/PageDown: scroll ST's real scrollback like an ordinary
        # terminal emulator (same motion as dragging the minimap) -- unlike
        # Home/End, no primary-screen readline-style CLI has a legitimate use
        # for PageUp/PageDown reaching its own input line, so this is correct
        # default behavior, not an opt-in. Exceptions: a real alt-screen app
        # (vim, less, htop) via `_tui_like`, or a profile that paints in
        # place with no ST history (Grok) via `page_keys_to_pty`.
        if not alt and key in ("pageup", "pagedown") and not _page_keys_to_pty(term):
            self.view.run_command("move", {"by": "pages", "forward": key == "pagedown", "extend": shift})
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
                    except Exception:
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
            # Fullscreen / mouse-tracking TUIs (Junie, Grok): never yank the
            # viewport on every printable — that fought mid-line caret and
            # made the next char land at EOL. Pin to rest instead.
            tui = _tui_like(term)
            if kl in ("pageup", "pagedown"):
                # Explicit scrollback navigation: same intent as a mouse
                # wheel/click, which already disengage follow (see
                # _auto_follow=False at the mouse handlers above). Without
                # this, PageDown itself is a no-scroll key (correctly, per
                # the comment below) but a stale True from prior typing
                # survives it, so the very next streaming render snaps the
                # viewport right back to the bottom -- PageDown "does
                # nothing" from the user's perspective.
                _set_auto_follow(term, False)
            elif kl not in _NO_SCROLL_KEYS:
                _set_auto_follow(term, True)
                if tui:
                    # Only re-pin when drifted; set_viewport every key on Windows
                    # forces layout work and feels like lag/jumps on Grok.
                    try:
                        rest = _host_rest_y(self.view)
                        cur = self.view.viewport_position()[1]
                        if abs(cur - rest) > 1.0:
                            _set_viewport(self.view, (0.0, rest), False)
                        term._last_vp_y = rest
                    except Exception:
                        pass
                else:
                    _scroll_to_bottom(self.view)
                    term._last_vp_y = self.view.viewport_position()[1]
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

    def _run(self, view, edit, term, vp, ve, lh, text, cursor, cursor_offset, regions, fast_caret):
        # Abort buffer mutation while the user is selecting text. Even a
        # single-char patch shifts offsets and kills a drag mid-response.
        # Critical: _do_render already cleared screen.dirty before invoking us.
        # If we return without painting, re-dirty + re-arm or the view freezes
        # on the empty pad frame until the next PTY byte (Grok often silent).
        if term is not None and _selection_paint_blocked(view, term):
            try:
                term.screen.dirty = True
            except Exception:
                pass
            try:
                term._render_pending = False
                _schedule_render(term)
            except Exception:
                pass
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

        if not patched:
            view.replace(edit, sublime.Region(0, view.size()), text)
            # Re-apply colour regions every frame: view.replace invalidates the old
            # regions, and add_regions with the same key replaces what was there.
            _apply_color_regions(view, regions or [])

        try:
            _record_render_history(view.id(), patched, diffs if patched else [], cur, text)
        except Exception:
            pass

        vp = _compensate_trim_scroll(view, term, vp)

        rest = _host_rest_y(view)
        real_h = _real_content_height(view)
        near_bottom = (vp[1] + ve[1]) >= (rest + real_h - lh * 2)
        content_fits = real_h <= ve[1] + 0.5
        tui_owns_scroll = _tui_like(term)
        if term is not None and not tui_owns_scroll:
            if vp[1] < term._last_vp_y - lh * 1.5:
                _set_auto_follow(term, False)
            if near_bottom:
                _set_auto_follow(term, True)
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
        keep_selection = bool(
            term is not None
            and getattr(term, "_user_owns_caret", False)
            and _setting_bool("user_owns_caret_enabled", False, profile_name=_term_profile_name(term))
        )
        if keep_selection and term is not None and term._auto_follow:
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
        if content_fits or tui_owns_scroll:
            _pin_viewport_rest(view, rest, term)


class AiTerminalEndSessionCommand(sublime_plugin.TextCommand):
    """End a detachable session's underlying agent/shell for real, then close
    the tab. Plain tab-close only detaches (see _BrokerPty.kill) -- this is
    the explicit "actually stop it" action for a detachable profile. No
    command palette / menu entry yet; run via View > Show Console:
        view.run_command("ai_terminal_end_session")
    Hidden/no-op on non-detachable tabs.
    """

    def run(self, edit):
        term = _Terminal.from_id(self.view.id())
        if term is None or not isinstance(term.pty, _BrokerPty):
            return
        try:
            term.pty.explicit_kill()
        except Exception as e:
            print(f"[ai_terminal] explicit_kill failed: {e}")
        self.view.close()

    def is_visible(self):
        term = _Terminal.from_id(self.view.id())
        return term is not None and isinstance(term.pty, _BrokerPty)


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
        view.set_read_only(False)
        view.replace(edit, sublime.Region(0, view.size()), "")
        term = _Terminal.from_id(view.id())
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
        if term is None or not _wheel_to_pty_enabled(term):
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

_HOVER_POLL_MS = 33  # ~30Hz -- reads as continuous; early-exits keep it cheap when idle
_hover_poll_token = None
_hover_last_cell = {}  # view_id -> (col, row) last cell a motion report was sent for


class _POINT(ctypes.Structure):
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
    except Exception:
        return None


def _hover_poll_tick():
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
    except Exception:
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
        sgr = term.screen.mouse_sgr
        seq = _encode_mouse(_BTN_RELEASE_X10, col, row, press=True, motion=True, sgr=sgr)
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
# view bbox triggers it BETWEEN renders. During generation a render clamps it
# within ~110ms, but when Claude is idle there's no TUI output -> no render ->
# the -20 persists until the next TUI frame (cursor blink ~500ms), so the user
# sees the text dip one line for ~500ms then snap back. This loop clamps vp to
# (0,0) whenever content fits, independent of the render clock, killing the dip
# within 16ms. It only fires when content fits (le <= ve), so it never fights
# the user scrolling up to read scrollback when content exceeds the viewport.

_clamp_token = None


def _vp_pan_to_tui_scroll(view, term, dy_from_rest):
    """Turn ST viewport drift from rest into PTY scroll — content-grab model.

    dy_from_rest = viewport_y - rest_y (rest = top of real TUI, below top pad).

      dy > 0  text slides UP   → reveal newer below → amount < 0
      dy < 0  text slides DOWN → reveal older above → amount > 0

    Opposite of the TUI scroll-button (view move). Both signs must work:
    rest is not y=0 so finger-down can produce dy < 0.
    """
    lh = view.line_height() or 12.0
    dy = float(dy_from_rest)
    if abs(dy) < 1.5:
        return False
    now = time.time()
    last = float(getattr(term, "_last_scroll_send_t", 0.0) or 0.0)
    if (now - last) < 0.08:
        return False
    ticks = 1 if abs(dy) < lh * 0.75 else 2
    # Content-grab: text moves with fingers.
    amount = -float(ticks) if dy > 0 else float(ticks)
    term._last_user_pan_t = now
    try:
        n = int(getattr(term, "_vp_pan_log_n", 0) or 0)
        if n < 8:
            print(
                f"[ai_terminal] content-grab pan→TUI "
                f"dy_rest={dy:.1f}px steps={ticks} "
                f"({'older' if amount > 0 else 'newer'})"
            )
            term._vp_pan_log_n = n + 1
        _route_mouse_wheel(view, term, amount)
        return True
    except Exception as e:
        print(f"[ai_terminal] vp-pan scroll failed: {e}")
        return False


def _clamp_vp_loop():
    global _clamp_token
    try:
        for _vid, term in list(_term_registry().items()):
            v = term.view
            if not v or not v.is_valid():
                continue
            # Every ai_terminal view: trackpad = core pan. Convert + pin.
            # (Previously gated on alt/mouse only; force_main_screen makes
            # alt_screen False even under Grok, and a missed mouse mode left
            # pure pan with no PTY traffic — matches empty wheel casts.)
            try:
                v.settings().set("scroll_past_end", True)
            except Exception:
                pass
            try:
                vp = v.viewport_position()
                rest = _host_rest_y(v)
                dy_rest = float(vp[1]) - rest
                dx = float(vp[0])
            except Exception:
                continue

            tui_like = _tui_like(term)
            try:
                le = v.layout_extent()
                ve = v.viewport_extent()
                lh = v.line_height() or 12.0
                # Near-fit relative to real content (pads always add height).
                near_fit = _real_content_height(v) <= ve[1] + lh * 2
            except Exception:
                near_fit = False
                lh = 12.0

            if tui_like:
                if near_fit:
                    # Short TUI pickers (for example Codex /hooks) are keyed
                    # with physical arrows. Do not turn Sublime's residual
                    # viewport drift into extra PTY arrows that pin selection.
                    if abs(dy_rest) >= 0.5 or abs(dx) >= 0.5:
                        _set_viewport(v, (0.0, rest), False)
                    continue
                # Spawn settle: pin only until viewport sits at rest once
                # after a short grace. Sending pan→TUI keys on the first
                # dy_rest=-pad_height injects Up arrows into Grok at t=0.
                armed = bool(getattr(term, "_vp_pan_armed", False))
                if not armed:
                    if abs(dy_rest) < 1.5 and abs(dx) < 0.5:
                        age = time.monotonic() - float(
                            getattr(term, "_spawn_mono", 0.0) or 0.0
                        )
                        if age >= 0.4:
                            term._vp_pan_armed = True
                    elif abs(dy_rest) >= 0.5 or abs(dx) >= 0.5:
                        _set_viewport(v, (0.0, rest), False)
                    continue
                # Treat viewport displacement as an edge, not a level. ST can
                # retain a small fractional offset (observed: 4 px) even after
                # set_viewport_position pins the view. Re-routing that stable
                # offset every cooldown interval floods the TUI with synthetic
                # arrows and pins pickers such as Codex /hooks to one item.
                # Re-arm only after the viewport actually returns to rest.
                pan_excursion = abs(dy_rest) >= 1.5
                pan_latched = bool(getattr(term, "_vp_pan_latched", False))
                if pan_excursion:
                    term._vp_pan_rest_frames = 0
                    if not pan_latched:
                        term._vp_pan_latched = True
                        _vp_pan_to_tui_scroll(v, term, dy_rest)
                elif pan_latched:
                    # set_viewport_position can produce one rest frame before
                    # ST rebounds to the same fractional/pixel displacement.
                    # Require sustained rest before accepting another gesture.
                    rest_frames = int(
                        getattr(term, "_vp_pan_rest_frames", 0) or 0
                    ) + 1
                    term._vp_pan_rest_frames = rest_frames
                    if rest_frames >= 4:
                        term._vp_pan_latched = False
                        term._vp_pan_rest_frames = 0
                if abs(dy_rest) >= 0.5 or abs(dx) >= 0.5:
                    _set_viewport(v, (0.0, rest), False)
                continue

            if near_fit:
                # Short picker/command menus are usually keyboard-driven. Keep
                # them pinned, but do not reinterpret their tiny viewport drift
                # as PTY arrow input.
                if abs(dy_rest) >= 0.5 or abs(dx) >= 0.5:
                    _set_viewport(v, (0.0, rest), False)
                continue

            # Tall scrollback shell: only kill tiny overflow dips, don't steal
            # real user scrollback browsing.
            if le[1] - ve[1] <= lh and (dx != 0.0 or abs(dy_rest) >= 0.5):
                _set_viewport(v, (0.0, rest), False)
    except Exception as e:
        print(f"[ai_terminal] clamp loop error: {e}")
    # 8ms: catch the brief pan before the next paint eats it
    _clamp_token = sublime.set_timeout(_clamp_vp_loop, 8)


def plugin_loaded():
    if not _PTY_OK:
        print("[ai_terminal] no PTY backend available; commands will report the error.")
    global _clamp_token, _settings, _generated_settings
    _init_dynamic_color_scheme()
    # Bind the settings object and live-apply edits (the callback fires on the
    # main thread right after a settings file write).
    _settings = sublime.load_settings(_SETTINGS_NAME)
    _settings.add_on_change("ai_terminal", _on_settings_change)
    # Same caching as _settings above -- _all_profiles() is on hot paths
    # (every keypress/mouse-event via _mouse_handling_enabled), so this must
    # not call sublime.load_settings() itself. AiTerminalSyncAgentProfilesCommand
    # writes through this same cached object (Settings objects are singletons
    # per base name), so a re-sync is visible immediately without a reload.
    _generated_settings = sublime.load_settings(_GENERATED_SETTINGS_NAME)
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
        except Exception:
            pass
    _clamp_token = sublime.set_timeout(_clamp_vp_loop, 8)
    global _hover_poll_token
    if _hover_poll_token:
        try:
            sublime.cancel_timeout(_hover_poll_token)
        except Exception:
            pass
    _hover_poll_token = sublime.set_timeout(_hover_poll_loop, _HOVER_POLL_MS)
    _start_layout_watcher()
    _ensure_usage_scanner()
    _start_usage_refresh()
    # Reconnect any detachable-profile tabs Sublime just restored from its
    # workspace session -- their agent_broker.py session may have survived
    # the restart even though this plugin instance is brand new.
    for window in sublime.windows():
        for view in window.views():
            _maybe_reattach_broker(view)
    print("[ai_terminal] loaded (trackpad pan→TUI scroll armed)")


def plugin_unloaded():
    global _clamp_token, _settings, _hover_poll_token
    if _settings is not None:
        try:
            _settings.clear_on_change("ai_terminal")
        except Exception:
            pass
    if _clamp_token:
        try:
            sublime.cancel_timeout(_clamp_token)
        except Exception:
            pass
        _clamp_token = None
    if _hover_poll_token:
        try:
            sublime.cancel_timeout(_hover_poll_token)
        except Exception:
            pass
        _hover_poll_token = None
    _stop_layout_watcher()
    _stop_usage_refresh()
    # Deliberately do NOT kill ConPTY children on unload.  The terminal
    # process may be opencode itself (or another long-running CLI agent);
    # killing it here means a plugin reload triggered by the agent's own
    # file deployment will murder the agent mid-session — an unrecoverable
    # crash with no error log.  The children are owned by this ST instance
    # and will be cleaned up when ST itself exits.
