#!/usr/bin/env bash
# Stop the running pipeline cleanly, then start it detached and kept awake.
set -euo pipefail
cd "$(dirname "$0")/.."

lock=data/writer.lock
if [[ -s "$lock" ]]; then
  pid=$(cat "$lock")
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid"
    for _ in $(seq 1 60); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "pipeline $pid did not stop within 60 s" >&2
      exit 1
    fi
  fi
fi

mkdir -p logs
nohup caffeinate -is uv run honcho start pipeline >> logs/honcho.out 2>&1 &
disown
echo "pipeline starting; see logs/pipeline.jsonl"
