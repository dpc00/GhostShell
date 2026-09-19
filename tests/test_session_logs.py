"""Smoke GhostShell session-log modules in terminal/."""

import json
import os
import threading
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


def test_file_is_the_tab(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["hello", "world"])
    log.flush_now()
    assert path.read_text(encoding="utf-8") == "hello\nworld\n"
    log.observe(["hello", "world"])
    log.flush_now()
    assert path.read_text(encoding="utf-8") == "hello\nworld\n"
    log.observe(["hello", "world", "more"])
    log.flush_now()
    assert path.read_text(encoding="utf-8") == "hello\nworld\nmore\n"
    log.close()
    assert path.read_text(encoding="utf-8") == "hello\nworld\nmore\n"

def test_lines_the_tab_trimmed_stay_in_the_log(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    lines = ["line %02d" % i for i in range(120)]
    for end in range(40, 121):
        log.observe(lines[end - 40 : end] + ["status %d" % end])
        if end % 3 == 0:
            log.flush_now()
    log.close()
    text = path.read_text(encoding="utf-8").splitlines()
    assert text[:120] == lines
    assert text[-41:] == lines[80:] + ["status 120"]



def test_file_matches_the_tab_after_a_redraw(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["one", "two", "three", "four", "five", "six"])
    log.observe(["A", "B", "C", "D", "E", "F"])
    log.close()
    assert path.read_text(encoding="utf-8") == "A\nB\nC\nD\nE\nF\n"


def test_typing_on_the_live_line_is_the_tab(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["a"])
    log.observe(["ab"])
    log.close()
    assert path.read_text(encoding="utf-8") == "ab\n"


def test_replaced_live_line_is_what_the_tab_shows(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    chrome = "Grok 4.6 (high)"
    log.observe([chrome, "thinking"])
    log.observe([chrome, "done"])
    log.close()
    assert path.read_text(encoding="utf-8") == chrome + "\ndone\n"


def test_observe_preserves_blank_lines_and_trailing_spaces(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["top  ", "", "bottom"])
    log.close()
    assert path.read_text(encoding="utf-8") == "top  \n\nbottom\n"


def test_close_flushes_the_tab(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["complete paint"])
    log.close()
    assert path.read_text(encoding="utf-8") == "complete paint\n"
    assert log.file is None


def test_write_failure_keeps_already_written_tab(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["old", "keep"])
    log.flush_now()
    assert path.read_text(encoding="utf-8") == "old\nkeep\n"

    def fail_write(payload):
        raise OSError("simulated write failure")

    monkeypatch.setattr(log.file, "write", fail_write)
    log.observe(["old", "keep", "new"])
    try:
        log.flush_now()
        assert False, "flush_now should report a failed write"
    except OSError as error:
        assert "simulated write failure" in str(error)

    assert path.read_text(encoding="utf-8") == "old\nkeep\n"




def test_terminal_close_does_not_replace_painted_snapshot_with_live_screen():
    source = Path("ai_terminal.py").read_text(encoding="utf-8")
    start = source.index("    def _close_text_log(self):")
    end = source.index("\n\n\ndef _maybe_apply_osc_title", start)
    close_source = source[start:end]
    assert "lines = self.screen.live_lines_text()" not in close_source
    assert "log.flush_live_lines" not in close_source
    assert "log.close()" in close_source


def test_cast_recorder_uses_supplied_correlated_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "CAST_DIR", str(tmp_path))
    rec = CastRecorder()
    rec.open(80, 24, ["codex"], filename_stamp="stamp_reattach")
    rec.close()
    path = tmp_path / "ai_stamp_reattach.cast"
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["version"] == 3


def test_cast_close_is_terminal_and_later_writes_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "CAST_DIR", str(tmp_path))
    rec = CastRecorder()
    rec.open(80, 24, ["codex"], filename_stamp="closed")
    rec.write("o", "before")
    rec.close()
    rec.write("o", "after")

    lines = (tmp_path / "ai_closed.cast").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines[1:]]
    assert [event[1:] for event in events] == [["o", "before"], ["x", "0"]]


def test_cast_serialization_does_not_block_the_pty_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "CAST_DIR", str(tmp_path))
    rec = CastRecorder()
    rec.open(80, 24, ["codex"], filename_stamp="async")
    entered = threading.Event()
    release = threading.Event()
    original_dumps = cr.json.dumps

    def blocking_event_dumps(value, *args, **kwargs):
        if isinstance(value, list) and value[1] == "o":
            entered.set()
            assert release.wait(2)
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(cr.json, "dumps", blocking_event_dumps)
    producer = threading.Thread(target=lambda: rec.write("o", "large replay"))
    producer.start()
    producer.join(0.5)
    assert not producer.is_alive()
    assert entered.wait(2)
    release.set()
    rec.close()

    lines = (tmp_path / "ai_async.cast").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines[1:]]
    assert [event[1:] for event in events] == [["o", "large replay"], ["x", "0"]]


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
