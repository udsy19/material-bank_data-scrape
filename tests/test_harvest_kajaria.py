"""Kajaria harvester: static PDP parse + pdfplumber technical-PDF spec extraction."""

import pytest

from material_bank import db as db_mod
from material_bank.fetch import FetchResult
from material_bank.harvest import kajaria


def test_slug_and_pdp_filter():
    assert kajaria.slug_of("https://www.kajariaceramics.com/products/terrazo-brown/") == "terrazo-brown"
    assert kajaria.is_pdp("https://www.kajariaceramics.com/products/x")
    assert not kajaria.is_pdp("https://www.kajariaceramics.com/blog/y")
    assert not kajaria.is_pdp("https://www.kajariaceramics.com/products/x.pdf")


def test_specs_from_real_pdf_text():
    # the exact text pdfplumber pulls from a Kajaria technical PDF
    txt = "800x2400mm (15mm) 1 pc. 1.92 sq. mtr. / 20.666 sq.ft. 68 Kg (Avg)\n162 163"
    s = kajaria.specs_from_text(txt)
    assert s["size_mm"] == "800x2400" and s["thickness_mm"] == 15.0
    assert s["coverage_sqft_per_box"] == 20.666


def test_specs_from_text_no_match():
    assert kajaria.specs_from_text("no dimensions here") == {}


PDP_WITH_PDF = (
    '<html><head><title>Terrazo Brown</title></head><body>'
    '<h1>Terrazo Brown</h1>'
    '<img src="https://www.kajariaceramics.com/storage/product/KPVT00131_b.jpg">'
    '<a href="https://www.kajariaceramics.com/storage/pdf/technical-vitronite-80x240.pdf">tech</a>'
    '</body></html>'
)
PDP_NO_PDF = (
    '<html><head><title>Morroco Gold</title></head><body><h1>Morroco Gold</h1>'
    '<img src="https://www.kajariaceramics.com/storage/product/PF02049_b.jpg"></body></html>'
)


class _PdfFetcher:
    """Serves fake PDF bytes; parse_technical_pdf handles the pdfplumber part,
    so we monkeypatch that to isolate PDP parsing from real PDF decoding."""
    def get(self, url):
        return FetchResult(requested_url=url, status_code=200, content=b"%PDF-fake", final_url=url)


def test_parse_pdp_with_pdf_specs(monkeypatch):
    monkeypatch.setattr(kajaria, "parse_technical_pdf",
                        lambda content: {"size_mm": "800x2400", "coverage_sqft_per_box": 20.666})
    p = kajaria.parse_pdp(PDP_WITH_PDF, "https://k/products/terrazo-brown", _PdfFetcher(), {})
    assert p.brand == "Kajaria" and p.title == "Terrazo Brown" and p.sku == "terrazo-brown"
    assert p.size_mm == "800x2400" and p.coverage_sqft_per_box == 20.666
    assert p.image_url.endswith("KPVT00131_b.jpg")
    assert p.source_url == "https://k/products/terrazo-brown"
    # price_unit + finish genuinely unknown -> flagged missing, never faked
    assert "price_unit" in p.missing and "finish" in p.missing


def test_parse_pdp_specs_only_when_no_pdf():
    p = kajaria.parse_pdp(PDP_NO_PDF, "https://k/products/morroco-gold", _PdfFetcher(), {})
    assert p.title == "Morroco Gold" and p.image_url.endswith("PF02049_b.jpg")
    assert p.size_mm is None and "size_mm" in p.missing and "coverage_sqft_per_box" in p.missing


def test_pdf_cache_avoids_refetch(monkeypatch):
    calls = {"n": 0}
    def counting(content):
        calls["n"] += 1
        return {"size_mm": "800x2400", "coverage_sqft_per_box": 20.666}
    monkeypatch.setattr(kajaria, "parse_technical_pdf", counting)
    cache = {}
    kajaria.parse_pdp(PDP_WITH_PDF, "https://k/products/a", _PdfFetcher(), cache)
    kajaria.parse_pdp(PDP_WITH_PDF, "https://k/products/b", _PdfFetcher(), cache)
    assert calls["n"] == 1  # same PDF parsed once, reused for both products


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db")
    db_mod.migrate(c)
    yield c
    c.close()


def test_harvest_stores_products(conn, monkeypatch):
    monkeypatch.setattr(kajaria, "parse_technical_pdf",
                        lambda content: {"size_mm": "800x2400", "coverage_sqft_per_box": 20.666})
    sitemap = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               '<url><loc>https://www.kajariaceramics.com/products/terrazo-brown</loc></url>'
               '<url><loc>https://www.kajariaceramics.com/blog/x</loc></url></urlset>')

    class _F:
        def get(self, url):
            if "sitemap" in url:
                body = sitemap
            elif url.endswith(".pdf"):
                return FetchResult(requested_url=url, status_code=200, content=b"%PDF-x", final_url=url)
            else:
                body = PDP_WITH_PDF
            return FetchResult(requested_url=url, status_code=200, text=body, final_url=url)

    stats = kajaria.harvest_kajaria(conn, _F())
    assert stats["candidates"] == 1 and stats["products"] == 1 and stats["with_coverage"] == 1
    row = conn.execute("SELECT coverage_sqft_per_box FROM products WHERE brand='Kajaria'").fetchone()
    assert row["coverage_sqft_per_box"] == 20.666
