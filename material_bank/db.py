"""SQLite control plane: schema, versioning, and the idempotent seed merge.

``catalog.db`` is the handoff contract to DSource, so it carries a
``schema_version`` row from day one — the two repos drift silently otherwise.

Seed philosophy: the seed loads only *identity* fields (brand, domain,
categories, confidence, notes). It never writes probe columns — the probe is
the sole verifier, and re-running the seed must never clobber probe results.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .models import NormalizedProduct, PriceObservation, Supplier

SCHEMA_VERSION = 20

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = _REPO_ROOT / "data" / "catalog.db"
# Seed sources, in precedence order (first occurrence of a domain wins).
NEW_REGISTRY_CSV = _REPO_ROOT / "suppliers.csv"
DSOURCE_SEED_CSV = _REPO_ROOT / "data" / "seed" / "manufacturers_dsource.csv"
# India Design ID exhibitor domains resolved by discover.py (LOWEST precedence:
# curated rows above win on collision, the exhibitor origin is grafted into notes).
ID_EXHIBITORS_CSV = _REPO_ROOT / "data" / "seed" / "india_design_id_resolved.csv"

_SUPPLIERS_DDL = """
CREATE TABLE IF NOT EXISTS suppliers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    -- identity (seeded) --
    brand             TEXT NOT NULL,
    domain            TEXT NOT NULL UNIQUE,
    categories        TEXT,
    domain_confidence TEXT,
    status            TEXT DEFAULT 'active',
    notes             TEXT,
    -- probe facts (written only by the probe; NULL until probed) --
    scrape_tier       TEXT,
    robots_ok         INTEGER,
    robots_url        TEXT,
    sitemap_url       TEXT,
    sku_estimate      INTEGER,
    price_published   TEXT,
    cms               TEXT,
    http_status       INTEGER,
    final_host        TEXT,
    probe_status      TEXT,
    probed_at         TEXT,
    probe_log         TEXT,
    -- harvest facts (Stage 2; NULL now) --
    last_harvest      TEXT,
    last_yield        INTEGER
);
"""

_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT NOT NULL,
    description TEXT
);
"""

# Stage-3 normalized spec (no price — prices are observations, Stage 7).
# Surface-unit columns + per-field provenance mirror models.NormalizedProduct.
_PRODUCTS_DDL = """
CREATE TABLE IF NOT EXISTS products (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_domain       TEXT,
    brand                 TEXT NOT NULL,
    sku                   TEXT NOT NULL,
    title                 TEXT,
    category              TEXT,
    size_mm               TEXT,
    finish                TEXT,
    price_unit            TEXT,
    coverage_sqft_per_box REAL,
    provenance            TEXT,   -- JSON: {field: {confidence, source, basis}}
    missing               TEXT,   -- JSON: [field, ...] known-absent, flagged
    created_at            TEXT,
    updated_at            TEXT,
    UNIQUE(brand, sku)            -- Stage-4 exact upsert key
);
"""

# Seed identity columns updated on conflict — deliberately excludes every probe
# and harvest column so a re-seed preserves probe work.
_SEED_COLUMNS = ("brand", "domain", "categories", "domain_confidence", "status", "notes")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_domain(value: str) -> str:
    """Reduce any URL or bare domain to a comparable host.

    ``https://www.Featherlite.com/shop?x=1`` -> ``featherlite.com``.
    Used as the dedupe key across both seed CSVs.
    """
    v = (value or "").strip().lower()
    if not v:
        return ""
    if "//" not in v:
        v = "//" + v  # give urlparse a netloc to find
    host = urlparse(v).netloc or urlparse(v).path
    host = host.split("@")[-1].split(":")[0]  # drop userinfo / port
    if host.startswith("www."):
        host = host[4:]
    return host.strip(".")


def connect(db_path: Path | str = DEFAULT_DB_PATH, *, check_same_thread: bool = True) -> sqlite3.Connection:
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")  # tolerate concurrent writers (harvest + embed)
    try:
        conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
    except sqlite3.OperationalError:
        pass
    # Cap WAL bloat: without this a checkpoint that can't truncate (a reader is mid-
    # snapshot during heavy writes) lets the -wal grow unbounded — it hit 562MB once
    # and locked out migrate. journal_size_limit truncates the WAL back to 64MB after
    # each checkpoint, so it stays bounded regardless of reader/writer overlap.
    conn.execute("PRAGMA journal_size_limit = 67108864")
    return conn


def connect_reader(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """A per-thread READ connection for the serving path. Deliberately does NOT run
    ``PRAGMA journal_mode`` (changing journal mode needs a write lock, so a fresh
    connection would BLOCK up to busy_timeout behind an active writer — probe/harvest/
    enrich — which is exactly what hung the API). A WAL database is read concurrently
    by any number of plain connections without setting the mode. ``query_only`` is the
    safety belt: this connection physically cannot write the catalog. NOTE: ``mode=ro``
    is NOT usable here — a read-only open of a LIVE WAL db needs -shm write access and
    hangs; ``query_only`` on a normal open is the correct read-concurrent path."""
    conn = sqlite3.connect(str(Path(db_path)), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA query_only = ON")
    return conn


def run_locked(conn: sqlite3.Connection, fn, *, tries: int = 6):
    """Run a write-then-commit closure, retrying on 'database is locked'. Concurrent
    writers (harvest workers + mb-embed + enrich) saturate the WAL write path, and
    busy_timeout can't clear a snapshot deadlock (it returns SQLITE_BUSY immediately) —
    only rollback + a fresh attempt can. The canonical product-write retry; ``fn`` must
    be idempotent under rollback (it re-runs from a clean state)."""
    import time
    for attempt in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == tries - 1:
                raise
            try:
                conn.rollback()
            except sqlite3.OperationalError:
                pass
            time.sleep(0.2 * (attempt + 1))


# Ordered, idempotent migrations. Each runs once; its version is stamped so a
# v1 catalog.db upgrades to v2 without losing data.
# Prices are observations, never product attributes (CLAUDE.md). Append-only:
# a changed price is a new row; identical re-observation is ignored.
_PRICE_OBSERVATION_DDL = """
CREATE TABLE IF NOT EXISTS price_observation (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    source      TEXT,                 -- domain the price came from
    price_inr   REAL NOT NULL,
    price_unit  TEXT,
    basis       TEXT NOT NULL,        -- listed_mrp | dealer_quote | estimated_band
    observed_at TEXT NOT NULL,
    source_url  TEXT,
    UNIQUE(product_id, source_url, price_inr, basis)
);
"""

# Records that fail schema/parse — never silently dropped, never silently kept.
_QUARANTINE_DDL = """
CREATE TABLE IF NOT EXISTS quarantine (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stage       TEXT,
    source_url  TEXT,
    reason      TEXT,
    raw_ref     TEXT,                 -- sha256 of the raw payload, if captured
    created_at  TEXT
);
"""

# One in-process vector index inside catalog.db, used three ways (Explore
# back-match / Specify retrieval / novelty gate). NOTE: the locked stack names
# sqlite-vec (vec0), but this platform's sqlite3 cannot load extensions and no
# pysqlite3 wheel exists for it — so vectors are a normalized float32 BLOB and
# search is numpy cosine (milliseconds at this scale). Kept behind VectorStore
# so sqlite-vec drops in later on an extension-capable sqlite build.
_EMBEDDINGS_DDL = """
CREATE TABLE IF NOT EXISTS embeddings (
    product_id INTEGER NOT NULL REFERENCES products(id),
    kind       TEXT NOT NULL,          -- 'text' | 'image' (shared space)
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,          -- float32 little-endian, L2-normalized
    created_at TEXT,
    PRIMARY KEY (product_id, kind)
);
"""

_PRODUCTS_IMAGE_URL_DDL = "ALTER TABLE products ADD COLUMN image_url TEXT;"

# FTS5 keyword index over products, kept in sync with triggers. Hybrid
# retrieval fuses this (lexical) with the vector index (semantic).
_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
    title, brand, category, content='products', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS products_ai AFTER INSERT ON products BEGIN
    INSERT INTO products_fts(rowid, title, brand, category)
    VALUES (new.id, new.title, new.brand, new.category);
END;
CREATE TRIGGER IF NOT EXISTS products_ad AFTER DELETE ON products BEGIN
    INSERT INTO products_fts(products_fts, rowid, title, brand, category)
    VALUES ('delete', old.id, old.title, old.brand, old.category);
END;
CREATE TRIGGER IF NOT EXISTS products_au AFTER UPDATE ON products BEGIN
    INSERT INTO products_fts(products_fts, rowid, title, brand, category)
    VALUES ('delete', old.id, old.title, old.brand, old.category);
    INSERT INTO products_fts(rowid, title, brand, category)
    VALUES (new.id, new.title, new.brand, new.category);
END;
"""

# Durable job queue (PIPELINE.md orchestration): one row per (stage, target).
# Workers claim atomically; failures increment attempts and reschedule with
# exponential backoff; exhausted jobs dead-letter to status='failed'.
_PIPELINE_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stage        TEXT NOT NULL,
    target       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 4,
    priority     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    result       TEXT,
    next_run_at  TEXT,
    created_at   TEXT,
    updated_at   TEXT,
    UNIQUE(stage, target)
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON pipeline_jobs(stage, status, next_run_at);
"""

# Per-harvest yield history — the signal drift detection reads to spot parser
# rot (a yield that suddenly collapses) and auto-open a repair job (Stage 9).
_HARVEST_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS harvest_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    products    INTEGER,
    priced      INTEGER,
    quarantined INTEGER
);
CREATE INDEX IF NOT EXISTS idx_hist_domain ON harvest_history(domain, observed_at);
"""

_MIGRATIONS = (
    (1, _SUPPLIERS_DDL, "initial: suppliers registry + probe fields"),
    (2, _PRODUCTS_DDL, "products spec schema: surface units + per-field provenance"),
    (3, _PRICE_OBSERVATION_DDL + _QUARANTINE_DDL, "price_observation (observations) + quarantine"),
    (4, _PRODUCTS_IMAGE_URL_DDL + _EMBEDDINGS_DDL, "products.image_url + embeddings vector store"),
    (5, _FTS_DDL, "FTS5 keyword index over products (hybrid retrieval)"),
    (6, _PIPELINE_JOBS_DDL, "pipeline_jobs durable queue with retry/backoff"),
    (7, _HARVEST_HISTORY_DDL, "harvest_history for yield-drift self-healing"),
    (8, "ALTER TABLE products ADD COLUMN source_url TEXT;"
        "CREATE INDEX IF NOT EXISTS idx_products_srcurl ON products(supplier_domain, source_url);",
     "products.source_url — exact resume key for specs-only harvests"),
    # Phase A (Foundation of Trust): the trust contract lives ON the product —
    # completeness (0-100, category-aware), verification tier, publish gate —
    # plus a metrics table so "getting better" is a stored time series.
    (9, """
        ALTER TABLE products ADD COLUMN completeness INTEGER;
        ALTER TABLE products ADD COLUMN verification_tier TEXT NOT NULL DEFAULT 'unverified';
        ALTER TABLE products ADD COLUMN publish_ready INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE products ADD COLUMN scored_at TEXT;
        CREATE INDEX IF NOT EXISTS idx_products_publish ON products(publish_ready);
        CREATE TABLE IF NOT EXISTS metrics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL,
            scope       TEXT NOT NULL DEFAULT 'global',
            key         TEXT NOT NULL,
            value       REAL
        );
        CREATE INDEX IF NOT EXISTS idx_metrics_key ON metrics(key, captured_at);
        """,
     "trust contract: completeness/tier/publish gate + metrics time series"),
    # Phase B: fields deterministic enrichment fills (description fuels later
    # extraction + LLM phases; enriched_at is the enrich stage's resume marker).
    (10, """
        ALTER TABLE products ADD COLUMN description TEXT;
        ALTER TABLE products ADD COLUMN color TEXT;
        ALTER TABLE products ADD COLUMN color_family TEXT;
        ALTER TABLE products ADD COLUMN thickness_mm REAL;
        ALTER TABLE products ADD COLUMN enriched_at TEXT;
        """,
     "enrichment fields: description, color(+family), thickness, enriched_at"),
    # Phase B: canonical taxonomy on the product (freeform `category` kept as
    # provenance). OmniClass code where verified, else NULL.
    (11, """
        ALTER TABLE products ADD COLUMN family TEXT;
        ALTER TABLE products ADD COLUMN category_std TEXT;
        ALTER TABLE products ADD COLUMN omniclass TEXT;
        ALTER TABLE products ADD COLUMN classified_at TEXT;
        CREATE INDEX IF NOT EXISTS idx_products_family ON products(family, category_std);
        """,
     "canonical taxonomy: family / category_std / omniclass"),
    # Backfill: products harvested BEFORE v8 (which added products.source_url)
    # kept the real PDP url only on their price_observation. The resumable
    # harvester skips already-observed urls, so those product rows were never
    # re-upserted to carry it — leaving ~6.8k priced products (orientbell tile
    # anchor, royaletouche, ...) with no procurement link despite a real url on
    # file. Propagate the freshest observation url onto the product. Same real
    # url, same provenance — a data-repair, not a fabrication.
    (12, """
        UPDATE products SET source_url = (
            SELECT po.source_url FROM price_observation po
            WHERE po.product_id = products.id AND po.source_url IS NOT NULL
                  AND TRIM(po.source_url) != ''
            ORDER BY po.observed_at DESC LIMIT 1)
        WHERE (source_url IS NULL OR TRIM(source_url) = '')
          AND EXISTS (SELECT 1 FROM price_observation po2
                      WHERE po2.product_id = products.id
                        AND po2.source_url IS NOT NULL AND TRIM(po2.source_url) != '');
        """,
     "backfill products.source_url from price_observation (pre-v8 rows)"),
    # Phase C (One Product, One Truth): non-destructive variant grouping. Same
    # design, many SKUs (a mattress in 200 size×thickness combos) -> one
    # variant_group_id; the catalog collapses to one card per design, the SKUs
    # are all kept. NULL = singleton (its own canonical).
    (13, """
        ALTER TABLE products ADD COLUMN variant_group_id TEXT;
        ALTER TABLE products ADD COLUMN resolved_at TEXT;
        CREATE INDEX IF NOT EXISTS idx_products_variant ON products(variant_group_id);
        """,
     "variant grouping: variant_group_id + resolved_at"),
    # Supplier procurement enrichment: the "who supplies it / where to buy"
    # layer. Collected ONLY from the supplier's own registered domain (legal
    # guardrail), each field provenance-tagged in supplier_provenance. A separate
    # `dealers` table holds the where-to-buy network (evidence trail); regions
    # served are DERIVED from it, never declared.
    (14, """
        ALTER TABLE suppliers ADD COLUMN legal_name TEXT;
        ALTER TABLE suppliers ADD COLUMN phones TEXT;          -- JSON array
        ALTER TABLE suppliers ADD COLUMN emails TEXT;          -- JSON array
        ALTER TABLE suppliers ADD COLUMN address TEXT;
        ALTER TABLE suppliers ADD COLUMN city TEXT;
        ALTER TABLE suppliers ADD COLUMN state TEXT;
        ALTER TABLE suppliers ADD COLUMN pincode TEXT;
        ALTER TABLE suppliers ADD COLUMN gstin TEXT;
        ALTER TABLE suppliers ADD COLUMN cin TEXT;
        ALTER TABLE suppliers ADD COLUMN dealer_locator_url TEXT;
        ALTER TABLE suppliers ADD COLUMN social TEXT;          -- JSON object
        ALTER TABLE suppliers ADD COLUMN logo_url TEXT;
        ALTER TABLE suppliers ADD COLUMN year_established TEXT;
        ALTER TABLE suppliers ADD COLUMN states_served TEXT;   -- JSON array (derived)
        ALTER TABLE suppliers ADD COLUMN cities_served TEXT;   -- JSON array (derived)
        ALTER TABLE suppliers ADD COLUMN dealer_count INTEGER;
        ALTER TABLE suppliers ADD COLUMN pan_india INTEGER;
        ALTER TABLE suppliers ADD COLUMN supplier_provenance TEXT; -- JSON {field:{source,basis,confidence,observed_at}}
        ALTER TABLE suppliers ADD COLUMN supplier_enriched_at TEXT;
        CREATE TABLE IF NOT EXISTS dealers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_domain TEXT NOT NULL,
            name            TEXT,
            address         TEXT,
            city            TEXT,
            state           TEXT,
            pincode         TEXT,
            lat             REAL,
            lon             REAL,
            phone           TEXT,
            email           TEXT,
            source_url      TEXT,
            observed_at     TEXT,
            UNIQUE(supplier_domain, name, pincode, address)
        );
        CREATE INDEX IF NOT EXISTS idx_dealers_domain ON dealers(supplier_domain);
        CREATE INDEX IF NOT EXISTS idx_dealers_geo ON dealers(state, city);
        """,
     "supplier procurement fields + dealers (where-to-buy) table"),
    # Demand instrumentation + intent capture. Everything built so far is
    # supply-side; these tables are the first demand-side signal so the moment
    # the catalog is in front of a user we measure intent (searches, views,
    # quote requests) instead of guessing. supplier_claims also powers the
    # claim/correct/takedown flow (turns a brand's objection into an onboarding).
    (15, """
        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at  TEXT NOT NULL,
            session_id   TEXT,
            kind         TEXT NOT NULL,   -- search | product_view | result_click | quote_request
            query        TEXT,
            product_id   INTEGER,
            supplier_domain TEXT,
            meta         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, occurred_at);
        CREATE TABLE IF NOT EXISTS quote_requests (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT NOT NULL,
            product_id    INTEGER,
            supplier_domain TEXT,
            source_url    TEXT,
            buyer_name    TEXT,
            buyer_contact TEXT,           -- email/phone the buyer voluntarily gives
            message       TEXT,
            status        TEXT NOT NULL DEFAULT 'new'
        );
        CREATE TABLE IF NOT EXISTS supplier_claims (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at     TEXT NOT NULL,
            supplier_domain TEXT NOT NULL,
            kind           TEXT NOT NULL, -- claim | correct | remove
            claimant_email TEXT,
            message        TEXT,
            status         TEXT NOT NULL DEFAULT 'new'
        );
        """,
     "demand instrumentation: events + quote_requests + supplier_claims"),
    # Stage A1: image-derived colour (measured from pixels, basis
    # derived:pixel-clustering). Kept separate from the text-derived `color` so
    # provenance/survivorship stays crisp (pixel beats title where both exist).
    (16, """
        ALTER TABLE products ADD COLUMN colour_primary TEXT;
        ALTER TABLE products ADD COLUMN colour_secondary TEXT;
        ALTER TABLE products ADD COLUMN colour_confidence REAL;
        ALTER TABLE products ADD COLUMN colour_scored_at TEXT;
        """,
     "image-derived colour: colour_primary/secondary/confidence"),
    # Stage B: LLM enrichment. Generated content is BONUS — it lives in its own
    # column, carries basis generated:llm:*, and never counts toward the
    # completeness score that feeds the publish gate (the honesty guarantee stays
    # on measured fields). llm_hash is the novelty gate; llm_status the outcome.
    (17, """
        ALTER TABLE products ADD COLUMN llm_content TEXT;
        ALTER TABLE products ADD COLUMN llm_hash TEXT;
        ALTER TABLE products ADD COLUMN llm_status TEXT;
        ALTER TABLE products ADD COLUMN llm_enriched_at TEXT;
        """,
     "LLM enrichment: llm_content (bonus) + novelty hash + status"),
    # LLM call ledger — full-fidelity accounting: one row per API call (incl.
    # retries/escalations and failures), with actual token counts and ₹ cost
    # derived from them. This is the source of truth for spend + the budget
    # circuit-breaker, and powers the LLM-ops cockpit. Never estimated.
    (18, """
        CREATE TABLE IF NOT EXISTS llm_calls (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at   TEXT NOT NULL,
            product_id    INTEGER,
            model         TEXT,
            prompt_version TEXT,
            phase         TEXT,          -- realtime | batch | eval
            attempt       INTEGER,       -- 0 primary, 1 retry, 2 escalation
            input_tokens  INTEGER,
            output_tokens INTEGER,
            cost_inr      REAL,
            latency_ms    INTEGER,
            status        TEXT,          -- enriched | verifier_failed | api_error
            fail_reason   TEXT,
            batch_job     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_llm_calls_time ON llm_calls(occurred_at);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_status ON llm_calls(status);
        """,
     "llm_calls ledger: per-call tokens + ₹ cost + status (observability)"),
    # Batch enrichment job tracking — batch is the production path (50% off) and
    # async (24h SLA), so jobs are first-class, resumable objects: submit records
    # a row, a collector polls + ingests, state survives restarts. Products in a
    # submitted job are marked llm_status='batched' so they aren't resubmitted.
    (19, """
        CREATE TABLE IF NOT EXISTS llm_batch_jobs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_name      TEXT NOT NULL UNIQUE,   -- provider operation/batch id
            model         TEXT,
            prompt_version TEXT,
            product_count INTEGER,
            status        TEXT NOT NULL DEFAULT 'submitted',  -- submitted | ingested | failed
            submitted_at  TEXT,
            ingested_at   TEXT,
            result        TEXT                    -- JSON ingest stats
        );
        CREATE INDEX IF NOT EXISTS idx_batch_jobs_status ON llm_batch_jobs(status);
        """,
     "llm_batch_jobs: first-class resumable batch jobs"),
    (20, """
        CREATE INDEX IF NOT EXISTS idx_price_obs_product_observed
            ON price_observation(product_id, observed_at);
        """,
     "price_observation(product_id, observed_at): freshest-price lookups for the listing API"),
)


def record_harvest(conn: sqlite3.Connection, domain: str, *, products: int,
                   priced: int = 0, quarantined: int = 0) -> None:
    conn.execute(
        "INSERT INTO harvest_history (domain, observed_at, products, priced, quarantined) "
        "VALUES (?,?,?,?,?)", (domain, now_iso(), products, priced, quarantined))
    conn.commit()


def rebuild_fts(conn: sqlite3.Connection) -> int:
    """Backfill the FTS index from existing products (rows inserted pre-v5)."""
    conn.execute("INSERT INTO products_fts(products_fts) VALUES('rebuild')")
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM products_fts").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations incrementally and stamp each (idempotent)."""
    conn.execute(_SCHEMA_VERSION_DDL)
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_version")}
    for version, ddl, description in _MIGRATIONS:
        if version in applied:
            continue
        conn.executescript(ddl)  # ddl may contain multiple statements
        conn.execute(
            "INSERT INTO schema_version(version, applied_at, description) VALUES (?, ?, ?)",
            (version, now_iso(), description),
        )
    conn.commit()


def get_schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return None if row is None else row["v"]


# --- seed loading -----------------------------------------------------------


def _hint(**kv: str) -> str:
    """Render surviving-but-untrusted seed hints for the notes column."""
    parts = [f"{k}={v}" for k, v in kv.items() if v and str(v).strip()]
    return f"[seed hint: {', '.join(parts)}]" if parts else ""


def _rows_from_new_registry(path: Path) -> list[Supplier]:
    if not path.exists():
        return []
    out: list[Supplier] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            # The new CSV's price_published/scrape_tier are pre-probe guesses,
            # not facts — fold them into notes, never into probe columns.
            hint = _hint(
                price_published=r.get("price_published", ""),
                tier=r.get("scrape_tier", ""),
            )
            notes = " ".join(p for p in (r.get("notes", "").strip(), hint) if p)
            out.append(
                Supplier(
                    brand=r["brand"].strip(),
                    domain=normalize_domain(r["domain"]),
                    categories=r.get("categories", "").strip(),
                    domain_confidence=r.get("domain_confidence", "medium").strip() or "medium",
                    status=r.get("status", "active").strip() or "active",
                    notes=notes,
                )
            )
    return out


def _rows_from_dsource(path: Path) -> list[Supplier]:
    if not path.exists():
        return []
    out: list[Supplier] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            cats = "|".join(
                c.strip().lower() for c in r.get("category", "").split("/") if c.strip()
            )
            hint = _hint(
                has_prices=r.get("has_prices", ""),
                data_type=r.get("data_type", ""),
                scrape=r.get("scrape", ""),
                origin=r.get("origin", ""),
            )
            notes = " ".join(p for p in (r.get("notes", "").strip(), hint) if p)
            out.append(
                Supplier(
                    brand=r["manufacturer"].strip(),
                    domain=normalize_domain(r["website"]),
                    categories=cats,
                    domain_confidence="high",  # real URLs used in DSource harvest
                    status="active",
                    notes=notes,
                )
            )
    return out


def _rows_from_id_exhibitors(path: Path) -> list[Supplier]:
    """India Design ID exhibitor rows (brand, domain, profile_url) from discover.py.
    Rows whose domain never resolved (blank) are skipped — they carry no harvest
    target. The profile_url is kept in notes as provenance."""
    if not path.exists():
        return []
    out: list[Supplier] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            domain = normalize_domain(r.get("domain", ""))
            if not domain:
                continue                       # unresolved brand — nothing to harvest yet
            notes = _hint(source="india_design_id", profile=r.get("profile_url", "").strip())
            out.append(
                Supplier(
                    brand=r["brand"].strip(),
                    domain=domain,
                    categories="",             # derived by probe/enrich, not asserted here
                    domain_confidence="medium",  # heuristically resolved from the profile page
                    status="active",
                    notes=notes,
                )
            )
    return out


def load_seed(
    new_csv: Path | str = NEW_REGISTRY_CSV,
    dsource_csv: Path | str = DSOURCE_SEED_CSV,
    id_exhibitors_csv: Path | str = ID_EXHIBITORS_CSV,
) -> list[Supplier]:
    """Merge the seed CSVs, deduped on normalized domain (earlier source wins).

    Precedence: curated ``suppliers.csv`` > DSource > India Design ID exhibitors.
    On a domain collision the later row's hint is appended to the surviving row's
    notes so nothing learned downstream is lost.
    """
    merged: dict[str, Supplier] = {}
    for supplier in (_rows_from_new_registry(Path(new_csv))
                     + _rows_from_dsource(Path(dsource_csv))
                     + _rows_from_id_exhibitors(Path(id_exhibitors_csv))):
        key = supplier.domain
        if not key:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = supplier
        elif supplier.notes and supplier.notes not in existing.notes:
            # collision: keep the (higher-precedence) existing row, graft the hint
            existing.notes = f"{existing.notes} {supplier.notes}".strip()
    return list(merged.values())


def upsert_product(conn: sqlite3.Connection, product: NormalizedProduct,
                   supplier_domain: str = "") -> int:
    """Insert/update a product on (brand, sku); returns its product_id."""
    prov = json.dumps({k: v.model_dump() for k, v in product.provenance.items()})
    missing = json.dumps(product.missing)
    ts = now_iso()

    def _w():
        conn.execute(
            """
            INSERT INTO products (supplier_domain, brand, sku, title, category, size_mm,
                finish, price_unit, coverage_sqft_per_box, image_url, source_url, provenance,
                missing, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(brand, sku) DO UPDATE SET
                supplier_domain=excluded.supplier_domain, title=excluded.title,
                category=excluded.category, size_mm=excluded.size_mm, finish=excluded.finish,
                price_unit=excluded.price_unit, coverage_sqft_per_box=excluded.coverage_sqft_per_box,
                image_url=COALESCE(excluded.image_url, products.image_url),
                source_url=COALESCE(excluded.source_url, products.source_url),
                provenance=excluded.provenance, missing=excluded.missing, updated_at=excluded.updated_at
            """,
            (supplier_domain, product.brand, product.sku, product.title, product.category,
             product.size_mm, product.finish,
             product.price_unit.value if product.price_unit else None,
             product.coverage_sqft_per_box, product.image_url, product.source_url, prov, missing, ts, ts),
        )
        row = conn.execute(
            "SELECT id FROM products WHERE brand=? AND sku=?", (product.brand, product.sku)
        ).fetchone()
        conn.commit()
        return row["id"]
    return run_locked(conn, _w)


def add_price_observation(conn: sqlite3.Connection, product_id: int,
                          obs: PriceObservation) -> None:
    """Append a price observation (idempotent: identical re-observation ignored)."""
    def _w():
        conn.execute(
            """
            INSERT OR IGNORE INTO price_observation
                (product_id, source, price_inr, price_unit, basis, observed_at, source_url)
            VALUES (?,?,?,?,?,?,?)
            """,
            (product_id, obs.source, obs.price_inr,
             obs.price_unit.value if obs.price_unit else None,
             obs.basis.value, obs.observed_at, obs.source_url),
        )
        conn.commit()
    run_locked(conn, _w)


def quarantine(conn: sqlite3.Connection, *, stage: str, source_url: str,
               reason: str, raw_ref: str | None = None) -> None:
    def _w():
        conn.execute(
            "INSERT INTO quarantine (stage, source_url, reason, raw_ref, created_at) "
            "VALUES (?,?,?,?,?)",
            (stage, source_url, reason, raw_ref, now_iso()),
        )
        conn.commit()
    run_locked(conn, _w)


def seed(conn: sqlite3.Connection, suppliers: list[Supplier] | None = None) -> int:
    """Upsert seed identity rows. Idempotent; never touches probe columns."""
    rows = suppliers if suppliers is not None else load_seed()
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in _SEED_COLUMNS if c != "domain")
    sql = (
        f"INSERT INTO suppliers ({', '.join(_SEED_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _SEED_COLUMNS)}) "
        f"ON CONFLICT(domain) DO UPDATE SET {set_clause}"
    )
    for s in rows:
        conn.execute(sql, (s.brand, s.domain, s.categories, s.domain_confidence, s.status, s.notes))
    conn.commit()
    return len(rows)
