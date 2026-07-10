#!/usr/bin/env bash
# One harvest sweep, triggered by mb-harvest.timer. Incremental + polite:
#   * recover crashed jobs, re-arm suppliers past their (tier-aware) refresh window
#   * seed NEW suppliers, drain the queue (done+fresh suppliers are skipped)
#   * bespoke sites resume via source_url; drift -> repair jobs
# The queue-drain phase is time-boxed (HARVEST_DRAIN_TIMEOUT): draining a giant
# jsonld site at 1 req/2s can take *days*, and an unbounded drain monopolizes the
# timer so bespoke + self-heal never run (observed: one sweep ran 2 days, blocking
# every later phase). The worker is fully resumable (atomic queue claims +
# requeue_stale_running + source_url), so a cut-off drain just continues next tick
# — we still collect everything, spread across sweeps, without a per-run product cap.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=./.venv/bin/python
DRAIN_TIMEOUT="${HARVEST_DRAIN_TIMEOUT:-60m}"
echo "[harvest] $(date -u +%FT%TZ) sweep start"

$PY - <<'EOF'
from material_bank import db, jobs
c = db.connect()
print(f"[harvest] requeued stale: {jobs.requeue_stale_running(c)}")
print(f"[harvest] due refreshes:  {jobs.enqueue_due_refreshes(c)}")  # shopify/woo daily, jsonld weekly, else monthly
EOF

# SIGTERM at the deadline, SIGKILL 30s later if it ignores it. Exit 124 = timed
# out (expected for a big backlog), not a failure — the next sweep resumes.
timeout --kill-after=30s "$DRAIN_TIMEOUT" \
    $PY -m material_bank.harvest.worker \
    --tiers shopify,woocommerce,jsonld --workers 8 --jsonld-limit 0 \
    --exclude ikea.com,lxhausys.com,qutone.com \
    && echo "[harvest] drain: queue emptied" \
    || echo "[harvest] drain: time-boxed at $DRAIN_TIMEOUT (resumes next sweep)"

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

# NB: scoring/enrichment live in mb-flywheel (own timer) — a giant crawl here
# must never starve them (day-1 autonomy lesson).
echo "[harvest] sweep done"
