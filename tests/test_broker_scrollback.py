"""Unit tests for agent_broker._Scrollback, the in-memory replay ring.

Phase 1 of ai/DURABLE_BROKER_SCROLLBACK.md: behaviour of append/snapshot
only. No named pipes, no ConPTY, no reattach path.
"""
import importlib.util
import os
import threading
from pathlib import Path

import pytest

if os.name != "nt":
    pytest.skip(
        "tools/agent_broker.py sys.exits on import off Windows",
        allow_module_level=True,
    )

_SPEC = importlib.util.spec_from_file_location(
    "ghostshell_agent_broker",
    Path(__file__).resolve().parents[1] / "tools" / "agent_broker.py",
)
_BROKER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BROKER)
_Scrollback = _BROKER._Scrollback


def test_empty_snapshot_is_empty_bytes():
    assert _Scrollback(16).snapshot() == b""


def test_appends_under_cap_concatenate():
    buf = _Scrollback(16)
    buf.append(b"abc")
    buf.append(b"def")
    assert buf.snapshot() == b"abcdef"


def test_overflow_keeps_trailing_window_not_prefix():
    buf = _Scrollback(4)
    buf.append(b"abcd")
    assert buf.snapshot() == b"abcd"
    buf.append(b"ef")
    # Plausible bugs: keep the prefix ("abcd") or wrap ("efcd").
    assert buf.snapshot() == b"cdef"


def test_chunk_larger_than_cap_keeps_its_own_tail():
    buf = _Scrollback(4)
    buf.append(b"0123456789")
    assert buf.snapshot() == b"6789"


def test_overflow_across_chunks_is_the_concatenated_tail():
    buf = _Scrollback(8)
    buf.append(b"aaaa")
    buf.append(b"bbbb")
    buf.append(b"cccc")
    assert buf.snapshot() == b"bbbbcccc"


def test_concurrent_append_and_snapshot_never_exceed_cap():
    cap = 4096
    buf = _Scrollback(cap)
    written = bytearray()
    over = []

    def writer():
        for i in range(8000):
            chunk = bytes([i & 0xFF]) * 64
            written.extend(chunk)
            buf.append(chunk)

    def reader():
        for _ in range(8000):
            snap = buf.snapshot()
            if len(snap) > cap:
                over.append(len(snap))
                return

    reader_thread = threading.Thread(target=reader)
    writer_thread = threading.Thread(target=writer)
    reader_thread.start()
    writer_thread.start()
    writer_thread.join()
    reader_thread.join()

    assert over == []
    expected = bytes(written[-cap:])
    assert buf.snapshot() == expected
