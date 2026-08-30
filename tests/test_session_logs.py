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


def test_observe_preserves_absence_of_final_newline(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["exact", "paint"], trailing_newline=False)
    log.close()
    assert path.read_bytes() == b"exact\npaint"


def test_painted_tab_passes_its_actual_final_newline_state():
    source = Path("ai_terminal.py").read_text(encoding="utf-8")
    start = source.index("def _log_painted_tab(term, text):")
    end = source.index("\n\n\ndef _update_debug_status", start)
    log_source = source[start:end]
    assert 'trailing_newline=painted.endswith("\\n")' in log_source


def test_text_log_close_waits_for_an_in_progress_paint(tmp_path, monkeypatch):
    log, _path = _open_text_log(tmp_path, monkeypatch)
    entered_replace = threading.Event()
    release_replace = threading.Event()
    original_replace = stl.os.replace

    def blocking_replace(source, destination):
        entered_replace.set()
        assert release_replace.wait(2)
        return original_replace(source, destination)

    monkeypatch.setattr(stl.os, "replace", blocking_replace)
    paint = threading.Thread(target=lambda: log.observe(["complete paint"]))
    paint.start()
    assert entered_replace.wait(2)
    closing = threading.Thread(target=log.close)
    closing.start()
    assert closing.is_alive()
    release_replace.set()
    paint.join(2)
    closing.join(2)
    assert not paint.is_alive()
    assert not closing.is_alive()
    assert log.file is None


def test_observe_atomically_replaces_the_previous_snapshot(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    replacements = []
    original_replace = stl.os.replace

    def recording_replace(source, destination):
        assert Path(source).read_text(encoding="utf-8") == "new\nsnapshot\n"
        assert path.read_text(encoding="utf-8") == "old\nsnapshot\n"
        replacements.append((source, destination))
        return original_replace(source, destination)

    log.observe(["old", "snapshot"])
    monkeypatch.setattr(stl.os, "replace", recording_replace)
    log.observe(["new", "snapshot"])
    log.close()

    assert len(replacements) == 1
    assert path.read_text(encoding="utf-8") == "new\nsnapshot\n"
    assert not (tmp_path / "ai_observe.log.tmp").exists()


def test_observe_can_replace_snapshot_while_an_external_reader_is_open(
    tmp_path, monkeypatch
):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["first"])
    with path.open("r", encoding="utf-8") as reader:
        assert reader.read() == "first\n"
        log.observe(["second"])
    log.close()
    assert path.read_text(encoding="utf-8") == "second\n"


def test_failed_atomic_replace_keeps_old_snapshot_and_can_retry(
    tmp_path, monkeypatch
):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["old"])
    original_replace = stl.os.replace

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(stl.os, "replace", fail_replace)
    try:
        log.observe(["new"])
        assert False, "observe should report a failed replacement"
    except OSError as error:
        assert "simulated replace failure" in str(error)

    assert path.read_text(encoding="utf-8") == "old\n"
    assert not (tmp_path / "ai_observe.log.tmp").exists()

    monkeypatch.setattr(stl.os, "replace", original_replace)
    log.observe(["new"])
    log.close()
    assert path.read_text(encoding="utf-8") == "new\n"


def test_temp_file_permission_failure_does_not_truncate_old_snapshot(
    tmp_path, monkeypatch
):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["old"])
    original_open = stl.open_private
    calls = []

    def fail_temp_open(target, mode, **kwargs):
        calls.append((target, mode))
        if target.endswith(".tmp"):
            raise PermissionError("simulated temp permission failure")
        return original_open(target, mode, **kwargs)

    monkeypatch.setattr(stl, "open_private", fail_temp_open)
    try:
        log.observe(["new"])
        assert False, "observe should report a temp-file permission failure"
    except PermissionError as error:
        assert "simulated temp permission failure" in str(error)

    assert path.read_text(encoding="utf-8") == "old\n"
    assert not any(target == str(path) and mode == "w" for target, mode in calls)


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
