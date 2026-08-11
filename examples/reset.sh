#!/usr/bin/env bash
# Clean database reset for the DEMO + re-seed.
#
# SAFETY: this connects through DATABASE_URL (the same database the CLI uses) and REFUSES to
# wipe anything that is not the demo. An earlier version shelled into a hardcoded container
# name, which happily truncated a different, real database that happened to run under that
# name — exactly the kind of accident this project exists to prevent.
set -euo pipefail
cd "$(dirname "$0")/.."
DEMO_OWNER=acme-housing

uv run python - "$DEMO_OWNER" <<'PY'
import asyncio, sys
from provgraf import db
from provgraf.config import Settings

demo_owner = sys.argv[1]

async def main():
    pool = await db.create_pool(Settings().database_url)
    async with pool.acquire() as conn:
        foreign = await conn.fetch(
            "SELECT DISTINCT owner FROM entity WHERE owner IS NOT NULL AND owner <> $1",
            demo_owner,
        )
        if foreign:
            owners = ", ".join(r["owner"] for r in foreign)
            sys.exit(
                f"REFUSING to reset: this database holds non-demo data (owners: {owners}).\n"
                f"Point DATABASE_URL at a throwaway database before running the demo."
            )
        # explicit table list, never TRUNCATE ... CASCADE
        await conn.execute(
            "TRUNCATE agent, activity, entity, relation, activity_used RESTART IDENTITY"
        )
    await pool.close()

asyncio.run(main())
PY

bash examples/seed_demo.sh >/dev/null
echo "✓ reset + demo seed (state before the change)"
