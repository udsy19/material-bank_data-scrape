# Deploying DSource Material Bank to a VPS

Runs three always-on systemd services so the pipeline keeps going 24/7 and
survives reboots — no more session teardowns killing the harvest.

| service | what it does |
|---|---|
| `mb-harvest` | producer: drains the harvest queue (shopify/woo/jsonld + Kajaria/Steelcase), self-heals drift, then idle-refreshes every 6h. Resumable. |
| `mb-embed`   | consumer: continuously embeds new products + rebuilds FTS (polls every 45s) |
| `mb-api`     | FastAPI search API + dashboard on `:8000` |

## VPS requirements
- Ubuntu 22.04 or 24.04
- **4 GB RAM min** (8 GB comfortable — CPU embedding + torch + the model)
- 2–4 vCPU · **20 GB disk** (608 MB db + ~3.7 GB model cache + code/logs)
- Outbound HTTPS (harvest); inbound `:8000` if you want the API public

## Deploy (≈15 min, mostly the model download)

**1. Get the code on the VPS** — either:
```bash
# option A: git
sudo git clone <your-repo-url> /opt/material-bank
# option B: rsync from your laptop (excludes venv/db/raw)
rsync -avz --exclude .venv --exclude data --exclude raw --exclude .git \
      ./ user@VPS:/opt/material-bank/
```

**2. Transfer the catalog** (keeps the ~121k products already harvested; skip to start fresh from the registry):
```bash
rsync -avz --progress data/catalog.db user@VPS:/opt/material-bank/data/
# (~366 MB gzipped in transit)
```

**3. Bootstrap** (installs python/venv/deps + torch-CPU + Playwright, warms the
model, installs & starts the 3 services):
```bash
ssh user@VPS
cd /opt/material-bank
sudo APP_DIR=/opt/material-bank bash deploy/bootstrap.sh
```

## Verify
```bash
systemctl status mb-harvest mb-embed mb-api
journalctl -u mb-harvest -f                    # watch the sweep
curl localhost:8000/api/stats | python3 -m json.tool
curl localhost:8000/api/pipeline               # queue health + dead-letters
sqlite3 data/catalog.db 'SELECT COUNT(*) FROM products;'
```

## Operate
```bash
systemctl restart mb-harvest        # force a fresh sweep now
systemctl stop mb-embed             # pause embedding
journalctl -u mb-embed -n 100       # recent embed passes
# add a supplier: insert a row in `suppliers`, next sweep picks it up
# re-arm dead-letters: python -m material_bank.harvest.worker --retry-dead
```

## Make the API public (optional)
Put nginx/Caddy in front for TLS; don't expose uvicorn directly. Example Caddy:
```
material.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}
```

## Notes
- **Politeness is preserved** — ~1 req/2s per domain still holds; the VPS just
  removes the teardown problem, it doesn't (and shouldn't) crawl faster.
- The model + Playwright chromium download once on bootstrap.
- Everything is idempotent/resumable: a reboot re-drains the queue from where it
  stopped (via `source_url` resume + `requeue_stale_running`).
- Image embeddings are a separate slower backfill — run on demand:
  `python -m material_bank.harvest.images`.
