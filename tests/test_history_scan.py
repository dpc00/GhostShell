"""Unit tests for terminal/history_scan.py (agent history sweep).

Every test builds a throwaway fake home/%LOCALAPPDATA% tree, so nothing
touches the real machine's agent history.

Run from repo root:
    python -m unittest tests.test_history_scan -v
"""
import calendar
import os
import sqlite3
import tempfile
import unittest

from terminal.history_scan import (
    _mtime,
    _parse_iso_to_epoch,
    scan_all,
    scan_ollama,
    scan_t3,
)


def _touch(path, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{}\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class MtimeTests(unittest.TestCase):
    def test_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _touch(os.path.join(tmp, "f.jsonl"), mtime=1700000000)
            self.assertEqual(_mtime(path), 1700000000)

    def test_missing_file_is_zero(self):
        self.assertEqual(_mtime(os.path.join(tempfile.gettempdir(), "nope-xyz")), 0.0)


class ParseIsoTests(unittest.TestCase):
    def test_zulu_with_fraction(self):
        self.assertEqual(
            _parse_iso_to_epoch("2026-08-02T03:04:05.678Z"),
            calendar.timegm((2026, 8, 2, 3, 4, 5, 0, 0, 0)),
        )

    def test_zulu_without_fraction(self):
        self.assertEqual(
            _parse_iso_to_epoch("2026-08-02T03:04:05Z"),
            calendar.timegm((2026, 8, 2, 3, 4, 5, 0, 0, 0)),
        )

    def test_space_separated(self):
        self.assertEqual(
            _parse_iso_to_epoch("2026-08-02 03:04:05"),
            calendar.timegm((2026, 8, 2, 3, 4, 5, 0, 0, 0)),
        )

    def test_unparseable_uses_fallback(self):
        self.assertEqual(_parse_iso_to_epoch("yesterday", fallback=7.0), 7.0)
        self.assertEqual(_parse_iso_to_epoch("", fallback=7.0), 7.0)
        self.assertEqual(_parse_iso_to_epoch(None, fallback=7.0), 7.0)


class GlobSourceTests(unittest.TestCase):
    def test_claude_rows_label_project_and_session(self):
        with tempfile.TemporaryDirectory() as home:
            _touch(
                os.path.join(home, ".claude", "projects", "my-proj", "abc123.jsonl"),
                mtime=1700000000,
            )
            rows = [s for s in scan_all(home=home, localappdata="") if s["agent"] == "Claude Code"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["title"], "my-proj")
            self.assertEqual(rows[0]["detail"], "abc123")
            self.assertEqual(rows[0]["kind"], "text")
            self.assertEqual(rows[0]["mtime"], 1700000000)

    def test_codex_rows_use_filename(self):
        with tempfile.TemporaryDirectory() as home:
            _touch(
                os.path.join(
                    home, ".codex", "sessions", "2026", "08", "02", "rollout-x.jsonl"
                )
            )
            rows = [s for s in scan_all(home=home, localappdata="") if s["agent"] == "Codex"]
            self.assertEqual([r["title"] for r in rows], ["rollout-x.jsonl"])
            self.assertEqual(rows[0]["detail"], "")

    def test_antigravity_variants_are_separate_agents(self):
        with tempfile.TemporaryDirectory() as home:
            _touch(
                os.path.join(
                    home, ".gemini", "antigravity", "conversations", "one.db"
                )
            )
            _touch(
                os.path.join(
                    home, ".gemini", "antigravity-cli", "conversations", "two.db"
                )
            )
            agents = {s["agent"]: s for s in scan_all(home=home, localappdata="")}
            self.assertEqual(agents["Gemini (antigravity)"]["title"], "one")
            self.assertEqual(agents["Gemini (antigravity-cli)"]["title"], "two")
            self.assertEqual(agents["Gemini (antigravity)"]["kind"], "sqlite")

    def test_empty_home_yields_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(scan_all(home=home, localappdata=""), [])


class OllamaScanTests(unittest.TestCase):
    def _db(self, localappdata, rows):
        path = os.path.join(localappdata, "Ollama", "db.sqlite")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path)
        with conn:
            conn.execute("CREATE TABLE chats (id TEXT, title TEXT, created_at TEXT)")
            conn.executemany("INSERT INTO chats VALUES (?, ?, ?)", rows)
        conn.close()
        return path

    def test_rows_map_to_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db(tmp, [("c1", "Hello there", "1700000500")])
            (session,) = scan_ollama(tmp)
            self.assertEqual(session["agent"], "Ollama")
            self.assertEqual(session["title"], "Hello there")
            self.assertEqual(session["detail"], "c1")
            self.assertEqual(session["path"], db_path)
            self.assertEqual(session["mtime"], 1700000500.0)

    def test_untitled_and_unparseable_timestamp_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db(tmp, [("c2", None, "not-a-number")])
            (session,) = scan_ollama(tmp)
            self.assertEqual(session["title"], "(untitled chat)")
            self.assertEqual(session["mtime"], _mtime(db_path))

    def test_missing_db_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(scan_ollama(tmp), [])

    def test_unreadable_db_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Ollama", "db.sqlite")
            _touch(path)  # valid file, not a sqlite database
            self.assertEqual(scan_ollama(tmp), [])


class T3ScanTests(unittest.TestCase):
    def _db(self, home, threads, sessions=()):
        path = os.path.join(home, ".t3", "userdata", "state.sqlite")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path)
        with conn:
            conn.execute(
                "CREATE TABLE projection_threads "
                "(thread_id TEXT, title TEXT, updated_at TEXT, deleted_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE projection_thread_sessions "
                "(thread_id TEXT, provider_name TEXT)"
            )
            conn.executemany(
                "INSERT INTO projection_threads VALUES (?, ?, ?, ?)", threads
            )
            conn.executemany(
                "INSERT INTO projection_thread_sessions VALUES (?, ?)", sessions
            )
        conn.close()
        return path

    def test_provider_name_in_agent_label(self):
        with tempfile.TemporaryDirectory() as home:
            self._db(
                home,
                [("t1", "Refactor", "2026-08-02T03:04:05Z", None)],
                [("t1", "anthropic")],
            )
            (session,) = scan_t3(home)
            self.assertEqual(session["agent"], "T3 (anthropic)")
            self.assertEqual(session["detail"], "t1")
            self.assertEqual(
                session["mtime"], calendar.timegm((2026, 8, 2, 3, 4, 5, 0, 0, 0))
            )

    def test_no_provider_and_no_title(self):
        with tempfile.TemporaryDirectory() as home:
            self._db(home, [("t2", None, "2026-08-02T03:04:05Z", None)])
            (session,) = scan_t3(home)
            self.assertEqual(session["agent"], "T3")
            self.assertEqual(session["title"], "(untitled thread)")

    def test_deleted_threads_are_skipped(self):
        with tempfile.TemporaryDirectory() as home:
            self._db(
                home,
                [
                    ("t3", "kept", "2026-08-02T03:04:05Z", None),
                    ("t4", "gone", "2026-08-02T03:04:05Z", "2026-08-03T00:00:00Z"),
                ],
            )
            self.assertEqual([s["title"] for s in scan_t3(home)], ["kept"])

    def test_missing_and_unreadable_db(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(scan_t3(home), [])
            _touch(os.path.join(home, ".t3", "userdata", "state.sqlite"))
            self.assertEqual(scan_t3(home), [])


class ScanAllTests(unittest.TestCase):
    def test_merges_sources_newest_first_and_caps(self):
        with tempfile.TemporaryDirectory() as home:
            _touch(
                os.path.join(home, ".claude", "projects", "p", "old.jsonl"),
                mtime=1000,
            )
            _touch(
                os.path.join(home, ".claude", "projects", "p", "new.jsonl"),
                mtime=2000,
            )
            _touch(
                os.path.join(
                    home, ".codex", "sessions", "2026", "08", "02", "mid.jsonl"
                ),
                mtime=1500,
            )
            merged = scan_all(home=home, localappdata="")
            self.assertEqual(
                [s["mtime"] for s in merged], [2000.0, 1500.0, 1000.0]
            )
            self.assertEqual(len(scan_all(home=home, localappdata="", limit=2)), 2)

    def test_localappdata_sources_included(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as lad:
            path = os.path.join(lad, "Ollama", "db.sqlite")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            conn = sqlite3.connect(path)
            with conn:
                conn.execute(
                    "CREATE TABLE chats (id TEXT, title TEXT, created_at TEXT)"
                )
                conn.execute("INSERT INTO chats VALUES ('c1', 'chat', '3000')")
            conn.close()
            agents = {s["agent"] for s in scan_all(home=home, localappdata=lad)}
            self.assertEqual(agents, {"Ollama"})


if __name__ == "__main__":
    unittest.main()
