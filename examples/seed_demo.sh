#!/usr/bin/env bash
# Demo seed — a FICTIONAL social-housing company ("Acme Community Housing Ltd.").
# State BEFORE the source change. Run on a fresh database (after `provgraf init`).
# Then: `provgraf check` (before) → demo_cascade.sh (change) → `provgraf check` (after).
set -euo pipefail
cd "$(dirname "$0")/.."
OWNER=acme-housing
PG() { uv run provgraf "$@"; }

echo "── Agents ──────────────────────────────────────────────"
PG agent analyst      --kind person       --name "Analyst (human curator)"
PG agent claude-code  --kind software     --name "Claude Code"
PG agent acme:office  --kind organization --name "Acme Community Housing — office"
PG agent acme:council --kind organization --name "Riverside Town Council"

echo "── Source documents ────────────────────────────────────"
PG add-doc acme:src.registry     --by analyst      --owner "$OWNER" --label "Company registry extract" \
  --file examples/docs/registry.md
PG add-doc acme:src.datasheet    --by acme:office  --owner "$OWNER" --date 2026-06-22 --label "Office datasheet 2026-06-22" \
  --file examples/docs/datasheet-2026-06.md
PG add-doc acme:src.resolution   --by acme:council --owner "$OWNER" --label "Council resolution 29/226 (deposit rules)" \
  --file examples/docs/resolution-29-226.md
PG add-doc acme:src.rental-terms --by acme:office  --owner "$OWNER" --label "Rental terms (income thresholds)" \
  --file examples/docs/rental-terms.md

echo "── Identity core (eager) ───────────────────────────────"
PG add acme:company      --value "Acme Community Housing Ltd." --from acme:src.registry --type text --load eager --owner "$OWNER" --label "Company"
PG add acme:ceo          --value "Jane Doe" --from acme:src.registry --type text --load eager --owner "$OWNER" --label "CEO (since 2025)"

echo "── Developments (numbers) ──────────────────────────────"
PG add acme:riverside.units_phase1 --value 152 --from acme:src.datasheet --unit "units" --load eager --owner "$OWNER" --label "Riverside phase 1 — units"
PG add acme:riverside.rent         --value 27  --from acme:src.datasheet --unit "EUR/m2/month" --load eager --owner "$OWNER" --label "Riverside — rent" \
  --world-from 2026-06-01
PG add acme:hillside.units_phase1  --value 84  --from acme:src.datasheet --unit "units" --owner "$OWNER" --label "Hillside phase 1"
PG add acme:lakeside.units_phase1  --value 58  --from acme:src.datasheet --unit "units" --owner "$OWNER" --label "Lakeside phase 1"

echo "── Derivations (aggregates, 2 levels) ──────────────────"
PG derive acme:units_total.phase1 --value 294 \
  --from acme:riverside.units_phase1 --from acme:hillside.units_phase1 \
  --from acme:lakeside.units_phase1 \
  --formula "152+84+58" --unit "units" --owner "$OWNER" --label "Total units, phase 1 (3 towns)"
PG derive acme:report.units_phase1 --value 294 \
  --from acme:units_total.phase1 --formula "= phase-1 total" \
  --unit "units" --owner "$OWNER" --label "Report: phase-1 units (level 2)"

echo "── Source conflict: Riverside deposit (6x datasheet vs 2x resolution) ──"
PG add acme:riverside.deposit@datasheet  --value 6 --from acme:src.datasheet  --status disputed --unit "x rent" --owner "$OWNER" --label "Riverside deposit per datasheet"
PG add acme:riverside.deposit@resolution --value 2 --from acme:src.resolution --status disputed --unit "x rent" --owner "$OWNER" --label "Riverside deposit per council resolution"
PG link acme:riverside.deposit@datasheet alternateOf acme:riverside.deposit@resolution --note "datasheet 6x vs resolution 29/226 2x — to be resolved"

echo "── Sensitive data (audience=internal) ──────────────────"
PG add acme:hillside.savings --value 415740 --from acme:src.datasheet --unit "EUR" \
  --audience internal --owner "$OWNER" --label "Design-cost saving (INTERNAL)"

echo "── Freshness: income thresholds from 2023-12 (overdue) ──"
PG add acme:income_thresholds --value "income criteria as of 2023-12" --from acme:src.rental-terms --type text \
  --owner "$OWNER" --label "Income thresholds (changed in Q4 2024 — re-verify)" \
  --verify-days 180 --last-verified 2023-12-01

echo
echo "✓ Demo seed ready. Now run: uv run provgraf check   (state BEFORE the change)"
