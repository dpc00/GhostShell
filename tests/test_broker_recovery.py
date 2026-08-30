"""Regression checks for detachable broker recovery."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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


def test_recovery_tool_has_guarded_stale_output_path():
    source = (ROOT / "tools" / "recover_console.py").read_text(encoding="utf-8")
    assert "def connect_output(pipe_name):" in source
    assert "if getattr(error, \"winerror\", None) != _ERROR_PIPE_BUSY" in source
    assert "h_in = connect(in_path, _GENERIC_WRITE" in source


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
        for item in commands
    )
    source = (ROOT / "ai_terminal.py").read_text(encoding="utf-8")
    start = source.index("class AiTerminalReattachSessionCommand")
    end = source.index("class AiTerminalNukeCommand", start)
    command_source = source[start:end]
    assert "pty.kill()" in command_source
    assert "explicit_kill" not in command_source


def test_reattach_command_discovers_orphaned_broker_processes():
    source = (ROOT / "ai_terminal.py").read_text(encoding="utf-8")
    start = source.index("class AiTerminalReattachSessionCommand")
    end = source.index("class AiTerminalNukeCommand", start)
    command_source = source[start:end]
    assert "Get-CimInstance Win32_Process" in command_source
    assert "def _attach_orphan(" in command_source
    assert 'state = "orphaned broker"' in command_source
    assert "threading.Thread(target=self._discover" in command_source
    assert '_terminal_view(self.window, name="Recovered Codex")' in command_source


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
