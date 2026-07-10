"""Supplier enrichment stage: own-domain fetch, extract, store with provenance."""

import json

import pytest

from material_bank import db as db_mod
from material_bank import supplier_enrich
from material_bank.fetch import FetchResult

HOME = """
<html><head><script type="application/ld+json">
{"@type":"Organization","name":"Somany","legalName":"Somany Ceramics Limited",
 "telephone":"+91 124 4623000","email":"info@somany.example",
 "address":{"@type":"PostalAddress","addressLocality":"Gurugram","addressRegion":"Haryana","postalCode":"122001"},
 "sameAs":["https://www.linkedin.com/company/somany"]}
</script></head><body>
<a href="/store-locator">Where to buy</a></body></html>
"""
CONTACT = """
<html><body><footer>Somany Ceramics Limited GSTIN: 06AABCK0710M1Z8
Toll Free: 1800-419-2077</footer></body></html>
"""


class FakeFetcher:
    def get(self, url):
        path = url.split("somany.example", 1)[1] if "somany.example" in url else url
        body = ""
        if path in ("/", ""):
            body = HOME
        elif path == "/contact-us":
            body = CONTACT
        elif path == "/robots.txt":
            body = ""              # permissive
        status = 200 if (path in ("/", "", "/contact-us", "/robots.txt")) else 404
        return FetchResult(requested_url=url, status_code=status, text=body, final_url=url)


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db", check_same_thread=False)
    db_mod.migrate(c)
    c.execute("INSERT INTO suppliers (domain, brand, status) VALUES (?,?,?)",
              ("somany.example", "Somany", "active"))
    c.commit()
    yield c
    c.close()


def test_enrich_supplier_stores_company_info_with_provenance(conn):
    stats = supplier_enrich.enrich_supplier(conn, "somany.example", FakeFetcher())
    assert stats["reachable"] and stats["pages_fetched"] >= 2

    r = conn.execute("SELECT * FROM suppliers WHERE domain=?", ("somany.example",)).fetchone()
    assert r["legal_name"] == "Somany Ceramics Limited"
    assert r["city"] == "Gurugram" and r["pincode"] == "122001"
    assert r["gstin"] == "06AABCK0710M1Z8"                    # from the contact page
    assert r["dealer_locator_url"].endswith("/store-locator")
    phones = json.loads(r["phones"])
    assert "1244623000" in phones and any(p.startswith("1800") for p in phones)
    assert json.loads(r["social"])["linkedin"].endswith("/somany")
    prov = json.loads(r["supplier_provenance"])
    assert prov["legal_name"]["basis"] == "observed"
    assert r["supplier_enriched_at"] is not None
    # brand/status preserved (not clobbered)
    assert r["brand"] == "Somany" and r["status"] == "active"


def test_unreachable_domain_marked(conn):
    class Dead:
        def get(self, url):
            return FetchResult(requested_url=url, status_code=503, text="", final_url=url)
    stats = supplier_enrich.enrich_supplier(conn, "somany.example", Dead())
    assert stats["reachable"] is False and stats["pages_fetched"] == 0


def test_seed_supplier_jobs(conn):
    from material_bank import jobs
    assert supplier_enrich.seed_supplier_jobs(conn) == 1
    assert jobs.counts(conn, "supplier")["pending"] == 1
