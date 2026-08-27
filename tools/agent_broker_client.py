"""agent_broker_client.py -- minimal attach/detach test client for
agent_broker.py. Connects to a named pipe, streams the broker's output to
stdout, forwards stdin to the broker. Ctrl+C (or closing this window)
disconnects WITHOUT stopping the broker or its child -- reattach by running
this again with the same --pipe-name.

Usage:
    python tools/agent_broker_client.py --pipe-name test1
"""
import argparse
import ctypes
import os
import sys
import threading
import time
from ctypes import byref, c_char, c_void_p
from ctypes.wintypes import HANDLE, DWORD, LPCWSTR

if sys.platform != "win32":
    sys.exit("agent_broker_client.py is Windows-only (named pipes).")

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


def connect(path, access, timeout_s=10.0):
    deadline = time.time() + timeout_s
    while True:
        h = _k32.CreateFileW(path, access, 0, None, _OPEN_EXISTING, 0, None)
        if h != _INVALID_HANDLE_VALUE:
            return h
        err = ctypes.get_last_error()
        if time.time() > deadline:
            raise OSError("could not connect to pipe %r (GetLastError %d)" % (path, err))
        if err == _ERROR_PIPE_BUSY:
            _k32.WaitNamedPipeW(path, 2000)
        elif err == _ERROR_FILE_NOT_FOUND:
            # Pipe doesn't exist yet -- WaitNamedPipeW returns immediately in
            # this case per MSDN (it only waits for a busy instance to free
            # up, not for first creation), so poll instead.
            time.sleep(0.2)
        else:
            raise OSError("could not connect to pipe %r (GetLastError %d)" % (path, err))


def _pump_output(handle, stop_evt):
    buf = (c_char * 4096)()
    n = DWORD(0)
    while not stop_evt.is_set():
        ok = _k32.ReadFile(handle, buf, 4096, byref(n), None)
        if not ok:
            err = ctypes.get_last_error()
            if err in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_HANDLE_EOF):
                break
            print("\n[client] ReadFile failed (GetLastError %d)" % err, file=sys.stderr)
            break
        if n.value == 0:
            break
        sys.stdout.buffer.write(bytes(buf[: n.value]))
        sys.stdout.buffer.flush()
    stop_evt.set()


def main():
    p = argparse.ArgumentParser(description="Attach to an agent_broker.py pipe.")
    p.add_argument("--pipe-name", required=True)
    p.add_argument("--send", default=None,
                    help="Non-interactive mode: write this text + CRLF, read for "
                         "--read-seconds, then exit. Useful for scripted checks.")
    p.add_argument("--read-seconds", type=float, default=2.0)
    args = p.parse_args()

    # Two separate unidirectional pipes -- <name> (broker writes, we read)
    # and <name>-in (we write, broker reads) -- not one duplex pipe. A
    # single reader thread ReadFile()ing while the broker WriteFile()s the
    # same handle from another thread was empirically unreliable: a pending
    # ReadFile could make a concurrent WriteFile on that handle simply never
    # complete under a fast burst, with no error on either side.
    h_out = connect("\\\\.\\pipe\\" + args.pipe_name, _GENERIC_READ)
    h_in = connect("\\\\.\\pipe\\" + args.pipe_name + "-in", _GENERIC_WRITE)
    print("[client] connected to \\\\.\\pipe\\%s (+ -in)" % args.pipe_name, file=sys.stderr)

    stop_evt = threading.Event()
    reader = threading.Thread(target=_pump_output, args=(h_out, stop_evt), daemon=True)
    reader.start()

    if args.send is not None:
        written = DWORD(0)
        data = (args.send + "\r\n").encode("utf-8")
        _k32.WriteFile(h_in, data, len(data), byref(written), None)
        time.sleep(args.read_seconds)
        # os._exit(), not CloseHandle()+return: the reader thread has a
        # blocking ReadFile pending on `h_out` on another thread, and closing
        # the handle out from under that pending read can hang here
        # instead of returning (observed live). The OS reclaims the handle
        # on process exit regardless, so skip the graceful close.
        sys.stdout.buffer.flush()
        os._exit(0)

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
        print("\n[client] disconnected (broker keeps running)", file=sys.stderr)
        # Same CloseHandle-vs-pending-ReadFile hang as the --send path above.
        os._exit(0)


if __name__ == "__main__":
    main()
