"""Unit tests for agent_broker._Scrollback.

Phase 1: in-memory append/snapshot. Phase 3: circular file mirror.
No named pipes, no ConPTY, no reattach path.
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
read_scrollback_file = _BROKER.read_scrollback_file
_scrollback_path_for_registry = _BROKER._scrollback_path_for_registry


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


def test_registry_path_maps_to_sibling_scrollback_file():
    assert _scrollback_path_for_registry(r"C:\gs\pipe.json") == r"C:\gs\pipe.scrollback"
    assert _scrollback_path_for_registry(None) is None


def test_appends_are_mirrored_in_the_scrollback_file(tmp_path):
    path = str(tmp_path / "s.scrollback")
    buf = _Scrollback(16, path=path)
    buf.append(b"abc")
    buf.append(b"def")
    assert buf.snapshot() == b"abcdef"
    assert read_scrollback_file(path) == b"abcdef"
    buf.close()


def test_file_overflow_matches_in_memory_trailing_window(tmp_path):
    path = str(tmp_path / "s.scrollback")
    buf = _Scrollback(8, path=path)
    buf.append(b"aaaa")
    buf.append(b"bbbb")
    buf.append(b"cccc")
    snap = buf.snapshot()
    assert snap == b"bbbbcccc"
    assert read_scrollback_file(path) == snap
    buf.close()


def test_chunk_larger_than_cap_mirrors_its_tail(tmp_path):
    path = str(tmp_path / "s.scrollback")
    buf = _Scrollback(4, path=path)
    buf.append(b"0123456789")
    snap = buf.snapshot()
    assert snap == b"6789"
    assert read_scrollback_file(path) == snap
    buf.close()


def test_crash_without_close_still_recovers_fsynced_bytes(tmp_path):
    path = str(tmp_path / "s.scrollback")
    buf = _Scrollback(16, path=path)
    buf.append(b"hello")
    buf.snapshot()
    # Simulate process death: drop the handle without close()/unlink.
    buf._file.close()
    buf._file = None
    buf._path = None
    assert read_scrollback_file(path) == b"hello"
    assert os.path.isfile(path)


def test_close_unlinks_the_scrollback_file(tmp_path):
    path = str(tmp_path / "s.scrollback")
    buf = _Scrollback(8, path=path)
    buf.append(b"xy")
    buf.snapshot()
    assert os.path.isfile(path)
    buf.close()
    assert not os.path.isfile(path)
    buf.close()  # idempotent


def test_snapshot_fsyncs_before_returning(tmp_path, monkeypatch):
    path = str(tmp_path / "s.scrollback")
    synced = []
    real_fsync = os.fsync

    def spy(fd):
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    buf = _Scrollback(8, path=path)
    buf.append(b"ab")
    before = len(synced)
    assert buf.snapshot() == b"ab"
    assert len(synced) > before
    buf.close()


def test_remove_registry_unlinks_scrollback_sibling(tmp_path):
    registry = tmp_path / "pipe.json"
    registry.write_text("{}", encoding="utf-8")
    side = tmp_path / "pipe.scrollback"
    side.write_bytes(b"leftover")
    _BROKER._remove_registry(str(registry))
    assert not registry.exists()
    assert not side.exists()
