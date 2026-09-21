"""No usage, quota or credential-scanning code may return to GhostShell (owner's decision, 2026-09-21).

omp reports usage and quota properly. GhostShell shows none of it: no percent remaining, no
"resets" text, no exhausted marker, and it never reads another program's credentials.
"""
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKIP_DIRS = {".git", ".tmp", "__pycache__", "tests", "docs", "dist"}

_BANNED = re.compile(
    r"usage_scan|gather_usage|oauth/usage|wham/usage|"
    r"%\s*remaining|%\s*left|quota exhausted|no usage data|"
    r"_observed_usage|_record_profile_usage|_profile_is_exhausted|usage_update_from_text|"
    r"\|\s*resets\b|resets\s+(?:in|at|on)\s",
    re.IGNORECASE,
)


def _python_files():
    for folder, dirs, files in os.walk(_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(folder, name)


class NoUsageCodeTests(unittest.TestCase):
    def test_no_usage_or_quota_code_in_the_package(self):
        offenders = []
        for path in _python_files():
            with open(path, encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, 1):
                    if _BANNED.search(line):
                        offenders.append("%s:%d: %s" % (os.path.relpath(path, _ROOT), number, line.strip()[:90]))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
