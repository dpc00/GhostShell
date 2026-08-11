"""Unit tests for the usage_scan fetchers (HTTP layer stubbed out).

No test here touches the network or the real home directory: every fetcher is
pointed at a temporary credentials tree and ``urllib.request.urlopen`` is
replaced with a fake that returns canned payloads (or raises).

Run from repo root:
    python -m unittest tests.test_usage_endpoints -v
"""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from ai.terminal.usage_scan import (
    _http_json,
    _iso_to_epoch,
    _openrouter_key_from_qwen,
    _persist_kimi_oauth,
    _refresh_claude_token,
    _refresh_kimi_token,
    _tail_lines,
    fetch_claude_usage,
    fetch_codex_usage,
    fetch_kimi_usage,
    fetch_ollama_usage,
    fetch_openrouter_usage,
    gather_usage,
)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _responder(*payloads):
    """urlopen stub: returns each payload in turn, recording the requests.

    A payload may be a dict/list (encoded as JSON), raw bytes, or an
    exception instance to raise.
    """
    remaining = list(payloads)
    requests = []

    def urlopen(request, *_args, **_kwargs):
        requests.append(request)
        payload = remaining.pop(0) if remaining else {}
        if isinstance(payload, BaseException):
            raise payload
        if isinstance(payload, bytes):
            return _FakeResponse(payload)
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    urlopen.requests = requests
    return urlopen


_WHAM_PAYLOAD = {
    "rate_limit": {
        "primary_window": {
            "used_percent": 50,
            "limit_window_seconds": 604800,
            "reset_at": 1785640000,
        },
        "secondary_window": None,
    }
}


class HttpHelperTests(unittest.TestCase):
    def test_http_json_decodes_body(self):
        with mock.patch("urllib.request.urlopen", _responder({"ok": True})):
            self.assertEqual(_http_json("https://x/y", {"Accept": "*/*"}), {"ok": True})

    def test_http_json_sends_headers(self):
        urlopen = _responder({})
        with mock.patch("urllib.request.urlopen", urlopen):
            _http_json("https://x/y", {"Authorization": "Bearer t"})
        self.assertEqual(urlopen.requests[0].get_header("Authorization"), "Bearer t")

    def test_iso_to_epoch(self):
        self.assertEqual(_iso_to_epoch(1785640000), 1785640000)
        self.assertEqual(_iso_to_epoch("1970-01-01T00:00:00Z"), 0.0)
        self.assertIsNone(_iso_to_epoch("whenever"))
        self.assertIsNone(_iso_to_epoch(None))


class TailLinesTests(unittest.TestCase):
    def test_reads_only_the_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rollout.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("first\nsecond\nthird\n")
            self.assertEqual(_tail_lines(path), ["first", "second", "third"])
            self.assertEqual(_tail_lines(path, max_bytes=6), ["third"])

    def test_missing_file_is_empty(self):
        self.assertEqual(_tail_lines(os.path.join(tempfile.gettempdir(), "nope-x")), [])


class CodexFetchTests(unittest.TestCase):
    def _home(self, tmp, auth):
        os.makedirs(os.path.join(tmp, ".codex"), exist_ok=True)
        path = os.path.join(tmp, ".codex", "auth.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(auth if isinstance(auth, str) else json.dumps(auth))
        return os.path.join(tmp, ".codex")

    def test_live_usage_uses_persisted_oauth_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(
                tmp, {"tokens": {"access_token": "tok", "account_id": "acct"}}
            )
            urlopen = _responder(_WHAM_PAYLOAD)
            with mock.patch("urllib.request.urlopen", urlopen):
                usage = fetch_codex_usage(home, now=1785630000)
            self.assertEqual(usage["remaining"], 50.0)
            self.assertEqual(usage["source"], "live")
            request = urlopen.requests[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer tok")
            self.assertEqual(request.get_header("Chatgpt-account-id"), "acct")

    def test_account_id_header_omitted_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp, {"tokens": {"access_token": "tok"}})
            urlopen = _responder(_WHAM_PAYLOAD)
            with mock.patch("urllib.request.urlopen", urlopen):
                fetch_codex_usage(home, now=1785630000)
            self.assertIsNone(urlopen.requests[0].get_header("Chatgpt-account-id"))

    def test_missing_or_malformed_auth_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(fetch_codex_usage(os.path.join(tmp, ".codex")))
            home = self._home(tmp, "not json")
            self.assertIsNone(fetch_codex_usage(home))
            home = self._home(tmp, {"tokens": {}})
            self.assertIsNone(fetch_codex_usage(home))

    def test_transport_failure_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp, {"tokens": {"access_token": "tok"}})
            with mock.patch(
                "urllib.request.urlopen", _responder(urllib.error.URLError("offline"))
            ):
                self.assertIsNone(fetch_codex_usage(home))


_CLAUDE_USAGE = {"five_hour": {"utilization": 25, "resets_at": "2026-08-02T03:00:00Z"}}


class ClaudeFetchTests(unittest.TestCase):
    def _creds(self, tmp, oauth):
        path = os.path.join(tmp, ".credentials.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"claudeAiOauth": oauth}, handle)
        return path

    def test_valid_token_is_used_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._creds(
                tmp, {"accessToken": "tok", "expiresAt": (1785630000 + 3600) * 1000}
            )
            urlopen = _responder(_CLAUDE_USAGE)
            with mock.patch("urllib.request.urlopen", urlopen):
                usage = fetch_claude_usage(tmp, now=1785630000)
            self.assertEqual(usage["remaining"], 75.0)
            self.assertEqual(len(urlopen.requests), 1)
            self.assertEqual(
                urlopen.requests[0].get_header("Anthropic-beta"), "oauth-2025-04-20"
            )

    def test_expired_token_is_refreshed_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._creds(
                tmp,
                {
                    "accessToken": "old",
                    "refreshToken": "refresh",
                    "expiresAt": (1785630000 - 10) * 1000,
                },
            )
            urlopen = _responder(
                {
                    "access_token": "new",
                    "refresh_token": "rotated",
                    "expires_in": 3600,
                    "scope": "user:inference user:profile",
                },
                _CLAUDE_USAGE,
            )
            with mock.patch("urllib.request.urlopen", urlopen):
                usage = fetch_claude_usage(tmp, now=1785630000)
            self.assertEqual(usage["remaining"], 75.0)
            self.assertEqual(urlopen.requests[1].get_header("Authorization"), "Bearer new")
            with open(path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)["claudeAiOauth"]
            self.assertEqual(stored["accessToken"], "new")
            self.assertEqual(stored["refreshToken"], "rotated")
            self.assertEqual(stored["scopes"], ["user:inference", "user:profile"])

    def test_failed_refresh_reports_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._creds(
                tmp,
                {
                    "accessToken": "old",
                    "refreshToken": "refresh",
                    "expiresAt": (1785630000 - 10) * 1000,
                },
            )
            with mock.patch(
                "urllib.request.urlopen", _responder(urllib.error.URLError("nope"))
            ):
                usage = fetch_claude_usage(tmp, now=1785630000)
            self.assertIn("token expired", usage["error"])

    def test_missing_credentials_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(fetch_claude_usage(tmp))

    def test_usage_call_failure_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._creds(tmp, {"accessToken": "tok"})
            with mock.patch(
                "urllib.request.urlopen", _responder(urllib.error.URLError("nope"))
            ):
                self.assertIsNone(fetch_claude_usage(tmp, now=1785630000))


class ClaudeRefreshTests(unittest.TestCase):
    def test_refresh_without_token_is_none(self):
        self.assertIsNone(_refresh_claude_token("/nonexistent", {}))

    def test_rejected_grant_falls_back_to_a_concurrently_written_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".credentials.json")
            fresh = {"accessToken": "winner", "expiresAt": (1 << 41)}
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"claudeAiOauth": fresh}, handle)
            with mock.patch(
                "urllib.request.urlopen", _responder(urllib.error.URLError("409"))
            ):
                result = _refresh_claude_token(path, {"refreshToken": "stale"})
            self.assertEqual(result["accessToken"], "winner")

    def test_rejected_grant_with_no_fresh_token_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".credentials.json")
            with mock.patch(
                "urllib.request.urlopen", _responder(urllib.error.URLError("409"))
            ):
                self.assertIsNone(_refresh_claude_token(path, {"refreshToken": "x"}))

    def test_tokenless_response_is_none(self):
        with mock.patch("urllib.request.urlopen", _responder({"error": "invalid"})):
            self.assertIsNone(
                _refresh_claude_token("/nonexistent", {"refreshToken": "x"})
            )

    def test_form_is_json_encoded_with_the_cli_client_id(self):
        urlopen = _responder({"access_token": "new"})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".credentials.json")
            with mock.patch("urllib.request.urlopen", urlopen):
                _refresh_claude_token(path, {"refreshToken": "r"})
        request = urlopen.requests[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["grant_type"], "refresh_token")
        self.assertEqual(body["refresh_token"], "r")
        self.assertTrue(body["client_id"])
        self.assertEqual(request.get_header("Content-type"), "application/json")


class OllamaFetchTests(unittest.TestCase):
    def test_posts_to_local_server(self):
        urlopen = _responder({"plan": "free", "name": "someone"})
        with mock.patch("urllib.request.urlopen", urlopen):
            usage = fetch_ollama_usage("http://localhost:11434/")
        self.assertEqual(usage["plan"], "free")
        request = urlopen.requests[0]
        self.assertEqual(request.full_url, "http://localhost:11434/api/me")
        self.assertEqual(request.get_method(), "POST")

    def test_server_not_running_is_none(self):
        with mock.patch(
            "urllib.request.urlopen", _responder(urllib.error.URLError("refused"))
        ):
            self.assertIsNone(fetch_ollama_usage())


class KimiFetchTests(unittest.TestCase):
    def _creds(self, tmp, creds):
        path = os.path.join(tmp, "credentials", "kimi-code.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(creds if isinstance(creds, str) else json.dumps(creds))
        return path

    def test_valid_token_queries_the_me_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._creds(tmp, {"access_token": "tok", "expires_at": 1785640000})
            urlopen = _responder({"user_level_name": "Free"})
            with mock.patch("urllib.request.urlopen", urlopen):
                usage = fetch_kimi_usage(tmp, now=1785630000)
            self.assertEqual(usage["plan"], "Free")
            self.assertEqual(
                urlopen.requests[0].full_url, "https://api.kimi.com/coding/v1/me"
            )

    def test_expired_token_is_refreshed_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._creds(
                tmp,
                {
                    "access_token": "old",
                    "refresh_token": "r",
                    "expires_at": 1785630000 - 10,
                },
            )
            urlopen = _responder(
                {"access_token": "new", "refresh_token": "rotated", "expires_in": 3600},
                {"user_level_name": "Pro"},
            )
            with mock.patch("urllib.request.urlopen", urlopen):
                usage = fetch_kimi_usage(tmp, now=1785630000)
            self.assertEqual(usage["plan"], "Pro")
            self.assertEqual(urlopen.requests[1].get_header("Authorization"), "Bearer new")
            with open(path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            self.assertEqual(stored["refresh_token"], "rotated")

    def test_failed_refresh_reports_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._creds(
                tmp, {"access_token": "old", "refresh_token": "r", "expires_at": 0}
            )
            with mock.patch(
                "urllib.request.urlopen", _responder(urllib.error.URLError("nope"))
            ):
                usage = fetch_kimi_usage(tmp, now=1785630000)
            self.assertIn("token expired", usage["error"])

    def test_missing_and_tokenless_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(fetch_kimi_usage(tmp))
            self._creds(tmp, "not json")
            self.assertIsNone(fetch_kimi_usage(tmp))
            self._creds(tmp, {"refresh_token": "r"})
            self.assertIsNone(fetch_kimi_usage(tmp))

    def test_me_call_failure_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._creds(tmp, {"access_token": "tok"})
            with mock.patch(
                "urllib.request.urlopen", _responder(urllib.error.URLError("nope"))
            ):
                self.assertIsNone(fetch_kimi_usage(tmp, now=1785630000))


class KimiRefreshTests(unittest.TestCase):
    def test_grant_is_form_encoded(self):
        urlopen = _responder({"access_token": "new"})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "kimi-code.json")
            with mock.patch("urllib.request.urlopen", urlopen):
                creds = _refresh_kimi_token(path, {"refresh_token": "r"})
        request = urlopen.requests[0]
        self.assertEqual(
            request.get_header("Content-type"), "application/x-www-form-urlencoded"
        )
        self.assertIn(b"grant_type=refresh_token", request.data)
        self.assertEqual(creds["access_token"], "new")

    def test_missing_refresh_token_or_rejected_grant_is_none(self):
        self.assertIsNone(_refresh_kimi_token("/nonexistent", {}))
        with mock.patch(
            "urllib.request.urlopen", _responder(urllib.error.URLError("nope"))
        ):
            self.assertIsNone(_refresh_kimi_token("/nonexistent", {"refresh_token": "r"}))
        with mock.patch("urllib.request.urlopen", _responder({"error": "invalid"})):
            self.assertIsNone(_refresh_kimi_token("/nonexistent", {"refresh_token": "r"}))

    def test_persist_failure_is_swallowed(self):
        # Unwritable directory: the sweep must still finish with its token.
        _persist_kimi_oauth(
            os.path.join(tempfile.gettempdir(), "no-such-dir-xyz", "kimi-code.json"),
            {"access_token": "t"},
        )


class OpenRouterFetchTests(unittest.TestCase):
    def _settings(self, tmp, settings):
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(settings if isinstance(settings, str) else json.dumps(settings))
        return path

    def test_key_is_read_from_qwen_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._settings(tmp, {"env": {"OPENROUTER_API_KEY": "sk-or-abc"}})
            self.assertEqual(_openrouter_key_from_qwen(tmp), "sk-or-abc")

    def test_non_openrouter_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_openrouter_key_from_qwen(tmp))  # missing file
            self._settings(tmp, "not json")
            self.assertIsNone(_openrouter_key_from_qwen(tmp))
            self._settings(tmp, {"env": {"OPENROUTER_API_KEY": "sk-live-abc"}})
            self.assertIsNone(_openrouter_key_from_qwen(tmp))
            self._settings(tmp, {"env": "nope"})
            self.assertIsNone(_openrouter_key_from_qwen(tmp))

    def test_fetch_uses_settings_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._settings(tmp, {"env": {"OPENROUTER_API_KEY": "sk-or-abc"}})
            urlopen = _responder({"data": {"is_free_tier": True, "usage": 1.0}})
            with mock.patch("urllib.request.urlopen", urlopen), mock.patch.dict(
                os.environ, {}, clear=True
            ):
                usage = fetch_openrouter_usage(tmp)
            self.assertIn("free tier", usage["summary"])
            self.assertEqual(urlopen.requests[0].get_header("Authorization"), "Bearer sk-or-abc")

    def test_environment_key_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            urlopen = _responder({"data": {"usage": 2.0}})
            with mock.patch("urllib.request.urlopen", urlopen), mock.patch.dict(
                os.environ, {"OPENROUTER_API_KEY": "sk-env"}
            ):
                usage = fetch_openrouter_usage(tmp)
            self.assertIn("$2.00 total", usage["summary"])

    def test_no_key_skips_the_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            urlopen = _responder({})
            with mock.patch("urllib.request.urlopen", urlopen), mock.patch.dict(
                os.environ, {}, clear=True
            ):
                self.assertIsNone(fetch_openrouter_usage(tmp))
            self.assertEqual(urlopen.requests, [])

    def test_transport_failure_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "urllib.request.urlopen", _responder(urllib.error.URLError("nope"))
            ), mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-env"}):
                self.assertIsNone(fetch_openrouter_usage(tmp))


_CODEX_ROLLOUT_LINE = json.dumps({
    "payload": {
        "rate_limits": {
            "primary": {"used_percent": 10.0, "resets_at": 1785640000},
        }
    }
})


class GatherUsageTests(unittest.TestCase):
    def test_every_provider_is_independent(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".codex"))
            with open(
                os.path.join(home, ".codex", "auth.json"), "w", encoding="utf-8"
            ) as handle:
                json.dump({"tokens": {"access_token": "tok"}}, handle)
            urlopen = _responder(
                _WHAM_PAYLOAD,                              # codex
                urllib.error.URLError("no claude creds"),    # unreached
            )
            with mock.patch("urllib.request.urlopen", urlopen), mock.patch.dict(
                os.environ, {}, clear=True
            ):
                results = gather_usage(home=home, now=1785630000)
            self.assertEqual(list(results), ["codex"])
            self.assertEqual(results["codex"]["source"], "live")

    def test_live_failure_falls_back_to_local_snapshot(self):
        with tempfile.TemporaryDirectory() as home:
            day = os.path.join(home, ".codex", "sessions", "2026", "08", "02")
            os.makedirs(day)
            with open(os.path.join(day, "r.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(_CODEX_ROLLOUT_LINE + "\n")
            with mock.patch(
                "urllib.request.urlopen", _responder(urllib.error.URLError("offline"))
            ), mock.patch.dict(os.environ, {}, clear=True):
                results = gather_usage(home=home, now=1785630000)
            self.assertEqual(results["codex"]["source"], "local")
            self.assertEqual(results["codex"]["remaining"], 90.0)

    def test_nothing_configured_yields_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch(
                "urllib.request.urlopen", _responder(urllib.error.URLError("offline"))
            ), mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(gather_usage(home=home, now=1785630000), {})


if __name__ == "__main__":
    unittest.main()
