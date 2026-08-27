"""job_breakaway_test.py -- proves (or disproves) that a child process
spawned with CREATE_BREAKAWAY_FROM_JOB | DETACHED_PROCESS survives its
parent being killed by a Windows Job Object's kill-on-close semantics.

This simulates the worst case: if Sublime Text's process were ever
inside a job with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and NO
breakaway-allowed flag (the strict default), would a broker spawned
from inside it survive ST closing?

Usage:
    python tools/job_breakaway_test.py --pipe-name breaktest --pidfile out.pid

This process:
  1. Creates a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE only
     (deliberately NOT setting BREAKAWAY_OK -- the strict case).
  2. Assigns itself to that job.
  3. Spawns tools/agent_broker.py as a child with
     CREATE_BREAKAWAY_FROM_JOB | DETACHED_PROCESS, running cmd.exe.
  4. Writes the child's PID (or a spawn failure) to --pidfile.
  5. Exits immediately via os._exit(0), dropping the job's last handle
     and triggering kill-on-close for any process still assigned to it.
"""
import argparse
import ctypes
import os
import subprocess
import sys
from ctypes.wintypes import DWORD, HANDLE, LPCWSTR, LPVOID

if sys.platform != "win32":
    sys.exit("Windows-only")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateJobObjectW.argtypes = [LPVOID, LPCWSTR]
_k32.CreateJobObjectW.restype = HANDLE
_k32.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, LPVOID, DWORD]
_k32.SetInformationJobObject.restype = ctypes.c_int
_k32.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
_k32.AssignProcessToJobObject.restype = ctypes.c_int
_k32.GetCurrentProcess.argtypes = []
_k32.GetCurrentProcess.restype = HANDLE

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _make_kill_on_close_job():
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _k32.SetInformationJobObject(
        job, _JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return job


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pipe-name", required=True)
    p.add_argument("--pidfile", required=True)
    args = p.parse_args()

    job = _make_kill_on_close_job()
    me = _k32.GetCurrentProcess()
    if not _k32.AssignProcessToJobObject(job, me):
        raise ctypes.WinError(ctypes.get_last_error())
    print("[spawner pid=%d] assigned self to strict kill-on-close job" % os.getpid(),
          file=sys.stderr)

    broker_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_broker.py")

    try:
        child = subprocess.Popen(
            [sys.executable, broker_py, "--pipe-name", args.pipe_name, "--", "cmd.exe"],
            creationflags=(_CREATE_BREAKAWAY_FROM_JOB | _DETACHED_PROCESS
                           | _CREATE_NEW_PROCESS_GROUP),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as e:
        with open(args.pidfile, "w") as f:
            f.write("SPAWN_FAILED: %s" % e)
        print("[spawner] breakaway spawn FAILED: %s" % e, file=sys.stderr)
        os._exit(1)

    print("[spawner] spawned broker pid=%d with breakaway flags" % child.pid, file=sys.stderr)
    with open(args.pidfile, "w") as f:
        f.write(str(child.pid))

    print("[spawner pid=%d] exiting now -- job kill-on-close should fire for "
          "me; broker should survive only if breakaway actually worked" % os.getpid(),
          file=sys.stderr)
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
