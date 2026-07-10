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

# GSTIN registry backfill — no-op unless GSTIN_API_URL/KEY are configured (₹0 off).
$PY -m material_bank.gstin --limit 100 || echo "[suppliers] gstin backfill skipped (non-fatal)"

# dealer / where-to-buy networks (deterministic parsers, verified sources).
# Single-Interface brands are crawl-bound; --limit caps pages/sweep so they fill
# in across weeks (resumable, skips already-stored detail URLs).
for dom in kajariaceramics.com orientbell.com; do
    $PY -m material_bank.dealers --domain "$dom" || echo "[suppliers] dealers $dom failed (non-fatal)"
done
for dom in hrjohnsonindia.com somanyceramics.com jaquar.com godrejinterio.com; do
    timeout 40m $PY -m material_bank.dealers --domain "$dom" --limit 1500 \
        || echo "[suppliers] dealers $dom time-boxed/failed (resumes next week)"
done

echo "[suppliers] refresh done"
