#!/bin/bash
# AgentOS Mission Control — Launcher
set -euo pipefail

cd "$(dirname "$0")"

echo "Starting AgentOS task worker"
python3 scripts/agentos_task_worker.py >> agentos-task-worker.log 2>&1 &
WORKER_PID=$!
trap 'kill "$WORKER_PID" 2>/dev/null || true' EXIT

echo "Starting AgentOS Mission Control on http://0.0.0.0:8888"
exec python3 server.py
