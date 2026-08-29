"""Smoke GhostShell session-log modules in terminal/."""

import json
import os
from pathlib import Path

from terminal import color_scheme_log as csl
from terminal import log_paths as lp
from terminal import raw_debug_log as rdl
from terminal.cast_recorder import CastRecorder
from terminal.session_text_log import SessionTextLog
from terminal import cast_recorder as cr
from terminal import session_text_log as stl


def test_color_scheme_log_and_recorders_write_under_log_root(tmp_path, monkeypatch):
    td = str(tmp_path)
    monkeypatch.setattr(lp, "LOG_ROOT", td)
    monkeypatch.setattr(
        rdl, "DEBUG_PATH", os.path.join(td, "ai_terminal_raw_ansi_stream_debug_logs")
    )
    monkeypatch.setattr(
        cr,
        "CAST_DIR",
        os.path.join(td, "ai_terminal_asciinema_casts_for_troubleshooting_rendering"),
    )
    monkeypatch.setattr(
        stl, "TEXT_LOG_DIR", os.path.join(td, "ai_terminal_session_text_logs")
    )

    csl.color_scheme_log("hello-color")  # no-op; must not raise

    rec = CastRecorder(notify=lambda m: None)
    rec.open(80, 24, ["claude", "--api-key", "sk-abcdefghijklmnop"])
    rec.write("o", "hi")
    rec.close()
    casts = list(Path(cr.CAST_DIR).glob("*.cast"))
    assert len(casts) == 1
    lines = casts[0].read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 3
    assert "sk-" not in header["command"]
    events = [json.loads(x) for x in lines[1:]]
    assert events[0][1] == "o" and events[0][2] == "hi"
    assert events[-1][1] == "x"

    log = SessionTextLog()
    log.open("2026-08-15_000000")
    log.write_line("retired")
    log.flush_live_lines(["live", ""])
    log.close()
    log_path = Path(stl.TEXT_LOG_DIR) / "ai_2026-08-15_000000.log"
    assert log_path.read_text(encoding="utf-8") == "live\n\n"

    rdl.debug_log(b"\x1b[31mraw")
    raw = Path(rdl.DEBUG_PATH) / "raw.log"
    assert raw.read_bytes() == b"\x1b[31mraw"


def _open_text_log(tmp_path, monkeypatch):
    monkeypatch.setattr(stl, "TEXT_LOG_DIR", str(tmp_path))
    log = SessionTextLog()
    log.open("observe")
    return log, tmp_path / "ai_observe.log"


def test_observe_keeps_the_latest_complete_tab_paint(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["hello", "world"])
    assert path.read_text(encoding="utf-8") == "hello\nworld\n"
    log.observe(["hello", "world"])
    assert path.read_text(encoding="utf-8") == "hello\nworld\n"
    log.observe(["hello", "world", "more"])
    assert path.read_text(encoding="utf-8") == "hello\nworld\nmore\n"


def test_observe_replaces_a_line_when_it_changes_on_the_tab(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["a"])
    log.observe(["ab"])
    assert path.read_text(encoding="utf-8") == "ab\n"


def test_observe_does_not_preserve_superseded_tab_frames(
    tmp_path, monkeypatch
):
    log, path = _open_text_log(tmp_path, monkeypatch)
    chrome = "Grok 4.6 (high)"
    log.observe([chrome, "a"])
    log.observe([chrome, "ab"])
    log.observe([chrome, "done"])
    text = path.read_text(encoding="utf-8")
    assert text.count(chrome) == 1
    assert "a\n" not in text
    assert "ab\n" not in text
    assert "done\n" in text


def test_observe_preserves_blank_lines_and_trailing_spaces(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["top  ", "", "bottom"])
    assert path.read_text(encoding="utf-8") == "top  \n\nbottom\n"


def test_cast_recorder_uses_supplied_correlated_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "CAST_DIR", str(tmp_path))
    rec = CastRecorder()
    rec.open(80, 24, ["codex"], filename_stamp="stamp_reattach")
    rec.close()
    path = tmp_path / "ai_stamp_reattach.cast"
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["version"] == 3


def test_cast_header_is_fsynced_before_open_returns(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "CAST_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(cr.os, "fsync", lambda fd: calls.append(fd))
    rec = CastRecorder()
    rec.open(80, 24, ["codex"], filename_stamp="durable")
    assert calls == [rec.file.fileno()]
    rec.close()


def test_failed_cast_header_does_not_leave_zero_byte_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "CAST_DIR", str(tmp_path))

    class BrokenHandle:
        def __init__(self, path):
            self._handle = open(path, "w", encoding="utf-8")

        def write(self, _text):
            raise OSError("simulated header failure")

        def close(self):
            self._handle.close()

    monkeypatch.setattr(
        cr, "open_private",
        lambda path, mode, **kwargs: BrokenHandle(path),
    )
    rec = CastRecorder()
    try:
        rec.open(80, 24, ["codex"], filename_stamp="broken")
        assert False, "open should propagate the header failure"
    except OSError as error:
        assert "simulated header failure" in str(error)
    assert rec.file is None
    assert not (tmp_path / "ai_broken.cast").exists()
