#!/usr/bin/env bash
# ONE command to start provgraf: Docker -> Postgres -> dashboard.
# Usage:  bash start.sh        (run from the repository root)
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"

if ! docker info >/dev/null 2>&1; then
  echo "▸ Starting Docker Desktop…"
  open -a Docker
  for _ in $(seq 1 40); do docker info >/dev/null 2>&1 && break; sleep 3; done
fi

echo "▸ Postgres (provgraf-pg)…"
docker compose -f infra/postgres/docker-compose.yml up -d --wait
uv run provgraf init >/dev/null

echo "▸ Dashboard -> http://localhost:8501  (Ctrl+C to stop)"
uv run --group dashboard streamlit run dashboard/app.py \
  --server.headless=true --browser.gatherUsageStats=false
