"""Opt-in live Windows integration test for detachable broker persistence.

Run explicitly with:
    set GHOSTSHELL_RUN_SCHEDULED_BROKER_TEST=1
    python -m pytest tests/test_scheduled_broker_integration.py -v -s

This creates a real short-lived scheduled task, ConPTY, named-pipe broker, and
cmd.exe child. Cleanup explicitly kills the broker even when an assertion
fails. It is opt-in so the ordinary unit suite never mutates Task Scheduler.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "win32"
    or os.environ.get("GHOSTSHELL_RUN_SCHEDULED_BROKER_TEST") != "1",
    reason="set GHOSTSHELL_RUN_SCHEDULED_BROKER_TEST=1 for live Windows test",
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.sublime_stub import install as _install_stubs  # noqa: E402

_install_stubs()

import ai_terminal  # noqa: E402


class _Settings:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def _wait_until(predicate, timeout=10.0, message="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


def _reader(pty, chunks, replay_events):
    pty.read(chunks.append, lambda: replay_events.append(time.monotonic()))


def test_scheduled_broker_detaches_and_reconnects_same_child(tmp_path, monkeypatch):
    registry = tmp_path / "registry"
    log_file = Path.home() / "data" / "logs" / "ai_terminal" / "agent_broker.log"
    monkeypatch.setattr(
        ai_terminal,
        "_settings",
        _Settings({"broker_registry_dir": str(registry)}),
        raising=False,
    )
    pipe = "ghostshell_it_" + uuid.uuid4().hex[:16]
    marker = "PERSIST_" + uuid.uuid4().hex
    cycles = int(os.environ.get("GHOSTSHELL_BROKER_TEST_CYCLES", "10"))
    current = ai_terminal._BrokerPty(
        pipe, [os.environ.get("COMSPEC", "cmd.exe")], str(ROOT), 90, 30,
        os.environ.copy(), profile_name="Integration Test",
    )
    try:
        current.start()
        chunks = []
        replay_events = []
        reader = threading.Thread(
            target=_reader, args=(current, chunks, replay_events), daemon=True,
        )
        reader.start()
        _wait_until(
            lambda: list(registry.glob(pipe + ".json")),
            message="broker registry was not published",
        )
        record = json.loads((registry / (pipe + ".json")).read_text(encoding="utf-8"))
        broker_pid = int(record["broker_pid"])
        assert ai_terminal._pid_is_alive(broker_pid)
        assert not (registry / (pipe + ".json.launch")).exists()

        for cycle in range(cycles):
            value = "%s_%04d" % (marker, cycle)
            current.write(("set GHOSTSHELL_TEST_STATE=" + value + "\r").encode())
            current.kill()
            reader.join(timeout=3.0)
            assert not reader.is_alive(), "client reader did not stop on detach"
            assert ai_terminal._pid_is_alive(broker_pid)

            # A fresh client must attach to the same broker and observe state
            # established by the previous client.
            current = ai_terminal._BrokerPty(
                pipe, [os.environ.get("COMSPEC", "cmd.exe")], str(ROOT), 90, 30,
                os.environ.copy(), profile_name="Integration Test",
                allow_spawn=False,
            )
            current.start()
            chunks = []
            replay_events = []
            reader = threading.Thread(
                target=_reader, args=(current, chunks, replay_events), daemon=True,
            )
            reader.start()
            _wait_until(
                lambda: replay_events,
                timeout=5.0,
                message="reconnected client did not finish replay",
            )
            current.write(b"echo %GHOSTSHELL_TEST_STATE%\r")
            _wait_until(
                lambda: value.encode() in b"".join(chunks),
                timeout=10.0,
                message="cycle %d did not observe preserved child state" % cycle,
            )
            assert ai_terminal._pid_is_alive(broker_pid)

        # Model an editor crash: another OS process owns all three client
        # handles and disappears without calling _BrokerPty.kill(). The broker
        # must notice the broken input pipe and make all endpoints attachable.
        current.kill()
        reader.join(timeout=3.0)
        crash_marker = marker + "_CRASH"
        ready = tmp_path / "crash-client-ready"
        crash_client = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "broker_crash_client.py"),
             pipe, crash_marker, str(ready)],
            cwd=str(ROOT),
        )
        try:
            _wait_until(
                ready.exists, timeout=10.0,
                message="crash client did not attach and publish readiness",
            )
            crash_client.terminate()
            crash_client.wait(timeout=5.0)
        finally:
            if crash_client.poll() is None:
                crash_client.kill()
                crash_client.wait(timeout=5.0)

        current = ai_terminal._BrokerPty(
            pipe, [os.environ.get("COMSPEC", "cmd.exe")], str(ROOT), 90, 30,
            os.environ.copy(), profile_name="Integration Test",
            allow_spawn=False,
        )
        current.start()
        chunks = []
        replay_events = []
        reader = threading.Thread(
            target=_reader, args=(current, chunks, replay_events), daemon=True,
        )
        reader.start()
        current.write(b"echo %GHOSTSHELL_TEST_STATE%\r")
        _wait_until(
            lambda: crash_marker.encode() in b"".join(chunks),
            timeout=10.0,
            message="broker did not recover after abrupt client-process death",
        )
        assert ai_terminal._pid_is_alive(broker_pid)

        # The scheduled task is only a launch trampoline and must already be
        # gone while its broker remains alive.
        task_name = "GhostShell Broker " + pipe
        query = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", task_name],
            capture_output=True, timeout=10.0,
        )
        assert query.returncode != 0, "temporary scheduled task still registered"
        assert log_file.exists()
    finally:
        try:
            current.explicit_kill()
        except Exception:
            pass
        _wait_until(
            lambda: not (registry / (pipe + ".json")).exists(),
            timeout=10.0,
            message="broker registry was not removed after explicit kill",
        )
