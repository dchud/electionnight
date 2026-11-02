#!/usr/bin/env python3
"""Fail on process labels in artifacts that outlive the work.

Code, tests, and user-facing docs get read long after the branch that produced
them is gone. A bead ID or a PR number in them points at context that git
history carries more accurately.

Only high-confidence shapes are checked. Bare version numbers and the word
"phase" are not, because of false positives.

Put `process-label-ok` in a comment on the line to exempt it.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {
    ".abacus",
    ".beads",
    ".claude",
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "bin",
    "data",
    "logs",
    "reports",
    "tmp",
    "vendor",
}
EXCLUDED_PATHS = {
    # These define or specify the conventions, so they quote the shapes.
    Path("CLAUDE.md"),
    Path("SPEC.md"),
    Path("agent-approach-brief.md"),
    Path("src/monitor/web/static/css/app.css"),
}
SUFFIXES = {".py", ".html", ".md", ".css", ".js", ".yml", ".yaml", ".toml"}
EXEMPTION = "process-label-ok"

CHECKS = [
    (re.compile(r"\bbd-[a-z0-9]{3,}\b", re.IGNORECASE), "bead ID"),
    (
        re.compile(r"\bPR\s*#\s*\d+|\bPR\d+\b|\bthis PR\b", re.IGNORECASE),
        "PR reference",
    ),
    (
        re.compile(
            r"\b(?:since|as of|flipped in|changed in|default since)\s+"
            r"v?\d+\.\d+",
            re.IGNORECASE,
        ),
        "version-tagged claim",
    ),
    (
        re.compile(
            r"\b(?:pre|post)-(?:PR|release|merge|migration)\b", re.IGNORECASE
        ),
        "stage-relative wording",
    ),
]


def candidate_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in SUFFIXES:
            continue
        rel = path.relative_to(ROOT)
        if EXCLUDED_DIRS & set(rel.parts) or rel in EXCLUDED_PATHS:
            continue
        yield path, rel


def main():
    failures = []
    for path, rel in candidate_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, start=1):
            if EXEMPTION in line:
                continue
            for pattern, label in CHECKS:
                match = pattern.search(line)
                if match:
                    failures.append(
                        f"{rel}:{number}: {label} {match.group(0).strip()!r}"
                    )
    if failures:
        print("Process labels found in persistent artifacts:\n")
        for failure in failures:
            print(f"  {failure}")
        print(f"\nAdd a '{EXEMPTION}' comment on the line to exempt it.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
