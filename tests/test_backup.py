"""Backup round-trip: dump essential -> restore -> identical data, working FTS."""

import shutil

import pytest

from material_bank import backup, db as db_mod
from material_bank.harvest.common import build_product
from material_bank.models import PriceBasis, PriceObservation

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None,
                                reason="sqlite3 CLI not available")


@pytest.fixture()
def populated(tmp_path):
    path = tmp_path / "catalog.db"
    c = db_mod.connect(path)
    db_mod.migrate(c)
    c.execute("INSERT INTO suppliers(brand,domain,status,scrape_tier) "
              "VALUES('Orientbell','orientbell.com','active','jsonld')")
    for i in range(5):
        pid = db_mod.upsert_product(c, build_product(
            brand="Orientbell", sku=f"T{i}", title=f"Emperador Marble {i}",
            category="tiles", source="u", size_mm="600x600", finish="Glossy",
            price_unit=None, coverage_sqft_per_box=None), supplier_domain="orientbell.com")
        db_mod.add_price_observation(c, pid, PriceObservation(
            source="orientbell.com", price_inr=80 + i, basis=PriceBasis.LISTED_MRP,
            observed_at=db_mod.now_iso(), source_url=f"https://o/{i}"))
    db_mod.record_harvest(c, "orientbell.com", products=5, priced=5)
    c.commit()
    c.close()
    return path


def test_dump_restore_round_trip(populated, tmp_path):
    dump = tmp_path / "essential.sql.gz"
    info = backup.dump_essential(populated, dump)
    assert dump.exists() and info["bytes"] > 100

    counts = backup.restore(dump, tmp_path / "restored.db")
    assert counts["products"] == 5
    assert counts["price_observation"] == 5
    assert counts["suppliers"] == 1
    assert counts["harvest_history"] == 1
    assert counts["schema_version"] == db_mod.SCHEMA_VERSION  # migrations stamped
    assert counts["fts_rows"] == 5                            # keyword index rebuilt

    # restored db actually works: keyword search + derived tables present
    r = db_mod.connect(tmp_path / "restored.db")
    from material_bank.retrieval import keyword_search
    assert len(keyword_search(r, "emperador", k=10)) == 5
    assert r.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0  # recomputable, empty
    # provenance survived byte-exact
    row = r.execute("SELECT provenance, missing FROM products WHERE sku='T0'").fetchone()
    assert "size_mm" in row["provenance"] and "coverage_sqft_per_box" in row["missing"]
    r.close()


def test_verify_helper(populated, tmp_path):
    dump = tmp_path / "essential.sql.gz"
    backup.dump_essential(populated, dump)
    counts = backup.verify(dump)
    assert counts["products"] == 5


def test_restore_refuses_overwrite(populated, tmp_path):
    dump = tmp_path / "e.sql.gz"
    backup.dump_essential(populated, dump)
    target = tmp_path / "x.db"
    target.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        backup.restore(dump, target)


def test_dump_rejects_empty_db(tmp_path):
    empty = tmp_path / "empty.db"
    c = db_mod.connect(empty); c.close()  # no tables
    with pytest.raises(RuntimeError):
        backup.dump_essential(empty, tmp_path / "out.sql.gz")
