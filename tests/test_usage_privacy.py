"""No real credentials or provider requests are used by these checks."""
import sys
import threading

import pytest

from tests.sublime_stub import Settings, install

install()
import ai_terminal
from terminal import usage_scan


@pytest.fixture
def scanner_state(monkeypatch):
    settings = Settings({"usage_scan_enabled": False})
    monkeypatch.setattr(ai_terminal, "_settings", settings)
    monkeypatch.setattr(ai_terminal, "_usage_refresh_token", None)
    for name, value in {
        "_stext_ai_usage_scan_thread": None,
        "_stext_ai_usage_scan_cancel": None,
        "_stext_ai_usage_scan_lock": threading.Lock(),
        "_stext_ai_profile_scan": {},
        "_stext_ai_profile_scan_at": None,
        "_stext_ai_usage_scan_error": None,
    }.items():
        monkeypatch.setattr(sys, name, value, raising=False)
    return settings


@pytest.mark.parametrize("value", [False, None, "false", "true", 0, 1])
def test_disabled_or_invalid_switch_never_starts_a_scan(scanner_state, monkeypatch, value):
    scanner_state["usage_scan_enabled"] = value

    def forbidden(*args, **kwargs):
        pytest.fail("disabled scans must not create workers or read credentials")

    monkeypatch.setattr(ai_terminal.threading, "Thread", forbidden)
    monkeypatch.setattr(ai_terminal, "_gather_usage", forbidden)
    ai_terminal._ensure_usage_scanner()
    ai_terminal._ensure_usage_scanner(force=True)
    assert ai_terminal._usage_refresh_interval_ms() == 0


def test_manual_refresh_explains_disabled_state(scanner_state, monkeypatch):
    calls, messages = [], []
    monkeypatch.setattr(ai_terminal, "_ensure_usage_scanner", lambda **kw: calls.append(kw))
    monkeypatch.setattr(ai_terminal.sublime, "status_message", messages.append)
    ai_terminal.AiTerminalRefreshUsageCommand(object()).run()
    assert calls == []
    assert len(messages) == 1 and "usage_scan_enabled" in messages[0]
    assert "disabled" in messages[0]


def test_disabled_timer_is_cancelled_and_cannot_rearm(scanner_state, monkeypatch):
    cancelled, scheduled = [], []
    monkeypatch.setattr(ai_terminal, "_usage_refresh_token", "old-timer")
    monkeypatch.setattr(ai_terminal.sublime, "cancel_timeout", cancelled.append)
    monkeypatch.setattr(ai_terminal.sublime, "set_timeout", lambda *a: scheduled.append(a))
    ai_terminal._start_usage_refresh()
    ai_terminal._usage_refresh_tick()  # A callback already queued before disable.
    assert cancelled == ["old-timer"]
    assert scheduled == []
    assert ai_terminal._usage_refresh_token is None


def test_enabled_scan_publishes_and_force_refreshes(scanner_state, monkeypatch):
    scanner_state["usage_scan_enabled"] = True
    calls = []

    def gather(should_cancel):
        calls.append(should_cancel())
        return {"codex": {"summary": "available"}}

    monkeypatch.setattr(ai_terminal, "_gather_usage", gather)
    ai_terminal._ensure_usage_scanner()
    sys._stext_ai_usage_scan_thread.join(timeout=2)
    assert sys._stext_ai_profile_scan == {"codex": {"summary": "available"}}
    ai_terminal._ensure_usage_scanner()
    assert calls == [False]  # Existing result is reused.
    ai_terminal._ensure_usage_scanner(force=True)
    sys._stext_ai_usage_scan_thread.join(timeout=2)
    assert calls == [False, False]


def test_disabling_active_scan_discards_results(scanner_state, monkeypatch):
    scanner_state["usage_scan_enabled"] = True
    entered, release = threading.Event(), threading.Event()
    cancellation_seen = []

    def gather(should_cancel):
        entered.set()
        assert release.wait(timeout=2)
        cancellation_seen.append(should_cancel())
        return {"codex": {"summary": "must not be published"}}

    monkeypatch.setattr(ai_terminal, "_gather_usage", gather)
    ai_terminal._ensure_usage_scanner(force=True)
    worker = sys._stext_ai_usage_scan_thread
    try:
        assert entered.wait(timeout=2)
        scanner_state["usage_scan_enabled"] = False
        ai_terminal._start_usage_refresh()  # Settings-change callback path.
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert cancellation_seen == [True]
    assert sys._stext_ai_profile_scan == {}
    assert sys._stext_ai_profile_scan_at is None
    assert ai_terminal._scanned_usage_for_profile("Codex") is None


def test_reenabled_scanner_can_refresh_after_cancellation(scanner_state, monkeypatch):
    ai_terminal._stop_usage_scanner()
    scanner_state["usage_scan_enabled"] = True
    calls = []

    def gather(should_cancel):
        calls.append(should_cancel())
        return {}

    monkeypatch.setattr(ai_terminal, "_gather_usage", gather)
    ai_terminal._ensure_usage_scanner(force=True)
    sys._stext_ai_usage_scan_thread.join(timeout=2)
    assert calls == [False]
    assert sys._stext_ai_profile_scan_at is not None


def test_gather_cancelled_before_start_does_not_read_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("cancelled provider must not access disk or network")

    for name in ("fetch_codex_usage", "scan_codex_usage", "fetch_claude_usage",
                 "fetch_ollama_usage", "fetch_kimi_usage", "fetch_openrouter_usage"):
        monkeypatch.setattr(usage_scan, name, forbidden)
    assert usage_scan.gather_usage(should_cancel=lambda: True) == {}


def test_cancellation_finishes_current_provider_but_skips_remaining(monkeypatch):
    cancelled = threading.Event()
    calls = []

    def codex(*args, **kwargs):
        calls.append("codex")
        cancelled.set()
        # The provider is allowed to finish token persistence before returning.
        calls.append("provider finished")
        return {"summary": "partial result"}

    def forbidden(*args, **kwargs):
        pytest.fail("no fallback or subsequent provider may run after cancellation")

    monkeypatch.setattr(usage_scan, "fetch_codex_usage", codex)
    for name in ("scan_codex_usage", "fetch_claude_usage", "fetch_ollama_usage",
                 "fetch_kimi_usage", "fetch_openrouter_usage"):
        monkeypatch.setattr(usage_scan, name, forbidden)
    usage_scan.gather_usage(should_cancel=cancelled.is_set)
    assert calls == ["codex", "provider finished"]
