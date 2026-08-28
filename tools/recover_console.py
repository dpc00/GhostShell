"""recover_console.py -- attach a plain DOS/PowerShell console to a running
agent_broker.py session, so a Claude session stays reachable even if Sublime
Text itself crashes or becomes unresponsive ("dysfunctionalized"). Same
reattach mechanism as GhostShell's own ai_terminal.py uses to reattach a
Sublime view after an ST restart, but entirely outside Sublime -- so it
works even when ST itself is the thing that's broken.

Auto-detects the pipe name by scanning running processes for
`agent_broker.py --pipe-name <name> ... -- <child>`, so you don't have to
copy it out of Task Manager by hand. Disconnecting (Ctrl+C, or closing this
console) does NOT kill the broker or its child -- run this again with the
same pipe (or re-run with no args to re-detect) to reattach.

Usage:
    python recover_console.py                  # auto-detect, prompt if >1 match
    python recover_console.py --pipe-name NAME # attach to a known pipe directly
    python recover_console.py --list           # just list candidate sessions
"""
import argparse
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import byref, c_char, c_void_p
from ctypes.wintypes import HANDLE, DWORD, LPCWSTR

if sys.platform != "win32":
    sys.exit("attach_console.py is Windows-only (named pipes).")

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = HANDLE(-1).value
_ERROR_PIPE_BUSY = 231
_ERROR_BROKEN_PIPE = 109
_ERROR_HANDLE_EOF = 38
_ERROR_NO_DATA = 232
_ERROR_FILE_NOT_FOUND = 2

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [LPCWSTR, DWORD, DWORD, c_void_p, DWORD, DWORD, HANDLE]
_k32.CreateFileW.restype = HANDLE
_k32.ReadFile.argtypes = [HANDLE, ctypes.POINTER(c_char), DWORD, ctypes.POINTER(DWORD), c_void_p]
_k32.ReadFile.restype = ctypes.c_int
_k32.WriteFile.argtypes = [HANDLE, ctypes.c_char_p, DWORD, ctypes.POINTER(DWORD), c_void_p]
_k32.WriteFile.restype = ctypes.c_int
_k32.CloseHandle.argtypes = [HANDLE]
_k32.CloseHandle.restype = ctypes.c_int
_k32.WaitNamedPipeW.argtypes = [LPCWSTR, DWORD]
_k32.WaitNamedPipeW.restype = ctypes.c_int

_PIPE_NAME_RE = re.compile(r"agent_broker\.py.*--pipe-name\s+(\S+)", re.IGNORECASE)
_CWD_RE = re.compile(r"--cwd\s+(\S+)", re.IGNORECASE)
_CHILD_RE = re.compile(r"--\s+(.+)$")


def find_sessions():
    """Every running agent_broker.py, as (pipe_name, cwd, child_cmd)."""
    out = subprocess.check_output(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" "
            "| Select-Object -ExpandProperty CommandLine",
        ],
        text=True, stderr=subprocess.DEVNULL,
    )
    sessions = []
    for line in out.splitlines():
        m = _PIPE_NAME_RE.search(line)
        if not m:
            continue
        pipe_name = m.group(1)
        cwd_m = _CWD_RE.search(line)
        child_m = _CHILD_RE.search(line)
        sessions.append((
            pipe_name,
            cwd_m.group(1) if cwd_m else "?",
            child_m.group(1).strip() if child_m else "?",
        ))
    return sessions


def connect(path, access, timeout_s=10.0):
    deadline = time.time() + timeout_s
    while True:
        h = _k32.CreateFileW(path, access, 0, None, _OPEN_EXISTING, 0, None)
        if h != _INVALID_HANDLE_VALUE:
            return h
        err = ctypes.get_last_error()
        if time.time() > deadline:
            raise ctypes.WinError(err)
        if err == _ERROR_PIPE_BUSY:
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
        written = DWORD(0)
        redraw = b"\x0c"
        if not _k32.WriteFile(h_in, redraw, len(redraw), byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _k32.CloseHandle(h_in)

    print("[attach] released a stale quiet output attachment; retrying", file=sys.stderr)
    return connect(out_path, _GENERIC_READ, timeout_s=5.0)


def _pump_output(handle, stop_evt):
    buf = (c_char * 4096)()
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
        sys.stdout.buffer.write(bytes(buf[: n.value]))
        sys.stdout.buffer.flush()
    stop_evt.set()


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
    args = p.parse_args()

    if args.list:
        for pipe_name, cwd, child in find_sessions():
            print(f"pipe={pipe_name!r} cwd={cwd} child={child}")
        return

    pipe_name = pick_pipe_name(args.pipe_name)

    h_out = connect_output(pipe_name)
    h_in = connect("\\\\.\\pipe\\" + pipe_name + "-in", _GENERIC_WRITE)
    print("[attach] connected to \\\\.\\pipe\\%s (+ -in) -- Ctrl+C to detach "
          "(session keeps running)" % pipe_name, file=sys.stderr)

    stop_evt = threading.Event()
    reader = threading.Thread(target=_pump_output, args=(h_out, stop_evt), daemon=True)
    reader.start()

    try:
        while not stop_evt.is_set():
            line = sys.stdin.readline()
            if not line:
                break
            written = DWORD(0)
            data = line.encode("utf-8")
            if not _k32.WriteFile(h_in, data, len(data), byref(written), None):
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        print("\n[attach] detached (session keeps running)", file=sys.stderr)
        os._exit(0)


if __name__ == "__main__":
    main()
