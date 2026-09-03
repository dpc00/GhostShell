"""Regression checks for detachable broker recovery."""

import ast
import os
import time
from pathlib import Path

from tests.sublime_stub import install as _install_stubs

_install_stubs()

import ai_terminal


ROOT = Path(__file__).resolve().parents[1]


def test_pid_liveness_recognizes_current_process():
    assert ai_terminal._pid_is_alive(os.getpid())


def test_pid_liveness_rejects_invalid_pid():
    assert not ai_terminal._pid_is_alive(0x7FFFFFFF)


def test_broker_process_matches_accepts_a_freshly_recorded_pid():
    # The test runner itself is python.exe with a start time from moments
    # ago -- exactly what a just-published broker registry record looks like.
    assert ai_terminal._broker_process_matches(os.getpid(), time.time())


def test_broker_process_matches_rejects_reused_pid():
    # Same live, real python.exe process, but a created_at timestamp far
    # outside its actual start time -- this is exactly what a stale registry
    # record looks like once Windows has recycled the PID onto an unrelated
    # process days after the original broker died without cleaning up.
    # Plain PID-alive checks can't tell these apart; _broker_process_matches
    # must, because it is what stops "Recover Orphaned Session" from
    # offering a dead session that will hang for ~10s and then fail.
    assert not ai_terminal._broker_process_matches(os.getpid(), time.time() - 100000)


def _class_methods(path, class_name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {node.name for node in cls.body if isinstance(node, ast.FunctionDef)}


def test_output_server_exposes_disconnect_hook():
    methods = _class_methods(ROOT / "tools" / "agent_broker.py", "_OutputServer")
    assert "disconnect_client" in methods


def test_input_disconnect_is_wired_to_output_cleanup():
    source = (ROOT / "tools" / "agent_broker.py").read_text(encoding="utf-8")
    assert "on_disconnect=out_server.disconnect_client" in source


def test_broker_publishes_and_removes_external_session_registry():
    source = (ROOT / "tools" / "agent_broker.py").read_text(encoding="utf-8")
    assert "def _publish_registry(" in source
    assert "os.replace(temporary, path)" in source
    assert "_publish_registry(" in source[source.index("def main():"):]
    assert "_remove_registry(args.registry_file)" in source


def test_broker_lifecycle_log_captures_job_membership_and_child_exit_code():
    source = (ROOT / "tools" / "agent_broker.py").read_text(encoding="utf-8")
    launcher = (ROOT / "ai_terminal.py").read_text(encoding="utf-8")

    assert '"--log-file"' in launcher
    assert "_configure_lifecycle_log(args.log_file)" in source
    assert "_current_process_is_in_job()" in source
    assert "child process exited pid=%d exit_code=%d (0x%08X)" in source
    assert "def exit_code(self):" in source
    assert "broker stopping normally; child_alive=%r child_exit_code=%r" in source


def test_outside_job_launcher_uses_short_lived_interactive_scheduled_task():
    source = (ROOT / "tools" / "spawn_outside_job.ps1").read_text(encoding="utf-8")

    assert "New-ScheduledTaskPrincipal" in source
    assert "-LogonType Interactive" in source
    assert "Start-ScheduledTask" in source
    assert "Unregister-ScheduledTask" in source


def test_recovery_tool_has_guarded_stale_output_path():
    source = (ROOT / "tools" / "recover_console.py").read_text(encoding="utf-8")
    assert "def connect_output(pipe_name):" in source
    assert "if getattr(error, \"winerror\", None) != _ERROR_PIPE_BUSY" in source
    assert "h_in = connect(in_path, _GENERIC_WRITE" in source


def test_recovery_tool_is_a_raw_vt_console_client():
    source = (ROOT / "tools" / "recover_console.py").read_text(encoding="utf-8")
    assert "ENABLE_VIRTUAL_TERMINAL_PROCESSING" in source
    assert "ENABLE_VIRTUAL_TERMINAL_INPUT" in source
    assert "SetConsoleMode" in source
    assert "SetConsoleCtrlHandler" in source
    assert "sys.stdin.readline" not in source
    assert 'pipe_name + "-ctl"' in source
    assert "RESIZE" in source
    start = source.index("def _enable_raw_vt(")
    end = source.index("def _pump_output(", start)
    raw = source[start:end]
    assert "_ENABLE_LINE_INPUT" in raw
    assert "_ENABLE_ECHO_INPUT" in raw
    assert "_ENABLE_PROCESSED_INPUT" in raw


def test_recovery_tool_discovers_sessions_from_registry():
    source = (ROOT / "tools" / "recover_console.py").read_text(encoding="utf-8")
    assert "broker_sessions" in source
    start = source.index("def find_sessions():")
    end = source.index("def connect(", start)
    finder = source[start:end]
    assert "Get-CimInstance" not in finder
    assert ".json" in finder


def test_nonempty_terminal_selection_is_preserved_before_endpoint_checks():
    source = (ROOT / "ai_terminal.py").read_text(encoding="utf-8")
    selection_guard = source.index("if not sel[0].empty():")
    endpoint_check = source.index("pt = sel[0].b", selection_guard)
    assert selection_guard < endpoint_check
    assert "term._user_owns_caret = True" in source[selection_guard:endpoint_check]


def test_reattach_command_is_exposed_and_detaches_without_killing_broker():
    import json

    commands = json.loads(
        (ROOT / "Default.sublime-commands").read_text(encoding="utf-8")
    )
    assert any(
        item.get("command") == "ai_terminal_reattach_session"
        and item.get("caption") == "Ai Terminal: Recover Orphaned Session..."
        for item in commands
    )
    source = (ROOT / "ai_terminal.py").read_text(encoding="utf-8")
    start = source.index("class AiTerminalReattachSessionCommand")
    end = source.index("class AiTerminalNukeCommand", start)
    command_source = source[start:end]
    assert "_revive_terminal_client(term, self.window)" in command_source
    assert "explicit_kill" not in command_source


def test_revive_frozen_tab_command_is_exposed_and_never_kills_agent():
    import json

    commands = json.loads(
        (ROOT / "Default.sublime-commands").read_text(encoding="utf-8")
    )
    assert any(
        item.get("command") == "ai_terminal_revive_frozen_tab"
        and item.get("caption") == "Ai Terminal: Revive Frozen Tab"
        for item in commands
    )
    source = (ROOT / "ai_terminal.py").read_text(encoding="utf-8")
    helper_start = source.index("def _revive_terminal_client(")
    helper_end = source.index("class AiTerminalReviveFrozenTabCommand", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "pty.kill()" in helper_source
    assert "explicit_kill" not in helper_source
    assert "_reattach_broker_view" in helper_source


def test_reattach_command_discovers_orphaned_broker_processes():
    source = (ROOT / "ai_terminal.py").read_text(encoding="utf-8")
    start = source.index("class AiTerminalReattachSessionCommand")
    end = source.index("class AiTerminalNukeCommand", start)
    command_source = source[start:end]
    assert "_registered_brokers(" in command_source
    assert "def _attach_orphan(" in command_source
    assert 'state = "orphaned broker"' in command_source
    assert "threading.Thread(target=self._discover" in command_source
    assert "_BROKER_PROFILE_SETTING" in command_source
    assert 'name="Recovered Codex"' not in command_source
    assert "timeout=" in command_source[command_source.index("def _running_brokers("):]


def test_broker_reattach_never_connects_named_pipes_on_sublime_main_thread():
    source = (ROOT / "ai_terminal.py").read_text(encoding="utf-8")
    start = source.index("def _reattach_broker_view(")
    end = source.index("class AiTerminalOpenHereCommand", start)
    reattach_source = source[start:end]
    worker = reattach_source.index("def _connect_worker():")
    pipe_start = reattach_source.index("pty.start()", worker)
    thread_start = reattach_source.index(
        "threading.Thread(target=_connect_worker, daemon=True).start()", pipe_start
    )
    assert worker < pipe_start < thread_start
    assert "if vid in _BROKER_CONNECTING:" in reattach_source
    assert "_BROKER_CONNECTING.add(vid)" in reattach_source
    assert "_apply_terminal_view_settings(view)" in reattach_source


def test_broker_reattach_prepares_logs_only_after_connection_and_registry_win():
    source = (ROOT / "ai_terminal.py").read_text(encoding="utf-8")
    start = source.index("def _reattach_broker_view(")
    end = source.index("class AiTerminalOpenHereCommand", start)
    reattach_source = source[start:end]
    finish = reattach_source.index("def _finish_connected():")
    registry_check = reattach_source.index("if existing is not None:")
    prepare = reattach_source.index("term.prepare(reattach=True)")
    worker = reattach_source.index("def _connect_worker():")
    assert finish < registry_check < prepare < worker
    assert "term.prepare(" not in reattach_source[:finish]


def test_plugin_load_reapplies_terminal_view_styling_before_reattach():
    source = (ROOT / "ai_terminal.py").read_text(encoding="utf-8")
    start = source.index("def plugin_loaded():")
    end = source.index("def plugin_unloaded():", start)
    loaded_source = source[start:end]
    style = loaded_source.rindex("_apply_terminal_view_settings(view)")
    reattach = loaded_source.rindex("_maybe_reattach_broker(view)")
    assert style < reattach
