#!/usr/bin/env bash
# Continuous deployment (mb-deploy.timer, every 5 min, runs as root):
# when origin/main moves, fast-forward the VPS, sync deps/units if they changed,
# restart the services. Push to GitHub from anywhere -> live on the VPS in <=5min.
set -euo pipefail
APP_DIR=/opt/material-bank
cd "$APP_DIR"

sudo -u mb git fetch -q origin main
LOCAL=$(sudo -u mb git rev-parse HEAD)
REMOTE=$(sudo -u mb git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0

echo "[deploy] $(date -u +%FT%TZ) $LOCAL -> $REMOTE"
REQ_BEFORE=$(md5sum deploy/requirements.txt | cut -d' ' -f1)
UNITS_BEFORE=$(cat deploy/systemd/* 2>/dev/null | md5sum | cut -d' ' -f1)

sudo -u mb git reset -q --hard origin/main

# deps changed? sync them (CPU torch pair must stay matched — see bootstrap)
REQ_AFTER=$(md5sum deploy/requirements.txt | cut -d' ' -f1)
if [ "$REQ_BEFORE" != "$REQ_AFTER" ]; then
  echo "[deploy] requirements changed -> pip sync"
  sudo -u mb ./.venv/bin/pip install -q -r deploy/requirements.txt
  sudo -u mb ./.venv/bin/pip install -q --force-reinstall --no-cache-dir \
    torch==2.12.1 torchvision==0.27.1 --index-url https://download.pytorch.org/whl/cpu
fi

# unit files changed? re-render + daemon-reload + enable any new timers
UNITS_AFTER=$(cat deploy/systemd/* 2>/dev/null | md5sum | cut -d' ' -f1)
if [ "$UNITS_BEFORE" != "$UNITS_AFTER" ]; then
  echo "[deploy] systemd units changed -> reinstall"
  for u in deploy/systemd/*; do
    sed "s#__APP_DIR__#$APP_DIR#g; s#__APP_USER__#mb#g" "$u" > "/etc/systemd/system/$(basename "$u")"
  done
  systemctl daemon-reload
  for t in deploy/systemd/*.timer; do
    systemctl enable --now "$(basename "$t")" 2>/dev/null || true
  done
fi

systemctl restart mb-api mb-embed   # harvest is a oneshot timer: next tick uses new code
echo "[deploy] done -> $(sudo -u mb git log --oneline -1)"
