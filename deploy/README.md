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

## HTTPS without owning a domain (sslip.io + Caddy)
`sslip.io` resolves `<ip>.sslip.io` -> `<ip>`, so Caddy can get a real Let's
Encrypt cert for it. Already set up on the VPS -> **https://46.202.179.28.sslip.io**.
```bash
# install caddy (cloudsmith repo), then:
cp deploy/Caddyfile /etc/caddy/Caddyfile      # 46.202.179.28.sslip.io { reverse_proxy localhost:8000 }
systemctl restart caddy
```
Swap the hostname in `deploy/Caddyfile` for a real domain later (just point its
A record at the IP).

## API endpoints
| endpoint | purpose |
|---|---|
| `GET /api/stats` | coverage counts + top suppliers |
| `GET /api/suppliers` | every supplier with product/priced/image counts + tier |
| `GET /api/match?q=&k=` | hybrid keyword+semantic search |
| `GET /api/products` | **structured listing** — filters below, paginated |
| `GET /api/product/{id}` | one product + all price observations + similar |
| `GET /api/pipeline` | harvest-queue health + dead-letters |
| `GET /api/image?url=` | image proxy |

`/api/products` filters (all optional, combine freely):
`supplier`, `brand`, `category` (substring), `q` (title substring),
`priced` (true/false), `has_image` (true/false), `min_price`, `max_price`,
`order` (id|price|title|brand), `desc` (true/false), `limit` (<=200), `offset`.
Returns `{total, count, limit, offset, items[]}`.
```bash
# priced tiles over Rs 100, cheapest first, page 1
curl 'https://46.202.179.28.sslip.io/api/products?category=tiles&priced=true&min_price=100&order=price&limit=20'
# everything from one supplier
curl 'https://46.202.179.28.sslip.io/api/products?supplier=interio.com&limit=50'
```

## CI/CD + disaster recovery (nothing gets lost)
| unit | cadence | does |
|---|---|---|
| `mb-deploy.timer` | 5 min | fetch origin/main → if moved: reset --hard, pip-sync on requirements change, re-render units on unit change, restart services. **Push to GitHub from anywhere → live on the VPS ≤5 min.** |
| `mb-backup.timer` | 6 h | full local .db snapshot (keep 2) + **essential dump** (everything except recomputable embeddings/FTS, ~11MB gz) → *verified by restoring into a temp db* → force-pushed to the `vps-backups` branch on GitHub |

**If the VPS dies** — full recovery on any fresh box:
```bash
git clone git@github.com:udsy19/material-bank_data-scrape.git /opt/material-bank
cd /opt/material-bank && sudo bash deploy/bootstrap.sh          # provisions + services
git fetch origin vps-backups && git show origin/vps-backups:catalog-essential.sql.gz > /tmp/e.sql.gz
rm -f data/catalog.db && ./.venv/bin/python -m material_bank.backup restore /tmp/e.sql.gz data/catalog.db
systemctl restart mb-api mb-embed   # embed worker re-fills embeddings (~1h); FTS already rebuilt
```
Secrets: `/opt/material-bank/.env` (chmod 600, gitignored) — `GEMINI_API_KEY`, plumbed
into every service via `EnvironmentFile`. Re-create by hand on a new box.

## Notes
- **Politeness is preserved** — ~1 req/2s per domain still holds; the VPS just
  removes the teardown problem, it doesn't (and shouldn't) crawl faster.
- The model + Playwright chromium download once on bootstrap.
- Everything is idempotent/resumable: a reboot re-drains the queue from where it
  stopped (via `source_url` resume + `requeue_stale_running`).
- Image embeddings are a separate slower backfill — run on demand:
  `python -m material_bank.harvest.images`.
# CD smoke: 2026-07-07T20:40:11Z
