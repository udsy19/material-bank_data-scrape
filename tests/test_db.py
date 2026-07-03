import csv

import pytest

from material_bank import db
from material_bank.db import (
    SCHEMA_VERSION,
    connect,
    get_schema_version,
    load_seed,
    migrate,
    normalize_domain,
    seed,
)
from material_bank.models import Supplier


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "catalog.db")
    migrate(c)
    yield c
    c.close()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.featherlitefurniture.com/", "featherlitefurniture.com"),
        ("http://interio.com/business/workspace", "interio.com"),
        ("orientbell.com", "orientbell.com"),
        ("HTTPS://WWW.Kajaria.com:443/tiles", "kajaria.com"),
        ("//user@nitco.in/x", "nitco.in"),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def test_migrate_creates_all_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(suppliers)")}
    for c in (
        "brand", "domain", "categories", "domain_confidence", "status", "notes",
        "scrape_tier", "robots_ok", "sitemap_url", "sku_estimate", "price_published",
        "cms", "http_status", "final_host", "probe_status", "probed_at", "probe_log",
        "last_harvest", "last_yield",
    ):
        assert c in cols, f"missing column {c}"


def test_schema_version_stamped_once(conn):
    assert get_schema_version(conn) == SCHEMA_VERSION
    migrate(conn)  # re-run
    rows = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
    assert rows == 1  # not duplicated


def test_seed_probe_columns_default_null(conn):
    seed(conn, [Supplier(brand="X", domain="x.com", categories="tiles")])
    row = conn.execute("SELECT * FROM suppliers WHERE domain='x.com'").fetchone()
    for c in ("scrape_tier", "robots_ok", "sku_estimate", "price_published", "probed_at"):
        assert row[c] is None


def test_seed_is_idempotent(conn):
    rows = [Supplier(brand="X", domain="x.com"), Supplier(brand="Y", domain="y.com")]
    seed(conn, rows)
    seed(conn, rows)
    n = conn.execute("SELECT COUNT(*) AS n FROM suppliers").fetchone()["n"]
    assert n == 2  # no duplicate rows on re-seed


def test_reseed_preserves_probe_work(conn):
    """The safety invariant: seeding again must not clobber probe results."""
    seed(conn, [Supplier(brand="X", domain="x.com")])
    conn.execute(
        "UPDATE suppliers SET scrape_tier='shopify', probed_at='2026-07-02T00:00:00Z' "
        "WHERE domain='x.com'"
    )
    conn.commit()
    seed(conn, [Supplier(brand="X-renamed", domain="x.com", notes="new note")])
    row = conn.execute("SELECT * FROM suppliers WHERE domain='x.com'").fetchone()
    assert row["brand"] == "X-renamed"          # identity updated
    assert row["notes"] == "new note"
    assert row["scrape_tier"] == "shopify"       # probe work preserved
    assert row["probed_at"] == "2026-07-02T00:00:00Z"


def test_load_seed_merges_and_dedupes(tmp_path):
    new = tmp_path / "suppliers.csv"
    old = tmp_path / "dsource.csv"
    with new.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["brand", "domain", "categories", "price_published", "scrape_tier",
                    "domain_confidence", "status", "notes"])
        w.writerow(["Nilkamal", "nilkamalfurniture.com", "furniture", "yes", "0",
                    "verified", "active", "primary row"])
    with old.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["manufacturer", "category", "website", "origin", "has_prices",
                    "data_type", "scrape", "notes"])
        # collides with new on normalized domain -> deduped, hint grafted
        w.writerow(["Nilkamal Furniture", "office chairs / storage",
                    "https://www.nilkamalfurniture.com/x", "Indian", "yes",
                    "shopify-json", "easy", "old note"])
        # unique -> added
        w.writerow(["Geeken", "seating / workstations", "https://geeken.in/",
                    "Indian", "no", "js+pdf", "medium", "1100+ products"])

    merged = load_seed(new, old)
    by_domain = {s.domain: s for s in merged}
    assert set(by_domain) == {"nilkamalfurniture.com", "geeken.in"}  # deduped
    # new CSV wins identity; old hint grafted into notes
    nk = by_domain["nilkamalfurniture.com"]
    assert nk.brand == "Nilkamal"
    assert "shopify-json" in nk.notes and "primary row" in nk.notes
    # DSource row: slashes -> pipe categories, confidence promoted to high
    gk = by_domain["geeken.in"]
    assert gk.categories == "seating|workstations"
    assert gk.domain_confidence == "high"


def test_seed_does_not_write_new_csv_price_into_probe_column(tmp_path):
    """price_published='yes' in the seed CSV must not become a probe fact."""
    new = tmp_path / "suppliers.csv"
    with new.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["brand", "domain", "categories", "price_published", "scrape_tier",
                    "domain_confidence", "status", "notes"])
        w.writerow(["Orientbell", "orientbell.com", "tiles", "yes", "unknown",
                    "verified", "active", "anchor"])
    rows = load_seed(new, tmp_path / "missing.csv")
    s = rows[0]
    assert "price_published=yes" in s.notes  # preserved as hint...
    c = connect(tmp_path / "catalog.db")
    migrate(c)
    seed(c, rows)
    row = c.execute("SELECT * FROM suppliers WHERE domain='orientbell.com'").fetchone()
    assert row["price_published"] is None  # ...but NOT trusted as a probe fact
    c.close()


def test_real_seed_files_load(tmp_path):
    """Smoke: the actual repo seed CSVs parse and merge without error."""
    merged = load_seed(db.NEW_REGISTRY_CSV, db.DSOURCE_SEED_CSV)
    assert len(merged) > 90  # ~90 new + DSource extras, deduped
    assert all(s.domain and s.brand for s in merged)
    assert len({s.domain for s in merged}) == len(merged)  # domains unique
