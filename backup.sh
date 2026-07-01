#!/bin/bash
# Backup Protocol — run before any change to index.html or server.py
set -euo pipefail
DIR="$(dirname "$0")"
BACKUP_DIR="$DIR/backups"
VERSION="v1.0"
TS=$(date +%Y-%m-%dT%H-%M)
mkdir -p "$BACKUP_DIR"
cp "$DIR/index.html" "$BACKUP_DIR/index_${VERSION}_${TS}.html"
cp "$DIR/server.py" "$BACKUP_DIR/server_${VERSION}_${TS}.py"
echo "Backed up: index_${VERSION}_${TS}.html, server_${VERSION}_${TS}.py"
