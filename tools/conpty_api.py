"""conpty_api.py -- choose which ConPTY (Windows "pseudoconsole") GhostShell uses.

A ConPTY is the Windows device that lets a program like omp think it has a real
terminal.  Windows has one built in (in kernel32.dll).  It swallows the queries
a program sends to ask what the terminal can do (device attributes, colours,
and the Tern Surface Protocol hello), so the program never reaches GhostShell
with them.  Microsoft's Windows Terminal ships a newer ConPTY (conpty.dll plus
OpenConsole.exe) that passes those queries through.

This module returns the three ConPTY functions (create, resize, close) from
whichever implementation the profile asked for:

  * no folder given  -> Windows' built-in ConPTY (what GhostShell always used);
  * a folder given   -> conpty.dll in that folder, but only if both files are
                        there and match the SHA-256 recorded below.  Anything
                        wrong prints one line and falls back to the built-in
                        ConPTY, so a bad setting can never stop a session.

VERIFIED 2026-10-01 (measured with this repo's own _Pty class, omp 18.4.9):
built-in ConPTY delivered 0 DA1 / 0 OSC 11 / 0 APC probes to the host; the
files below delivered 11 DA1, 1 OSC 11 and 3 APC (including the TSP hello).

Windows only.  No third-party packages.
"""

import ctypes
import hashlib
import os
import platform
import urllib.request
import uuid
import zipfile

# The profile's spawn_env names the folder with this variable; the broker takes
# it out of the child's environment before the child starts.
CONPTY_FOLDER_ENV_VAR = "GHOSTSHELL_CONPTY_DIR"

CONPTY_DLL_NAME = "conpty.dll"
OPENCONSOLE_EXE_NAME = "OpenConsole.exe"

# Microsoft's signed files from Windows Terminal, build 1.24.2605.12001 (product
# "Windows Terminal", Authenticode signer "Microsoft Corporation", both valid,
# checked 2026-10-01).  Windows Terminal is MIT licensed
# (github.com/microsoft/terminal).  When these are updated, update the hashes
# and the version here together, and record it in docs/.
KNOWN_SHA256 = {
    CONPTY_DLL_NAME: "C46DCD04F52B97F6A8CF53E8F547C85A821660BED18DE2B3344AFCD4A8389AD6",
    OPENCONSOLE_EXE_NAME: "47828C3FE080212F69DFDB39AB3673170FCC7445924C76FE003CEFD18247DD5D",
}


class ConptyApi:
    """The three ConPTY functions, plus a label saying where they came from."""

    def __init__(self, create, resize, close, source):
        self.create = create    # CreatePseudoConsole(size, input_pipe, output_pipe, flags, byref(handle))
        self.resize = resize    # ResizePseudoConsole(handle, size)
        self.close = close      # ClosePseudoConsole(handle)
        self.source = source    # text for log lines: which ConPTY this is


def _sha256_of_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def builtin_api(kernel32):
    """Windows' built-in ConPTY, from the kernel32 library the caller loaded."""
    return ConptyApi(
        kernel32.CreatePseudoConsole,
        kernel32.ResizePseudoConsole,
        kernel32.ClosePseudoConsole,
        "Windows built-in ConPTY",
    )


def _check_files(folder):
    """Raise ValueError unless both files are in ``folder`` and match KNOWN_SHA256."""
    for name, expected in KNOWN_SHA256.items():
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            raise ValueError("%s is missing from %s" % (name, folder))
        found = _sha256_of_file(path)
        if found != expected:
            raise ValueError("%s does not match the expected SHA-256 (found %s)" % (name, found))


def modern_api(kernel32, folder):
    """Windows Terminal's ConPTY from ``folder``.  Raises ValueError or OSError.

    ``kernel32`` is only used to copy the argument and result types that the
    caller has already declared for the built-in functions: the new library
    exports the same three functions with the same signatures.
    """
    _check_files(folder)
    library = ctypes.WinDLL(os.path.join(folder, CONPTY_DLL_NAME), use_last_error=True)
    functions = []
    for name in ("CreatePseudoConsole", "ResizePseudoConsole", "ClosePseudoConsole"):
        function = getattr(library, name)
        reference = getattr(kernel32, name)
        function.argtypes = reference.argtypes
        function.restype = reference.restype
        functions.append(function)
    return ConptyApi(functions[0], functions[1], functions[2],
                     "Windows Terminal ConPTY from %s" % folder)


def select_api(kernel32, folder, log=print):
    """The ConPTY to use: the modern one when ``folder`` is given and good, else the built-in."""
    if not folder:
        return builtin_api(kernel32)
    try:
        return modern_api(kernel32, folder)
    except (ValueError, OSError) as error:
        log("[conpty_api] not using the Windows Terminal ConPTY (%s); falling back to the built-in one" % error)
        return builtin_api(kernel32)


# ---- getting the two files: download once from Microsoft's own NuGet package --------

# Microsoft publishes exactly these two files for programs like this one as the NuGet
# package Microsoft.Windows.Console.ConPTY (MIT, built from github.com/microsoft/terminal).
# Version 1.24.260512001 is the same build as above; its x64 files were compared with
# KNOWN_SHA256 on 2026-10-02 and are byte for byte identical.  GhostShell does not
# re-host them: it downloads the package from nuget.org when a profile needs them,
# checks the package's own SHA-256 and then each file's, and keeps the two files here.
NUGET_URL = ("https://api.nuget.org/v3-flatcontainer/microsoft.windows.console.conpty/"
             "1.24.260512001/microsoft.windows.console.conpty.1.24.260512001.nupkg")
NUGET_SHA256 = "F889A9272A8B257DC6D5BE7525626FDB0F7CA6B5CE7E13093FC4BC979D24F484"
NUGET_MEMBERS = {                         # file name -> path inside the package (x64)
    CONPTY_DLL_NAME: "runtimes/win-x64/native/conpty.dll",
    OPENCONSOLE_EXE_NAME: "build/native/runtimes/x64/OpenConsole.exe",
}
# Where the two files are kept (next to the other downloaded binary, ghostty-vt.dll).
DEFAULT_FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "terminal", "bin", "conpty")


def default_folder_if_ready():
    """DEFAULT_FOLDER when both files are already there and verified, else None.  No network."""
    try:
        _check_files(DEFAULT_FOLDER)
    except ValueError:
        return None
    return DEFAULT_FOLDER


def ensure_default_files(log=print, timeout=30):
    """Make sure DEFAULT_FOLDER holds the two verified files, downloading them if needed.

    Returns the folder, or None (after one log line saying why) on any failure: wrong
    CPU type, no network, a checksum that does not match.  Nothing is ever left half
    written: each file is written under a temporary name, verified, then moved into place.
    This waits on the network, so call it from a background thread, never the UI thread.
    """
    folder = default_folder_if_ready()
    if folder:
        return folder
    if platform.machine().lower() not in ("amd64", "x86_64"):
        log("[conpty_api] the Windows Terminal ConPTY download is only set up for x64 Windows (this is %s)"
            % platform.machine())
        return None
    package_path = os.path.join(os.path.dirname(DEFAULT_FOLDER), ".conpty-%s.nupkg.part" % uuid.uuid4().hex)
    written = []
    try:
        os.makedirs(DEFAULT_FOLDER, exist_ok=True)
        with urllib.request.urlopen(NUGET_URL, timeout=timeout) as response, open(package_path, "wb") as out:
            while True:
                block = response.read(1 << 16)
                if not block:
                    break
                out.write(block)
        found = _sha256_of_file(package_path)
        if found != NUGET_SHA256:
            raise ValueError("the downloaded package does not match the expected SHA-256 (found %s)" % found)
        with zipfile.ZipFile(package_path) as package:
            for name, member in NUGET_MEMBERS.items():
                part = os.path.join(DEFAULT_FOLDER, "%s.%s.part" % (name, uuid.uuid4().hex))
                written.append(part)
                with package.open(member) as source, open(part, "wb") as out:
                    out.write(source.read())
                if _sha256_of_file(part) != KNOWN_SHA256[name]:
                    raise ValueError("%s from the package does not match the expected SHA-256" % name)
        for part, name in zip(written, NUGET_MEMBERS):
            os.replace(part, os.path.join(DEFAULT_FOLDER, name))
        written = []
        log("[conpty_api] Windows Terminal ConPTY downloaded and verified into %s" % DEFAULT_FOLDER)
        return default_folder_if_ready()
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        log("[conpty_api] could not get the Windows Terminal ConPTY (%s); using the built-in one" % error)
        return None
    finally:
        for leftover in [package_path] + written:
            try:
                os.remove(leftover)
            except OSError:
                pass
