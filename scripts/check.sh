#!/usr/bin/env bash
# Run before every push. CI runs the same script.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run python scripts/lint_process_labels.py
echo "check.sh passed"
