#!/bin/bash
# AgentOS Mission Control — Launcher
cd "$(dirname "$0")"
echo "Starting AgentOS Mission Control on http://0.0.0.0:8888"
exec python3 server.py
