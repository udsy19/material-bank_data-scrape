#!/usr/bin/env bash
# Harvest supervisor: drain the queue, then idle-refresh on a cycle. Run by
# mb-harvest.service. Resumable + idempotent — a crash/reboot just re-drains.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=./.venv/bin/python
REFRESH_SECONDS="${REFRESH_SECONDS:-21600}"   # 6h between full sweeps

while true; do
  echo "[harvest] $(date -u +%FT%TZ) starting sweep"

  # 1) reclaim jobs a prior run left 'running' (crash/reboot recovery)
  $PY - <<'EOF'
from material_bank import db, jobs
c = db.connect(); n = jobs.requeue_stale_running(c)
print(f"[harvest] requeued {n} stale-running jobs")
EOF

  # 2) shopify/woo/jsonld across the registry (uncapped; skips done work)
  $PY -m material_bank.harvest.worker \
      --tiers shopify,woocommerce,jsonld --workers 8 --jsonld-limit 0 \
      --exclude ikea.com,lxhausys.com,qutone.com

  # 3) bespoke domains (Kajaria/Steelcase) — run their harvesters directly
  $PY - <<'EOF'
from material_bank import db
from material_bank.fetch import Fetcher
from material_bank.harvest.kajaria import harvest_kajaria
from material_bank.harvest.steelcase import harvest_steelcase
c = db.connect()
for name, fn in (("kajaria", harvest_kajaria), ("steelcase", harvest_steelcase)):
    try:
        s = fn(c, Fetcher(raw_dir=None))
        print(f"[harvest] {name}: {s.get('products',0)} new")
    except Exception as e:
        print(f"[harvest] {name} error: {e}")
EOF

  # 4) self-healing: yield-drift -> repair jobs; then run repairs
  $PY - <<'EOF'
from material_bank import db, drift, repair
c = db.connect()
print("[harvest] self-heal:", drift.scan_and_open(c))
print("[harvest] repairs:", repair.drain_repairs())
EOF

  echo "[harvest] sweep done; sleeping ${REFRESH_SECONDS}s"
  sleep "$REFRESH_SECONDS"
done
