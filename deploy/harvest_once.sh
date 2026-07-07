#!/usr/bin/env bash
# One harvest sweep, triggered hourly by mb-harvest.timer. Incremental + polite:
#   * recover crashed jobs, re-arm suppliers past their (tier-aware) refresh window
#   * seed NEW suppliers, drain the queue (done+fresh suppliers are skipped)
#   * bespoke sites resume via source_url; drift -> repair jobs
# systemd won't start a second run while one is in progress, so a long sweep
# (draining a giant) simply spans several hourly ticks — no overlap, no re-scrape.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=./.venv/bin/python
echo "[harvest] $(date -u +%FT%TZ) sweep start"

$PY - <<'EOF'
from material_bank import db, jobs
c = db.connect()
print(f"[harvest] requeued stale: {jobs.requeue_stale_running(c)}")
print(f"[harvest] due refreshes:  {jobs.enqueue_due_refreshes(c)}")  # shopify/woo daily, jsonld weekly, else monthly
EOF

$PY -m material_bank.harvest.worker \
    --tiers shopify,woocommerce,jsonld --workers 8 --jsonld-limit 0 \
    --exclude ikea.com,lxhausys.com,qutone.com

$PY - <<'EOF'
from material_bank import db
from material_bank.fetch import Fetcher
from material_bank.harvest.kajaria import harvest_kajaria
from material_bank.harvest.steelcase import harvest_steelcase
c = db.connect()
for name, fn in (("kajaria", harvest_kajaria), ("steelcase", harvest_steelcase)):
    try:
        print(f"[harvest] {name}: {fn(c, Fetcher(raw_dir=None)).get('products',0)} new")
    except Exception as e:
        print(f"[harvest] {name} error: {e}")
EOF

$PY - <<'EOF'
from material_bank import db, drift, repair
c = db.connect()
print("[harvest] self-heal:", drift.scan_and_open(c))
print("[harvest] repairs:", repair.drain_repairs())
EOF

# 5) the planner: re-score the trust contract + snapshot the scorecard
$PY -m material_bank.planner || echo "[harvest] planner failed (non-fatal)"

echo "[harvest] sweep done"
