"""Regressions for false-'unreachable' bugs found in the first live run.

Root causes: (1) probe only tried the bare domain when many India sites serve
only on www; (2) the Fetcher treated a transient connection exception as
terminal; (3) _host stripped the 'w'/'.' charset instead of the 'www.' prefix.
"""

from material_bank.fetch import Fetcher, _host
from material_bank.models import ProbeStatus, ScrapeTier
from material_bank.probe import classify

from .conftest import FakeFetcher

ROBOTS_OK = {"status": 200, "text": "User-agent: *\nAllow: /"}


def test_host_strips_www_prefix_not_charset():
    assert _host("https://wonderfloor.co.in/") == "wonderfloor.co.in"   # keeps leading w
    assert _host("https://welspunflooring.com/") == "welspunflooring.com"
    assert _host("https://www.kajaria.com/") == "kajaria.com"           # prefix stripped


class _FlakySession:
    """Raises once, then returns 200 — simulates a transient blip."""

    def __init__(self):
        self.n = 0

    def get(self, url, **kw):
        self.n += 1
        if self.n == 1:
            raise ConnectionError("temporary reset")

        class R:
            status_code = 200
            text = "ok"
            content = b"ok"
            headers = {}
            url = "https://a.com/"
        return R()


def test_fetcher_retries_transient_connection_error():
    f = Fetcher(session=_FlakySession(), raw_dir=None, min_interval=0,
                sleep=lambda d: None, max_retries=3)
    res = f.get("https://a.com/")
    assert res.ok and res.error is None   # recovered; error cleared


class _WwwOnlyFetcher(FakeFetcher):
    """Bare domain errors; only the www host resolves."""

    def get(self, url):
        if url.startswith("https://d.com/"):     # bare host fails
            from material_bank.fetch import FetchResult
            self.calls.append(url)
            return FetchResult(requested_url=url, error="CertificateVerifyError")
        return super().get(url)


def test_probe_www_fallback_avoids_false_unreachable():
    f = _WwwOnlyFetcher(
        landing={"status": 200, "text": "<html>home</html>"},  # served for www.d.com
        robots=ROBOTS_OK,
        products_json={"status": 404},
        woo={"status": 404},
        sitemap={"status": 404},
    )
    r = classify("d.com", f)
    assert r.probe_status is not ProbeStatus.UNREACHABLE
    assert r.scrape_tier is ScrapeTier.TIER3          # classified via www
    assert r.final_host == "www.d.com"
    assert any(e["result"] == "www-fallback" for e in r.log)
