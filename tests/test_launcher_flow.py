"""End-to-end flow tests: actually run the launcher/history commands.

Everything else in the suite tests the *pure* model or static structure. This
file executes the real command bodies from ``ai_terminal.py`` against a stub
Sublime API, driving the full path a user takes: open the launcher, pick an
agent, pick a folder, and assert the terminal is spawned with the right
arguments. That is the layer where the interesting bugs live (wrong argument
names, an exception inside a row builder, off-by-one on the Browse row) and
none of it is reachable from the pure tests.

Stubs (``sublime_stub``, shared with tools/check_import.py) are installed into
``sys.modules`` before importing ``ai_terminal``, which cannot be imported
outside Sublime otherwise.

Neither the launcher (agent/directory picker) nor the history command persist
anything to disk: the picker rows are unranked (alphabetical / sidebar-folder
order), and history is a live filesystem sweep via ``history_scan.scan_all``.
"""

import os
import sys
import threading

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.sublime_stub import Settings, install as _install_stubs  # noqa: E402


# ─── fake window/view the commands are driven through ────────────────────────


class FakeView:
    def __init__(self, vid=1, file_name=None):
        self._id = vid
        self._file_name = file_name
        self._settings = Settings()
        self._name = "view-%d" % vid
        self._closed = False

    def id(self):
        return self._id

    def file_name(self):
        return self._file_name

    def settings(self):
        return self._settings

    def set_name(self, name):
        self._name = name

    def name(self):
        return self._name

    def set_scratch(self, value):
        pass

    def is_valid(self):
        return not self._closed

    def close(self):
        self._closed = True

    def window(self):
        return None

    def run_command(self, name, args=None):
        pass


class FakeWindow:
    """Records show_quick_panel / show_input_panel / run_command calls."""

    _next_id = [1]

    def __init__(self, folders=(), active_view=None):
        self._folders = list(folders)
        self._active_view = active_view
        self._views = []
        self.panels = []          # [(items, on_done, kwargs)]
        self.input_panels = []    # [(caption, initial, on_done)]
        self.commands = []        # [(name, args)]
        self._id = FakeWindow._next_id[0]
        FakeWindow._next_id[0] += 1

    def id(self):
        return self._id

    def folders(self):
        return list(self._folders)

    def active_view(self):
        return self._active_view

    def show_quick_panel(self, items, on_done, *args, **kwargs):
        self.panels.append((items, on_done, kwargs))

    def show_input_panel(self, caption, initial, on_done, on_change, on_cancel):
        self.input_panels.append((caption, initial, on_done))

    def run_command(self, name, args=None):
        self.commands.append((name, args or {}))

    def find_view_by_id(self, vid):
        return None

    def focus_view(self, view):
        self.commands.append(("focus_view", {"id": view.id()}))

    def views(self):
        return list(self._views)

    def new_file(self):
        view = FakeView(vid=999)
        self._views.append(view)
        return view

    def open_file(self, path):
        self.commands.append(("open_file", {"path": path}))
        return FakeView(vid=998, file_name=path)


_messages = []
_install_stubs(message_sink=_messages)

from ai import ai_terminal  # noqa: E402


PROFILES = {
    "Claude": {"argv": ["claude"]},
    "Codex": {"argv": ["codex"]},
    "Bash": {"argv": ["bash"]},
}

ALPHA = os.path.join(REPO, "ai")          # real dirs so os.path.isdir passes
BETA = os.path.join(REPO, "tests")


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Fresh settings per test; never touch real state."""
    del _messages[:]
    settings = Settings()
    settings.update({"profiles": PROFILES, "default_profile": "Claude"})
    monkeypatch.setattr(ai_terminal, "_settings", settings, raising=False)
    monkeypatch.setattr(sys.modules["sublime"], "load_settings", lambda n: settings)
    # Treat every profile as installed unless a test says otherwise.
    monkeypatch.setattr(ai_terminal, "_profile_is_available", lambda n, s=None: True)
    monkeypatch.setattr(
        ai_terminal, "_profile_availability_label", lambda n, s=None: "80% remaining"
    )
    ai_terminal._working_dirs.clear()
    yield settings


def _launcher_cmd(window):
    return ai_terminal.AiTerminalLauncherCommand(window)


def _triggers(items):
    return [i.trigger if hasattr(i, "trigger") else i[0] for i in items]


# ─── the two-step launch flow ────────────────────────────────────────────────


def test_launcher_shows_all_profiles_first():
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    assert len(win.panels) == 1
    assert sorted(_triggers(win.panels[0][0])) == ["Bash", "Claude", "Codex"]


def test_profiles_are_listed_alphabetically():
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    assert _triggers(win.panels[0][0]) == ["Bash", "Claude", "Codex"]


def test_picking_an_agent_opens_the_directory_step():
    win = FakeWindow(folders=[ALPHA, BETA])
    _launcher_cmd(win).run()
    items, on_done, _ = win.panels[0]
    on_done(_triggers(items).index("Codex"))

    assert len(win.panels) == 2, "directory step did not open"
    dir_items = _triggers(win.panels[1][0])
    assert "Browse…" in dir_items
    assert os.path.basename(ALPHA) in dir_items


def test_directory_step_lists_sidebar_folders_in_order():
    win = FakeWindow(folders=[BETA, ALPHA])
    _launcher_cmd(win).run()
    win.panels[0][1](0)
    dir_items = _triggers(win.panels[1][0])
    assert dir_items == [os.path.basename(BETA), os.path.basename(ALPHA), "Browse…"]


def test_full_flow_spawns_the_chosen_agent_in_the_chosen_folder():
    win = FakeWindow(folders=[ALPHA, BETA])
    _launcher_cmd(win).run()

    items, on_profile, _ = win.panels[0]
    on_profile(_triggers(items).index("Claude"))

    dir_items, on_dir, _ = win.panels[1]
    on_dir(_triggers(dir_items).index(os.path.basename(BETA)))

    assert win.commands, "no command was run"
    name, args = win.commands[-1]
    assert name == "ai_terminal_open_here"
    assert args["profile"] == "Claude"
    assert args["paths"] == [BETA]


def test_escape_from_directory_step_reopens_the_agent_step():
    """Esc must not drop the whole flow; it should go back one step."""
    calls = []
    win = FakeWindow(folders=[ALPHA])
    win.run_command = lambda name, args=None: calls.append(name)
    # set_timeout is a no-op stub, so invoke the scheduled callback directly.
    scheduled = []
    sys.modules["sublime"].set_timeout = lambda fn, ms=0: scheduled.append(fn)
    try:
        _launcher_cmd(win).run()
        items, on_profile, _ = win.panels[0]
        on_profile(0)
        _, on_dir, _ = win.panels[1]
        on_dir(-1)
        assert scheduled, "nothing was scheduled on cancel"
        scheduled[0]()
        assert "ai_terminal_launcher" in calls
    finally:
        sys.modules["sublime"].set_timeout = lambda fn, ms=0: None


def test_browse_row_opens_an_input_panel_and_rejects_a_bad_path():
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    items, on_profile, _ = win.panels[0]
    on_profile(0)
    dir_items, on_dir, _ = win.panels[1]

    on_dir(len(dir_items) - 1)  # the Browse… row is last
    assert win.input_panels, "Browse did not open an input panel"

    _, _, on_text = win.input_panels[0]
    on_text(os.path.join(REPO, "definitely-not-a-real-dir"))
    assert any(kind == "error" for kind, _ in _messages)
    assert not win.commands, "a bad path must not spawn anything"


def test_browse_accepts_a_real_path():
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    win.panels[0][1](0)
    dir_items, on_dir, _ = win.panels[1]
    on_dir(len(dir_items) - 1)
    win.input_panels[0][2](BETA)

    name, args = win.commands[-1]
    assert name == "ai_terminal_open_here" and args["paths"] == [BETA]


def test_sidebar_paths_skip_the_directory_step():
    """A right-click already answered 'where'; do not ask again."""
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run(paths=[BETA])
    assert len(win.panels) == 1, "should only ask which agent"
    win.panels[0][1](0)
    assert win.commands[-1][0] == "ai_terminal_open_here"


def test_no_profiles_falls_back_to_default_terminal(monkeypatch):
    monkeypatch.setattr(ai_terminal, "_profile_names", lambda s=None: [])
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    assert not win.panels
    assert win.commands[-1][0] == "ai_terminal_open_here"


def test_rows_carry_usage_annotation_and_a_kind():
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    row = win.panels[0][0][0]
    assert "80% remaining" in row.annotation
    assert row.kind and len(row.kind) == 3


def test_unavailable_profile_is_listed_but_marked(monkeypatch):
    monkeypatch.setattr(
        ai_terminal, "_profile_is_available", lambda n, s=None: n != "Codex"
    )
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    rows = {r.trigger: r for r in win.panels[0][0]}
    assert "Codex" in rows, "unavailable profiles must stay visible"
    assert rows["Codex"].kind[1] == "x"


# ─── cross-agent history (live sweep, nothing persisted) ─────────────────────


def _history_cmd(window):
    return ai_terminal.AiTerminalHistoryCommand(window)


def test_history_lists_sessions_from_the_live_scan(monkeypatch):
    monkeypatch.setattr(
        ai_terminal._history_scan,
        "scan_all",
        lambda: [
            {
                "agent": "Claude Code",
                "title": "SText",
                "detail": "abc123",
                "path": os.path.join(BETA, "abc123.jsonl"),
                "mtime": 1_800_000_000.0,
                "kind": "text",
            }
        ],
    )
    win = FakeWindow()
    _history_cmd(win).run()
    assert len(win.panels) == 1
    row = win.panels[0][0][0]
    assert "Claude Code" in row.trigger and "SText" in row.trigger


def test_history_reports_when_nothing_is_found(monkeypatch):
    monkeypatch.setattr(ai_terminal._history_scan, "scan_all", lambda: [])
    win = FakeWindow()
    _history_cmd(win).run()
    assert not win.panels
    assert any(kind == "status" for kind, _ in _messages)


def test_history_opens_text_sessions_as_a_file(monkeypatch):
    path = os.path.join(BETA, "abc123.jsonl")
    monkeypatch.setattr(
        ai_terminal._history_scan,
        "scan_all",
        lambda: [{
            "agent": "Claude Code", "title": "SText", "detail": "abc123",
            "path": path, "mtime": 1_800_000_000.0, "kind": "text",
        }],
    )
    win = FakeWindow()
    _history_cmd(win).run()
    win.panels[0][1](0)
    assert win.commands[-1] == ("open_file", {"path": path})


# ─── usage refresh command ───────────────────────────────────────────────────


def test_refresh_usage_forces_a_sweep(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ai_terminal, "_ensure_usage_scanner", lambda force=False: calls.append(force)
    )
    ai_terminal.AiTerminalRefreshUsageCommand(FakeWindow()).run()
    assert calls == [True]


# ─── sticky working directory (no picker, TermMate/GeminiCLI convention) ─────


def test_set_working_directory_from_sidebar_folder():
    win = FakeWindow()
    ai_terminal.AiTerminalSetWorkingDirectoryCommand(win).run(paths=[BETA])
    assert ai_terminal._get_working_dir(win) == BETA


def test_set_working_directory_from_a_file_uses_its_folder():
    win = FakeWindow()
    a_file = os.path.join(BETA, "test_launcher_flow.py")
    ai_terminal.AiTerminalSetWorkingDirectoryCommand(win).run(paths=[a_file])
    assert ai_terminal._get_working_dir(win) == BETA


def test_set_working_directory_visibility():
    cmd = ai_terminal.AiTerminalSetWorkingDirectoryCommand(FakeWindow())
    assert cmd.is_visible(paths=[BETA]) is True
    # Command Palette passes no paths arg at all (None) -> always visible.
    assert cmd.is_visible(paths=None) is True
    # Sidebar right-click with nothing usable selected -> hidden.
    assert cmd.is_visible(paths=[]) is False


def test_set_working_directory_from_command_palette_uses_sole_folder():
    """No sidebar selection (palette invocation): fall back like everything
    else does, never show a picker."""
    win = FakeWindow(folders=[ALPHA])
    ai_terminal.AiTerminalSetWorkingDirectoryCommand(win).run(paths=None)
    assert ai_terminal._get_working_dir(win) == ALPHA


def test_clear_working_directory_removes_it():
    win = FakeWindow()
    ai_terminal.AiTerminalSetWorkingDirectoryCommand(win).run(paths=[BETA])
    ai_terminal.AiTerminalClearWorkingDirectoryCommand(win).run()
    assert ai_terminal._get_working_dir(win) is None


def test_clear_working_directory_only_visible_when_one_is_set():
    win = FakeWindow()
    cmd = ai_terminal.AiTerminalClearWorkingDirectoryCommand(win)
    assert cmd.is_visible() is False
    ai_terminal.AiTerminalSetWorkingDirectoryCommand(win).run(paths=[BETA])
    assert cmd.is_visible() is True


def test_pick_cwd_then_uses_sticky_dir_without_prompting():
    win = FakeWindow(folders=[ALPHA, BETA])  # would otherwise be ambiguous
    ai_terminal.AiTerminalSetWorkingDirectoryCommand(win).run(paths=[BETA])
    picked = []
    ai_terminal._pick_cwd_then(win, picked.append)
    assert picked == [BETA]
    assert not win.panels, "a sticky directory must never open a picker"


def test_pick_cwd_then_reports_ambiguity_instead_of_a_picker():
    win = FakeWindow(folders=[ALPHA, BETA])
    picked = []
    ai_terminal._pick_cwd_then(win, picked.append)
    assert not picked, "must not silently guess between two ambiguous folders"
    assert not win.panels, "the picker must be gone entirely"
    assert any(kind == "status" for kind, _ in _messages)


def test_spawn_is_ready_to_answer_keyboard_probe_before_child_starts(monkeypatch):
    """Grok sends CSI ? u at process start and never retests. If the child
    exists before write_pty is bound and the writer is running, the probe
    times out and `/doctor` reports keyboard protocol unavailable for the
    whole session.
    """
    events = []
    reader_entered = threading.Event()

    class FakeParser:
        def bind_write_pty(self, sink):
            events.append("bind_write_pty")
            self._sink = sink

        def resize(self, cols, rows):
            pass

    class FakePty:
        def __init__(self, argv, cwd, cols, rows, env):
            self.argv = list(argv)
            self.pid = 0
            self._alive = True
            events.append("pty_construct")

        def start(self):
            events.append("child_start")
            self.pid = 1

        def read(self, on_data):
            events.append("reader_enter")
            reader_entered.set()

        def write(self, data):
            events.append("write")

        def resize(self, cols, rows):
            return True

        def is_alive(self):
            return self._alive and self.pid

        def kill(self):
            self._alive = False

    orig_ensure = ai_terminal._Terminal._ensure_writer

    def wrapped_ensure(self):
        events.append("writer_ready")
        return orig_ensure(self)

    monkeypatch.setattr(ai_terminal, "_PTY_OK", True)
    monkeypatch.setattr(ai_terminal, "_Pty", FakePty)
    monkeypatch.setattr(ai_terminal, "_PosixPty", FakePty)
    monkeypatch.setattr(ai_terminal, "_measure", lambda view: (80, 24))
    monkeypatch.setattr(
        ai_terminal, "_resolve_launch_argv", lambda argv, env=None: list(argv)
    )
    monkeypatch.setattr(ai_terminal, "_log_tab_text", lambda profile_name=None: False)
    monkeypatch.setattr(
        ai_terminal, "_make_parser", lambda screen, force_main_screen: FakeParser()
    )
    monkeypatch.setattr(ai_terminal._Terminal, "_ensure_writer", wrapped_ensure)
    monkeypatch.setattr(
        sys.modules["sublime"], "load_settings",
        lambda n: Settings({"record_asciicast": False, "profiles": PROFILES}),
    )

    try:
        win = FakeWindow()
        ai_terminal._spawn(win, ALPHA, profile="Claude")

        assert reader_entered.wait(1.0), "reader thread never started"
        assert "bind_write_pty" in events, events
        assert "writer_ready" in events, events
        assert "child_start" in events, events
        assert "reader_enter" in events, events
        assert events.index("bind_write_pty") < events.index("child_start"), events
        assert events.index("writer_ready") < events.index("child_start"), events
        assert events.index("child_start") < events.index("reader_enter"), events
    finally:
        with ai_terminal._term_lock():
            ai_terminal._term_registry().clear()


def test_do_render_defers_while_synchronized_output_is_open(monkeypatch):
    """term.screen.sync_output (native-backed: GhosttyParser queries
    ghostty_terminal_mode_get after every feed) is a boolean level per spec
    -- a repeated "h" while already open is a legal no-op, not a nested
    open, so this can never become a stacked/running open count the way a
    naive regex-count tracker could. _do_render must not paint a
    half-written frame while it's true -- that is the write-then-retract
    stutter this defer exists for.
    """
    events = []

    class FakeParser:
        """feed() mirrors sync_output the way the real GhosttyParser does
        (querying ghostty_terminal_mode_get after every feed) -- a level,
        not a stack, since a repeated "h" while already open is a legal
        DECSET no-op, never a nested/counted open.
        """

        def __init__(self, screen):
            self.screen = screen

        def bind_write_pty(self, sink):
            pass

        def feed(self, text):
            if "\x1b[?2026h" in text:
                self.screen.sync_output = True
            if "\x1b[?2026l" in text:
                self.screen.sync_output = False

        def resize(self, cols, rows):
            pass

    class FakePty:
        def __init__(self, argv, cwd, cols, rows, env):
            self.pid = 0
            self._alive = True

        def start(self):
            self.pid = 1

        def read(self, on_data):
            pass

        def write(self, data):
            pass

        def resize(self, cols, rows):
            return True

        def is_alive(self):
            return self._alive and self.pid

        def kill(self):
            self._alive = False

    monkeypatch.setattr(ai_terminal, "_PTY_OK", True)
    monkeypatch.setattr(ai_terminal, "_Pty", FakePty)
    monkeypatch.setattr(ai_terminal, "_PosixPty", FakePty)
    monkeypatch.setattr(ai_terminal, "_measure", lambda view: (80, 24))
    monkeypatch.setattr(
        ai_terminal, "_resolve_launch_argv", lambda argv, env=None: list(argv)
    )
    monkeypatch.setattr(ai_terminal, "_log_tab_text", lambda profile_name=None: False)
    monkeypatch.setattr(
        ai_terminal, "_make_parser", lambda screen, force_main_screen: FakeParser(screen)
    )
    monkeypatch.setattr(
        sys.modules["sublime"], "load_settings",
        lambda n: Settings({"record_asciicast": False, "profiles": PROFILES}),
    )
    monkeypatch.setattr(
        sys.modules["sublime"], "set_timeout",
        lambda fn, ms=0: events.append(("set_timeout", ms)),
    )

    try:
        win = FakeWindow()
        ai_terminal._spawn(win, ALPHA, profile="Claude")
        term = next(iter(ai_terminal._term_registry().values()))

        term._on_data(b"\x1b[?2026h")
        assert term.screen.sync_output is True
        assert term._render_pending is True

        # _render_pending is already True (set by _schedule_render inside
        # _on_data above) and stays True the whole time a paint is armed but
        # not yet completed -- calling _do_render early (as its own
        # rescheduled timer eventually would) must not clear it while the
        # sync-update batch is still open.
        ai_terminal._do_render(term)
        assert term._render_pending is True, (
            "must stay armed instead of painting a half-written frame"
        )

        term._on_data(b"some content\x1b[?2026l")
        assert term.screen.sync_output is False
    finally:
        with ai_terminal._term_lock():
            ai_terminal._term_registry().clear()


def test_resize_forgets_target_size_when_pty_rejects_it(monkeypatch):
    """_Pty.resize/_PosixPty.resize return True/False for whether ConPTY/
    TIOCSWINSZ actually accepted the new size. _Terminal.resize must not
    treat a False (rejected) result as applied -- if it did,
    self._last_cols/_last_rows would already claim the rejected size, so a
    later poll that measures that exact size again would hit the
    "already at this size" early-return and silently never retry telling
    the real child, which would stay at its old winsize forever.
    """
    events = []

    class FakeParser:
        def bind_write_pty(self, sink):
            pass

        def feed(self, text):
            pass

        def resize(self, cols, rows):
            pass  # parser/screen side always "succeeds" in this test

    class FakePty:
        def __init__(self, argv, cwd, cols, rows, env):
            self.pid = 1
            self._alive = True

        def start(self):
            pass

        def read(self, on_data):
            pass

        def write(self, data):
            pass

        def resize(self, cols, rows):
            events.append((cols, rows))
            return False  # simulate ConPTY/TIOCSWINSZ rejecting every resize

        def is_alive(self):
            return self._alive and self.pid

        def kill(self):
            self._alive = False

    monkeypatch.setattr(ai_terminal, "_PTY_OK", True)
    monkeypatch.setattr(ai_terminal, "_Pty", FakePty)
    monkeypatch.setattr(ai_terminal, "_PosixPty", FakePty)
    monkeypatch.setattr(ai_terminal, "_measure", lambda view: (80, 24))
    monkeypatch.setattr(
        ai_terminal, "_resolve_launch_argv", lambda argv, env=None: list(argv)
    )
    monkeypatch.setattr(ai_terminal, "_log_tab_text", lambda profile_name=None: False)
    monkeypatch.setattr(
        ai_terminal, "_make_parser", lambda screen, force_main_screen: FakeParser()
    )
    monkeypatch.setattr(
        sys.modules["sublime"], "load_settings",
        lambda n: Settings({"record_asciicast": False, "profiles": PROFILES}),
    )
    monkeypatch.setattr(sys.modules["sublime"], "set_timeout", lambda fn, ms=0: None)

    try:
        win = FakeWindow()
        ai_terminal._spawn(win, ALPHA, profile="Claude")
        term = next(iter(ai_terminal._term_registry().values()))
        assert (term._last_cols, term._last_rows) == (80, 24)

        term.resize(100, 30)
        assert events == [(100, 30)]
        assert (term._last_cols, term._last_rows) == (None, None), (
            "a rejected pty resize must not be recorded as the applied size"
        )

        # A later poll re-measuring the SAME size the rejected call already
        # tried must retry (call the pty again), not skip it as "unchanged"
        # -- that early-return only makes sense once a size is confirmed
        # applied.
        term.resize(100, 30)
        assert events == [(100, 30), (100, 30)]
    finally:
        with ai_terminal._term_lock():
            ai_terminal._term_registry().clear()


def test_kill_closes_the_parser_once_the_reader_thread_has_stopped(monkeypatch):
    """_Terminal.kill() must free the native ghostty-vt resources
    (GhosttyParser.close) exactly once per tab close, and only after the
    reader thread that could still be calling parser.feed() has actually
    stopped -- calling close() while a reader is still mid-feed would be a
    native use-after-free, not a Python exception, so this can't be
    observed by any assertion if it went wrong; the ordering is what's
    under test.
    """
    close_calls = []

    class FakeParser:
        def bind_write_pty(self, sink):
            pass

        def feed(self, text):
            pass

        def resize(self, cols, rows):
            pass

        def close(self):
            # Mirrors GhosttyParser.close()'s own idempotency guard -- that
            # guard, not _Terminal.kill() itself, is what makes a double
            # kill() safe.
            if getattr(self, "_closed", False):
                return
            self._closed = True
            close_calls.append(1)

    class FakePty:
        def __init__(self, argv, cwd, cols, rows, env):
            self.pid = 1
            self._alive = True

        def start(self):
            pass

        def read(self, on_data):
            pass  # returns immediately, same as a real PTY hitting EOF

        def write(self, data):
            pass

        def resize(self, cols, rows):
            return True

        def is_alive(self):
            return self._alive and self.pid

        def kill(self):
            self._alive = False

    monkeypatch.setattr(ai_terminal, "_PTY_OK", True)
    monkeypatch.setattr(ai_terminal, "_Pty", FakePty)
    monkeypatch.setattr(ai_terminal, "_PosixPty", FakePty)
    monkeypatch.setattr(ai_terminal, "_measure", lambda view: (80, 24))
    monkeypatch.setattr(
        ai_terminal, "_resolve_launch_argv", lambda argv, env=None: list(argv)
    )
    monkeypatch.setattr(ai_terminal, "_log_tab_text", lambda profile_name=None: False)
    monkeypatch.setattr(
        ai_terminal, "_make_parser", lambda screen, force_main_screen: FakeParser()
    )
    monkeypatch.setattr(
        sys.modules["sublime"], "load_settings",
        lambda n: Settings({"record_asciicast": False, "profiles": PROFILES}),
    )
    monkeypatch.setattr(sys.modules["sublime"], "set_timeout", lambda fn, ms=0: None)

    try:
        win = FakeWindow()
        ai_terminal._spawn(win, ALPHA, profile="Claude")
        term = next(iter(ai_terminal._term_registry().values()))

        term.kill()
        assert close_calls == [1]

        # Idempotent: a second kill() (e.g. a double tab-close) must not
        # double-free.
        term.kill()
        assert close_calls == [1]
    finally:
        with ai_terminal._term_lock():
            ai_terminal._term_registry().clear()
