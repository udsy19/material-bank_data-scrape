"""Deterministic company-info extraction: JSON-LD, links, regex, provenance."""

from material_bank.company_extract import extract_company, merge_company

JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"WebSite","url":"https://somany.example"},
  {"@type":"Organization","name":"Somany Ceramics","legalName":"Somany Ceramics Limited",
   "url":"https://somany.example","logo":{"@type":"ImageObject","url":"https://somany.example/logo.png"},
   "telephone":"+91 124 4623000","email":"info@somany.example","foundingDate":"1968-01-01",
   "address":{"@type":"PostalAddress","streetAddress":"Plot 4, Sector 32","addressLocality":"Gurugram",
              "addressRegion":"Haryana","postalCode":"122001","addressCountry":"IN"},
   "sameAs":["https://www.instagram.com/somany","https://www.linkedin.com/company/somany"]}
]}
</script></head><body></body></html>
"""


def test_extracts_organization_jsonld():
    c = extract_company(JSONLD_PAGE, "https://somany.example/")
    assert c["legal_name"] == "Somany Ceramics Limited"
    assert "9124" not in "".join(c["phones"])           # not mangled
    assert c["phones"] == ["1244623000"]                 # landline normalized
    assert c["emails"] == ["info@somany.example"]
    assert c["city"] == "Gurugram" and c["state"] == "Haryana" and c["pincode"] == "122001"
    assert "Plot 4" in c["address"]
    assert c["logo_url"].endswith("logo.png")
    assert c["year_established"] == "1968"
    assert c["social"] == {"instagram": "https://www.instagram.com/somany",
                           "linkedin": "https://www.linkedin.com/company/somany"}
    assert c["_provenance"]["legal_name"]["basis"] == "observed"
    assert c["_provenance"]["legal_name"]["source"] == "https://somany.example/"


FOOTER_PAGE = """
<html><body><footer>
  Kajaria Ceramics Limited &nbsp; CIN: L26924HR1985PLC056150 &nbsp; GSTIN: 06AABCK0710M1Z8
  <a href="mailto:care@kajaria.example">care@kajaria.example</a>
  Toll Free: 1800-419-2077 &nbsp; Tel: <a href="tel:+911244623000">0124-4623000</a>
  Regd. Office: A-2, Ist Floor, Mangoli, New Delhi - 110033
  Personal contact: Rajesh Kumar 9876543210 (this name must NOT be captured)
</footer></body></html>
"""


def test_extracts_from_footer_regex_and_masks_taxids():
    c = extract_company(FOOTER_PAGE, "https://kajaria.example/contact-us")
    assert c["gstin"] == "06AABCK0710M1Z8"
    assert c["cin"] == "L26924HR1985PLC056150"
    # phones: toll-free + landline captured; the tax-id digit runs are NOT misread
    assert "1800419277" not in c["phones"]               # tollfree stays grouped form-normalized
    assert any(p.startswith("1800") for p in c["phones"])
    assert "1244623000" in c["phones"]
    # a person's name is never stored as a field
    assert "legal_name" not in c or "Rajesh" not in c.get("legal_name", "")


def test_no_org_returns_provenance_only_dict():
    c = extract_company("<html><body>nothing here</body></html>", "https://x.example/")
    assert c == {"_provenance": {}} or c.get("_provenance") == {}


def test_pincode_not_read_from_phone_digits():
    # a bare 10-digit phone must not yield a 6-digit "pincode"
    html = '<body>Call 9876543210 for orders</body>'
    c = extract_company(html, "https://x.example/")
    assert "pincode" not in c
    assert "9876543210" in c.get("phones", [])


def test_merge_unions_channels_keeps_best_scalar():
    home = extract_company(JSONLD_PAGE, "https://somany.example/")
    contact = extract_company(FOOTER_PAGE, "https://somany.example/contact")
    m = merge_company([home, contact])
    # legal_name from the higher-confidence JSON-LD survives
    assert m["legal_name"] == "Somany Ceramics Limited"
    # phones unioned across both pages
    assert "1244623000" in m["phones"] and any(p.startswith("1800") for p in m["phones"])
    assert m["gstin"] == "06AABCK0710M1Z8"                # only present on contact page
    assert "instagram" in m["social"]
