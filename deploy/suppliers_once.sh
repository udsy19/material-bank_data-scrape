#!/usr/bin/env bash
# Weekly supplier procurement refresh (mb-suppliers.timer): the "who supplies it
# / where to buy" layer. Supplier sites + dealer networks change slowly, so this
# runs weekly, NOT on the fast flywheel — polite (own-domain fetches at ~1 req/2s)
# and idempotent. Company info: seed only un-enriched suppliers + drain. Dealers:
# re-harvest each registered platform (dedup keeps it clean); regions re-derived.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=./.venv/bin/python
echo "[suppliers] $(date -u +%FT%TZ) refresh start"

$PY -m material_bank.supplier_enrich --seed --only-missing || echo "[suppliers] seed failed (non-fatal)"
timeout 90m $PY -m material_bank.supplier_enrich --drain --workers 4 \
    || echo "[suppliers] enrich drain time-boxed/failed (resumes next week)"

# dealer / where-to-buy networks (deterministic parsers, verified sources)
for dom in kajariaceramics.com orientbell.com; do
    $PY -m material_bank.dealers --domain "$dom" || echo "[suppliers] dealers $dom failed (non-fatal)"
done

echo "[suppliers] refresh done"
