"""Supplier discovery: resolving a brand's own site from a directory profile."""

from material_bank.discover import resolve_site

PROFILE = """
<html><body>
<a href="https://twitter.com/IndiaDesignID">twitter</a>
<a href="https://gmpg.org/xfn/11">profile</a>
<a href="https://in.bookmyshow.com/events/id-2026/ET1">tickets</a>
<a href="https://www.instagram.com/indiadesignid/">insta</a>
<a href="https://www.aclassmarble.co.in/">Visit website</a>
</body></html>
"""


def test_resolve_site_skips_noise_and_social():
    assert resolve_site(PROFILE, "A-Class Marble") == "aclassmarble.co.in"


def test_resolve_prefers_brand_name_token_match():
    html = ('<a href="https://somevendor.com">x</a>'
            '<a href="https://altrove.in/">Altrove</a>')
    assert resolve_site(html, "Altrove") == "altrove.in"      # token match wins over first


def test_resolve_none_when_only_noise():
    html = '<a href="https://instagram.com/x">i</a><a href="https://indiadesignid.com/y">p</a>'
    assert resolve_site(html, "Brand") is None
