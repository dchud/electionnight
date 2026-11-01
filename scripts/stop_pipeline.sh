#!/usr/bin/env bash
# Stop this project's running pipeline cleanly. A lock file keeps the last
# holder's PID after it exits, and the system can reuse that PID, so a process
# is signaled only if it has this lock file open and runs `monitor pipeline`.
set -euo pipefail
cd "$(dirname "$0")/.."

lock="${SCRATCH_DIR:-data}/writer.lock"
[[ -s "$lock" ]] || exit 0
pid=$(cat "$lock")
kill -0 "$pid" 2>/dev/null || exit 0

if ! lsof -a -p "$pid" -- "$lock" >/dev/null 2>&1; then
  echo "process $pid does not hold $lock; not signaling it"
  exit 0
fi
command=$(ps -o command= -p "$pid")
if [[ "$command" != *"monitor pipeline"* ]]; then
  echo "$lock is held by another command, not signaling it: $command" >&2
  exit 1
fi

kill -TERM "$pid"
for _ in $(seq 1 60); do
  kill -0 "$pid" 2>/dev/null || exit 0
  sleep 1
done
echo "pipeline $pid did not stop within 60 s" >&2
exit 1
