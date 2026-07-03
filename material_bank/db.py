"""SQLite control plane: schema, versioning, and the idempotent seed merge.

``catalog.db`` is the handoff contract to DSource, so it carries a
``schema_version`` row from day one — the two repos drift silently otherwise.

Seed philosophy: the seed loads only *identity* fields (brand, domain,
categories, confidence, notes). It never writes probe columns — the probe is
the sole verifier, and re-running the seed must never clobber probe results.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .models import Supplier

SCHEMA_VERSION = 2

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = _REPO_ROOT / "data" / "catalog.db"
# Seed sources, in precedence order (first occurrence of a domain wins).
NEW_REGISTRY_CSV = _REPO_ROOT / "suppliers.csv"
DSOURCE_SEED_CSV = _REPO_ROOT / "data" / "seed" / "manufacturers_dsource.csv"

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


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Ordered, idempotent migrations. Each runs once; its version is stamped so a
# v1 catalog.db upgrades to v2 without losing data.
_MIGRATIONS = (
    (1, _SUPPLIERS_DDL, "initial: suppliers registry + probe fields"),
    (2, _PRODUCTS_DDL, "products spec schema: surface units + per-field provenance"),
)


def migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations incrementally and stamp each (idempotent)."""
    conn.execute(_SCHEMA_VERSION_DDL)
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_version")}
    for version, ddl, description in _MIGRATIONS:
        if version in applied:
            continue
        conn.execute(ddl)
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


def load_seed(
    new_csv: Path | str = NEW_REGISTRY_CSV,
    dsource_csv: Path | str = DSOURCE_SEED_CSV,
) -> list[Supplier]:
    """Merge both seed CSVs, deduped on normalized domain (new CSV wins).

    On a domain collision, the DSource row's harvest hint is appended to the
    surviving row's notes so nothing learned in DSource is lost.
    """
    merged: dict[str, Supplier] = {}
    for supplier in _rows_from_new_registry(Path(new_csv)) + _rows_from_dsource(Path(dsource_csv)):
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
