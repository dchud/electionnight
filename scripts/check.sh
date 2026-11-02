#!/usr/bin/env bash
# Run before every push. CI runs the same script.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run ruff format --check .
uv run ruff check .
uv run mypy
mkdir -p tmp
uv run pytest
uv run python scripts/lint_process_labels.py
NO_MKDOCS_2_WARNING=1 uv run mkdocs build --strict --quiet
echo "check.sh passed"
