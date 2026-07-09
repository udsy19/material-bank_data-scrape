#!/usr/bin/env bash
# One flywheel tick (mb-flywheel.timer, ~45min): the FAST self-improvement
# stages — text-pass extraction -> score/snapshot/seed (planner) -> bounded
# enrich drain. Split from mb-harvest on purpose: a giant supplier crawl can
# hold the harvest sweep for many hours, and these stages must never starve
# behind it (day-1 autonomy lesson). Queue claims are atomic, WAL absorbs the
# concurrent writers, so both services run side by side safely.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=./.venv/bin/python
echo "[flywheel] $(date -u +%FT%TZ) tick"

$PY -m material_bank.enrich --text-pass  || echo "[flywheel] text-pass failed (non-fatal)"
$PY -m material_bank.planner              || echo "[flywheel] planner failed (non-fatal)"
$PY -m material_bank.enrich --drain --limit 250 --workers 4 \
                                          || echo "[flywheel] enrich drain failed (non-fatal)"

echo "[flywheel] tick done"
