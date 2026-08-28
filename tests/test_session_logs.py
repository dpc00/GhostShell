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
    assert log_path.read_text(encoding="utf-8") == "retired\nlive\n"

    rdl.debug_log(b"\x1b[31mraw")
    raw = Path(rdl.DEBUG_PATH) / "raw.log"
    assert raw.read_bytes() == b"\x1b[31mraw"


def _open_text_log(tmp_path, monkeypatch):
    monkeypatch.setattr(stl, "TEXT_LOG_DIR", str(tmp_path))
    log = SessionTextLog()
    log.open("observe")
    return log, tmp_path / "ai_observe.log"


def test_observe_writes_new_tab_lines_once(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["hello", "world"])
    assert path.read_text(encoding="utf-8") == "hello\nworld\n"
    log.observe(["hello", "world"])
    assert path.read_text(encoding="utf-8") == "hello\nworld\n"
    log.observe(["hello", "world", "more"])
    assert path.read_text(encoding="utf-8") == "hello\nworld\nmore\n"


def test_observe_writes_a_line_when_it_changes_on_the_tab(tmp_path, monkeypatch):
    log, path = _open_text_log(tmp_path, monkeypatch)
    log.observe(["a"])
    log.observe(["ab"])
    assert path.read_text(encoding="utf-8") == "a\nab\n"


def test_observe_does_not_reprint_a_line_that_stayed_on_the_tab(
    tmp_path, monkeypatch
):
    log, path = _open_text_log(tmp_path, monkeypatch)
    chrome = "Grok 4.6 (high)"
    log.observe([chrome, "a"])
    log.observe([chrome, "ab"])
    log.observe([chrome, "done"])
    text = path.read_text(encoding="utf-8")
    assert text.count(chrome) == 1
    assert "a\n" in text
    assert "ab\n" in text
    assert "done\n" in text
