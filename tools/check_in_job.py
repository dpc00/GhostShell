"""check_in_job.py -- reports whether a running process (by PID) is
currently assigned to any Windows Job Object.

Usage:
    python tools/check_in_job.py <pid>
"""
import ctypes
import sys
from ctypes.wintypes import BOOL, DWORD, HANDLE

if sys.platform != "win32":
    sys.exit("Windows-only")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
_k32.OpenProcess.restype = HANDLE
_k32.IsProcessInJob.argtypes = [HANDLE, HANDLE, ctypes.POINTER(BOOL)]
_k32.IsProcessInJob.restype = BOOL
_k32.CloseHandle.argtypes = [HANDLE]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def main():
    pid = int(sys.argv[1])
    h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        print("could not open pid %d (GetLastError %d)" % (pid, ctypes.get_last_error()))
        sys.exit(1)
    result = BOOL(0)
    ok = _k32.IsProcessInJob(h, None, ctypes.byref(result))
    _k32.CloseHandle(h)
    if not ok:
        print("IsProcessInJob failed (GetLastError %d)" % ctypes.get_last_error())
        sys.exit(1)
    print("pid %d in a job object: %s" % (pid, bool(result.value)))


if __name__ == "__main__":
    main()
