"""Smoke GhostShell session-log modules in ai/terminal/."""

import json
import os
from pathlib import Path

from ai.terminal import color_scheme_log as csl
from ai.terminal import log_paths as lp
from ai.terminal import raw_debug_log as rdl
from ai.terminal.cast_recorder import CastRecorder
from ai.terminal.session_text_log import SessionTextLog
from ai.terminal import cast_recorder as cr
from ai.terminal import session_text_log as stl


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

    csl.color_scheme_log("hello-color")
    color_path = Path(td) / "ai_terminal" / "color_scheme.log"
    assert "hello-color" in color_path.read_text(encoding="utf-8")

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
