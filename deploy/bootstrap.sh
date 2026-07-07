#!/usr/bin/env bash
# Provision a fresh Ubuntu 22.04/24.04 VPS to run the DSource Material Bank
# pipeline 24/7. Idempotent — safe to re-run. Run as root (or with sudo).
#
#   curl -fsSL <repo>/deploy/bootstrap.sh | sudo bash
# or after cloning:  sudo bash deploy/bootstrap.sh
set -euo pipefail

APP_USER="${APP_USER:-mb}"
APP_DIR="${APP_DIR:-/opt/material-bank}"
REPO_URL="${REPO_URL:-}"          # set to your git remote, or rsync the code in first
PY="${PY:-python3}"

echo "==> system packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git rsync sqlite3 \
  ca-certificates curl build-essential libpq-dev

echo "==> app user + dir"
id -u "$APP_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$APP_USER"
mkdir -p "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> code (git clone or expect rsync'd code already present)"
if [ -n "$REPO_URL" ] && [ ! -d "$APP_DIR/.git" ]; then
  sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> venv + deps (torch CPU wheel to avoid the multi-GB CUDA build)"
sudo -u "$APP_USER" bash -c "
  cd '$APP_DIR'
  $PY -m venv .venv
  ./.venv/bin/pip install --upgrade pip wheel
  ./.venv/bin/pip install -r deploy/requirements.txt
  # open_clip pulls a PyPI torchvision that mismatches a CPU torch wheel
  # (RuntimeError: operator torchvision::nms does not exist). Install the matched
  # CPU pair LAST with --force-reinstall so the +cpu builds win.
  ./.venv/bin/pip install --force-reinstall --no-cache-dir \
      torch==2.12.1 torchvision==0.27.1 --index-url https://download.pytorch.org/whl/cpu
  ./.venv/bin/playwright install chromium
  ./.venv/bin/playwright install-deps chromium 2>/dev/null || true
"

echo "==> data dir (transfer catalog.db here, or it starts fresh)"
sudo -u "$APP_USER" mkdir -p "$APP_DIR/data" "$APP_DIR/reports"
if [ ! -f "$APP_DIR/data/catalog.db" ]; then
  echo "   NOTE: no catalog.db — rsync yours in, or it will build from the registry:"
  echo "     rsync -avz --progress data/catalog.db user@vps:$APP_DIR/data/"
fi

echo "==> warm the embedding model (one-time ~1GB download)"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && ./.venv/bin/python -c \"from material_bank.embeddings import MarqoEmbedder; MarqoEmbedder().encode_text(['warmup']); print('model ready')\""

echo "==> install systemd services"
sed "s#__APP_DIR__#$APP_DIR#g; s#__APP_USER__#$APP_USER#g" \
  deploy/systemd/mb-embed.service   > /etc/systemd/system/mb-embed.service
sed "s#__APP_DIR__#$APP_DIR#g; s#__APP_USER__#$APP_USER#g" \
  deploy/systemd/mb-harvest.service > /etc/systemd/system/mb-harvest.service
sed "s#__APP_DIR__#$APP_DIR#g; s#__APP_USER__#$APP_USER#g" \
  deploy/systemd/mb-api.service     > /etc/systemd/system/mb-api.service
systemctl daemon-reload
systemctl enable --now mb-embed mb-harvest mb-api

echo "==> done. status:"
systemctl --no-pager status mb-embed mb-harvest mb-api | grep -E "Loaded|Active" || true
echo
echo "API:      http://<vps-ip>:8000   (put behind nginx/caddy + TLS for public use)"
echo "logs:     journalctl -u mb-harvest -f   (or mb-embed / mb-api)"
echo "db:       sqlite3 $APP_DIR/data/catalog.db 'SELECT COUNT(*) FROM products;'"
