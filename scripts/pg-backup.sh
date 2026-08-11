#!/usr/bin/env bash
# pg_dump provgraf-pg -> snapshots/<timestamp>.sql.gz (dated backup; no markdown mirror).
set -euo pipefail
cd "$(dirname "$0")/.."
TS=$(date +"%Y-%m-%dT%H%M")
uv run provgraf snapshot "$TS"
ln -sf "$TS.sql.gz" "snapshots/latest.sql.gz"
echo "✓ backup snapshots/$TS.sql.gz (symlink latest)"
