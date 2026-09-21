"""Measure how well ctypes code is documented (rule 5: every ctypes line has a proper name and a stated purpose).

Method (a heuristic, stated so the numbers can be judged): a "ctypes line" is a code line that uses ctypes names.
It counts as "documented" when it carries a # comment, or the previous non-blank line is a comment, or it sits inside a
function/class that has a docstring. Prints one row per file that imports ctypes.
"""
import ast
import os
import re
import sys

REPO = sys.argv[1]
CT = re.compile(r"\bctypes\b|\bc_[a-z_0-9]+\b|\bbyref\b|\bPOINTER\b|\bStructure\b|\bWINFUNCTYPE\b|\bCFUNCTYPE\b|\bwindll\b|\bWinDLL\b|\bCDLL\b|\b_fields_\b|\bwintypes\b")

rows = []
for folder, dirs, files in os.walk(REPO):
    dirs[:] = [d for d in dirs if d not in (".git", ".tmp", "__pycache__")]
    for name in files:
        if not name.endswith(".py"):
            continue
        path = os.path.join(folder, name)
        with open(path, encoding="utf8", errors="replace") as handle:
            text = handle.read()
        if "import ctypes" not in text and "from ctypes" not in text:
            continue
        lines = text.split("\n")
        tree = ast.parse(text)
        doc_ranges = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and ast.get_docstring(node):
                doc_ranges.append((node.lineno, node.end_lineno))
        string_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.end_lineno > node.lineno:
                string_lines.update(range(node.lineno, node.end_lineno + 1))
        total = documented = 0
        undocumented_defs = {}
        for number, line in enumerate(lines, 1):
            code = line.split("#")[0]
            if number in string_lines or not CT.search(code):
                continue
            total += 1
            previous = next((l.strip() for l in reversed(lines[:number - 1]) if l.strip()), "")
            in_documented_def = any(lo <= number <= hi for lo, hi in doc_ranges)
            if "#" in line or previous.startswith("#") or in_documented_def:
                documented += 1
            else:
                undocumented_defs.setdefault(number, line.strip()[:70])
        rows.append((os.path.relpath(path, REPO), total, documented))

print("%-38s %10s %12s %6s" % ("file", "ctypes lines", "documented", "%"))
for rel, total, documented in sorted(rows, key=lambda r: -r[1]):
    print("%-38s %10d %12d %5.0f%%" % (rel, total, documented, 100.0 * documented / total if total else 100))
