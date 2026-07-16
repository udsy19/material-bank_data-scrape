#!/usr/bin/env bash
# Harvest supervisor (mb-harvest.service). Incremental + self-healing:
#   * hourly: recover crashed jobs, pick up NEW/incomplete suppliers, drain queue
#   * cadenced: re-fetch only suppliers past their refresh window
#     (priced weekly / spec-only monthly) — routine cycles stay cheap
#   * bespoke sites (Kajaria/Steelcase) resume via source_url (no re-download)
#   * drift -> repair jobs
# Resumable/idempotent: a reboot just re-drains from where it stopped.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=./.venv/bin/python
POLL="${POLL_SECONDS:-3600}"   # 1h — drains are cheap; refresh is per-supplier cadenced

while true; do
  echo "[harvest] $(date -u +%FT%TZ) cycle start"

  # 1) recover crashed 'running' jobs + re-arm suppliers due a refresh
  $PY - <<'EOF'
from material_bank import db, jobs
c = db.connect()
print(f"[harvest] requeued stale: {jobs.requeue_stale_running(c)}")
print(f"[harvest] due refreshes:  {jobs.enqueue_due_refreshes(c)}")  # priced 7d / spec 30d
EOF

  # 2) drain the queue (seeds NEW suppliers, then processes pending only — done
  #    suppliers are skipped unless a refresh re-armed them)
  $PY -m material_bank.harvest.worker \
      --tiers shopify,woocommerce,jsonld --workers 8 --jsonld-limit 0 \
      --exclude ikea.com,lxhausys.com,qutone.com

  # 3) bespoke domains — cheap no-op when nothing new (source_url resume)
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

  # 4) self-heal: yield-drift/quarantine-spike -> repair jobs, then run repairs
  $PY - <<'EOF'
from material_bank import db, drift, repair
c = db.connect()
print("[harvest] self-heal:", drift.scan_and_open(c))
print("[harvest] repairs:", repair.drain_repairs())
EOF

  echo "[harvest] cycle done; sleeping ${POLL}s"
  sleep "$POLL"
done
