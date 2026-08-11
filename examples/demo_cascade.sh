#!/usr/bin/env bash
# Self-contained staleness-cascade demo (repeatable):
# reset+seed → check (before) → change a source → check (after).
set -euo pipefail
cd "$(dirname "$0")/.."
OWNER=acme-housing
PG() { uv run provgraf "$@"; }

echo "═══ 1. RESET + SEED (state before) ═══"
bash examples/reset.sh

echo; echo "═══ 2. CHECK (BEFORE the change) ═══"
PG check

echo; echo "═══ 3. EVENT: a new datasheet changes Riverside units 152 → 154 ═══"
PG add-doc acme:src.datasheet-2026-07 --by acme:office --owner "$OWNER" --date 2026-07-15 \
  --label "Office datasheet 2026-07" --file examples/docs/datasheet-2026-07.md
PG revise acme:riverside.units_phase1 --value 154 --from acme:src.datasheet-2026-07 \
  --by analyst --note "correction after the tender"

echo; echo "═══ 4. CHECK (AFTER) — the cascade flags everything derived from the old value ═══"
echo "Expected HARD-STALE: acme:units_total.phase1 + acme:report.units_phase1 (transitively)."
PG check

echo; echo "═══ 5. BITEMPORAL: two independent time axes ═══"
echo "The rent was recorded now, but holds in the world from 2026-06-01:"
PG get acme:riverside.rent --world-at 2026-06-15
echo "…and asking about a date before it took effect finds no version in force:"
PG get acme:riverside.rent --world-at 2026-05-15 || true
