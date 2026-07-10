"""Dealer harvesting: deterministic parsers, CRM-field exclusion, region derive."""

import json

import pytest

from material_bank import db as db_mod
from material_bank import dealers


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db", check_same_thread=False)
    db_mod.migrate(c)
    c.execute("INSERT INTO suppliers (domain, brand, status) VALUES (?,?,?)",
              ("kajariaceramics.com", "Kajaria", "active"))
    c.commit()
    yield c
    c.close()


def test_parse_kajaria_store(conn):
    rec = {"name": "Ceramic Center", "address1": "1, Gat No. 726", "address2": "Sangamner Road",
           "city": "ahmednagar", "state": "maharashtra", "pincode": "422605",
           "mobile": "9730635377", "phone": None, "email": "cc@rediffmail.com",
           "latitude": "19.5646", "longitude": "74.1820",
           "dealer_sap_code": "SECRET", "no_of_sales": "1000"}   # internal fields present
    d = dealers.parse_kajaria_store(rec)
    assert d["name"] == "Ceramic Center" and d["city"] == "Ahmednagar"
    assert d["state"] == "Maharashtra" and d["pincode"] == "422605"
    assert d["phone"] == "9730635377" and d["lat"] == pytest.approx(19.5646)
    assert "SECRET" not in json.dumps(d) and "no_of_sales" not in d   # CRM excluded


SI_HTML = """
<div itemscope itemtype="https://schema.org/LocalBusiness">
  <div itemprop="name">House of Johnson Experience Centre</div>
  <div itemprop="address" itemscope itemtype="https://schema.org/PostalAddress">
    <span itemprop="streetAddress">G-3, Block B, Mohan Estate</span>
    <span itemprop="addressRegion">new delhi</span>
    <span itemprop="postalCode">110044</span>
    <span itemprop="telephone">08037762679</span>
  </div>
  <meta itemprop="latitude" content="28.5163">
  <meta itemprop="longitude" content="77.2957">
</div>
<h2 class="dl-loc-address"><div>G-3, Block B, Badarpur, new delhi, delhi - 110044</div></h2>
"""


def test_parse_singleinterface_detail_reads_city_from_region_state_from_block():
    d = dealers.parse_singleinterface_detail(SI_HTML, "https://stores.x/abc/home")
    assert d["name"] == "House of Johnson Experience Centre"
    assert d["city"] == "New Delhi"                 # addressRegion holds the CITY here
    assert d["state"] == "Delhi"                    # parsed from the dl-loc-address block
    assert d["pincode"] == "110044" and d["phone"] == "08037762679"
    assert d["lat"] == pytest.approx(28.5163) and d["email"] is None
    assert d["source_url"].endswith("/home")


def test_parse_orientbell_excludes_crm_fields():
    obtb = {"business_name": "OBTB Pune", "address_1": "Vega Centre", "address_2": "Shankarsheth Rd",
            "city": "Pune", "state": "Maharashtra", "postcode": "411037",
            "main_phone_no": "+919167340218", "latitude": 18.499, "longitude": 73.862,
            "no_of_sales": "1000", "billed_in_history": None, "distance": 239.1}
    nonobtb = {"name": "Shanti Sanitation", "address": "904 Bhawani Peth", "city": "Pune",
               "state_desc": "Maharashtra", "phone_no": "9823331144",
               "email": "x@gmail.com", "invoice_no": "SIDRA/2223", "no_of_sales": "432"}
    a = dealers.parse_orientbell_store(obtb)
    b = dealers.parse_orientbell_store(nonobtb)
    assert a["name"] == "OBTB Pune" and a["pincode"] == "411037" and a["email"] is None
    assert b["name"] == "Shanti Sanitation" and b["state"] == "Maharashtra" and b["email"] == "x@gmail.com"
    for d in (a, b):
        blob = json.dumps(d)
        assert "no_of_sales" not in d and "invoice_no" not in blob and "billed_in_history" not in d


def test_singleinterface_crawler_follows_index_and_parses_details(conn):
    from material_bank.fetch import FetchResult
    base = "https://stores.hrjohnsonindia.com"
    SITEMAP_INDEX = (f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                     f'<sitemap><loc>{base}/sm-stores.xml</loc></sitemap></sitemapindex>')
    URLSET = (f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
              f'<url><loc>{base}/tiles-shop/delhi/badarpur/house-of-johnson--4OW/home</loc></url>'
              f'<url><loc>{base}/location/delhi/</loc></url></urlset>')

    class SIFetcher:
        def get(self, url):
            body = ""
            if url.endswith("/sitemap.xml"):
                body = SITEMAP_INDEX
            elif url.endswith("/sm-stores.xml"):
                body = URLSET
            elif url.endswith("/home"):
                body = SI_HTML   # reuse the microdata fixture above
            return FetchResult(requested_url=url, status_code=200, text=body, final_url=url)

    conn.execute("INSERT OR IGNORE INTO suppliers (domain,brand,status) VALUES "
                 "('hrjohnsonindia.com','H&R Johnson','active')")
    conn.commit()
    stats = dealers.harvest_singleinterface(conn, SIFetcher(), domain="hrjohnsonindia.com")
    assert stats["detail_pages"] == 1 and stats["dealers_added"] == 1  # /location/ not crawled as detail
    r = conn.execute("SELECT name, city, state FROM dealers WHERE supplier_domain=?",
                     ("hrjohnsonindia.com",)).fetchone()
    assert r["name"] == "House of Johnson Experience Centre" and r["city"] == "New Delhi"


def test_store_strips_leaked_html(conn):
    rows = [{"name": "Satish <b>Enterprises</b>", "city": "Amalapuram", "state": "Andhra Pradesh",
             "pincode": "533201", "phone": "<p>8639751848</p>"}]
    dealers.store_dealers(conn, "kajariaceramics.com", rows)
    r = conn.execute("SELECT name, phone FROM dealers WHERE supplier_domain=?",
                     ("kajariaceramics.com",)).fetchone()
    assert r["name"] == "Satish Enterprises" and r["phone"] == "8639751848"
    # HTML entities are decoded too (&amp; -> &)
    dealers.store_dealers(conn, "hrjohnsonindia.com",
                          [{"name": "H &amp; R Johnson", "city": "Latur", "pincode": "413517"}])
    assert conn.execute("SELECT name FROM dealers WHERE supplier_domain='hrjohnsonindia.com'"
                        ).fetchone()[0] == "H & R Johnson"


def test_store_and_derive_regions(conn):
    rows = [
        dealers.parse_kajaria_store({"name": "A", "city": "Pune", "state": "Maharashtra",
                                     "pincode": "411001", "mobile": "9000000001"}),
        dealers.parse_kajaria_store({"name": "B", "city": "Delhi", "state": "Delhi",
                                     "pincode": "110001", "mobile": "9000000002"}),
        dealers.parse_kajaria_store({"name": "A", "city": "Pune", "state": "Maharashtra",
                                     "pincode": "411001", "mobile": "9000000001"}),  # dup
    ]
    added = dealers.store_dealers(conn, "kajariaceramics.com", rows)
    assert added == 2                              # duplicate ignored
    summary = dealers.derive_regions(conn, "kajariaceramics.com")
    assert summary["dealers"] == 2 and summary["states"] == 2 and summary["pan_india"] == 0
    r = conn.execute("SELECT states_served, cities_served, dealer_count FROM suppliers "
                     "WHERE domain=?", ("kajariaceramics.com",)).fetchone()
    assert set(json.loads(r["states_served"])) == {"Maharashtra", "Delhi"}
    assert r["dealer_count"] == 2
