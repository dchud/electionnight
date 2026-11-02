#!/usr/bin/env bash
# Stop the running pipeline cleanly, then start it detached and kept awake.
set -euo pipefail
cd "$(dirname "$0")/.."

scripts/stop_pipeline.sh

mkdir -p logs
nohup caffeinate -is uv run honcho start pipeline >> logs/honcho.out 2>&1 &
disown
echo "pipeline starting; see logs/pipeline.jsonl"
