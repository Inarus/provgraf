#!/usr/bin/env bash
# Applies new init/NN_*.sql files to provgraf-pg via the _migrations table + an md5 checksum.
# After a schema refactor: add a NEW NN_*.sql with ALTER (immutable-after-deploy), do not edit the old one.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
INIT_DIR="infra/postgres/init"
PSQL() { docker exec -i provgraf-pg psql -U provgraf -d provgraf "$@"; }
md5of() { md5 -q "$1" 2>/dev/null || md5sum "$1" | cut -d' ' -f1; }

PSQL >/dev/null <<'SQL'
CREATE TABLE IF NOT EXISTS _migrations (
  filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now(), checksum TEXT NOT NULL);
SQL

shopt -s nullglob
for f in "$INIT_DIR"/*.sql; do
  fname=$(basename "$f"); cs=$(md5of "$f")
  existing=$(PSQL -At -c "SELECT checksum FROM _migrations WHERE filename='$fname'" 2>/dev/null || echo "")
  if [ "$existing" = "$cs" ]; then echo "SKIP $fname"; continue; fi
  echo "APPLY $fname"
  PSQL -v ON_ERROR_STOP=1 < "$f"
  PSQL >/dev/null -c "INSERT INTO _migrations(filename,checksum) VALUES('$fname','$cs')
    ON CONFLICT(filename) DO UPDATE SET checksum=EXCLUDED.checksum, applied_at=now()"
done
echo "✓ migrate done"
