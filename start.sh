#!/bin/bash
# AgentOS Mission Control — Launcher
cd "$(dirname "$0")"
echo "Starting AgentOS Mission Control on http://0.0.0.0:51763"
exec python3 server.py
