#!/usr/bin/env bash
# Durable backup (mb-backup.timer, every 6h). Nothing is lost even if the VPS
# dies: the essential dump (everything except recomputable embeddings/FTS) is
# VERIFIED restorable, then force-pushed to the `vps-backups` branch on GitHub.
# Also keeps 2 full local .db snapshots for fast same-box recovery.
# Restore anywhere:  python -m material_bank.backup restore <dump.sql.gz> data/catalog.db
set -euo pipefail
cd "$(dirname "$0")/.."
PY=./.venv/bin/python
TS=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p data/backups

echo "[backup] $TS start"

# 1) full local snapshot (WAL-safe via sqlite .backup), keep newest 2
sqlite3 data/catalog.db ".backup data/backups/catalog-full-$TS.db"
gzip -f "data/backups/catalog-full-$TS.db"
ls -t data/backups/catalog-full-*.db.gz 2>/dev/null | tail -n +3 | xargs -r rm -f

# 2) essential dump + restore-verify (a backup that can't restore isn't one)
$PY -m material_bank.backup dump data/backups/catalog-essential.sql.gz

# 3) push to GitHub: single-commit orphan branch, force-pushed (remote stays small)
COUNTS=$(sqlite3 data/catalog.db "SELECT COUNT(*) FROM products" 2>/dev/null || echo "?")
BK=$(mktemp -d)
trap 'rm -rf "$BK"' EXIT
cp data/backups/catalog-essential.sql.gz "$BK/"
sqlite3 data/catalog.db "SELECT 'products='||(SELECT COUNT(*) FROM products)||' priced='||(SELECT COUNT(DISTINCT product_id) FROM price_observation)||' suppliers='||(SELECT COUNT(DISTINCT supplier_domain) FROM products)" > "$BK/MANIFEST.txt" 2>/dev/null || true
echo "created_utc=$TS" >> "$BK/MANIFEST.txt"
SIZE=$(stat -c%s "$BK/catalog-essential.sql.gz" 2>/dev/null || stat -f%z "$BK/catalog-essential.sql.gz")
if [ "$SIZE" -gt 95000000 ]; then  # GitHub 100MB file cap — split if we outgrow it
  split -b 90M "$BK/catalog-essential.sql.gz" "$BK/catalog-essential.sql.gz.part-"
  rm "$BK/catalog-essential.sql.gz"
  echo "split=true (reassemble: cat part-* > catalog-essential.sql.gz)" >> "$BK/MANIFEST.txt"
fi
cd "$BK"
git init -q && git checkout -q -b vps-backups
git config user.name "mb-vps" && git config user.email "mb-vps@material-bank"
git remote add origin git@github.com:udsy19/material-bank_data-scrape.git
git add -A && git commit -qm "backup $TS ($COUNTS products)"
git push -qf origin vps-backups
echo "[backup] pushed vps-backups ($COUNTS products, $SIZE bytes)"
