"""Supplier discovery — resolve a brand's own website from a directory profile.

First use: the India Design ID exhibitor list (352 design brands). Each entry is
a brand name + its indiadesignid.com *profile* page; the brand's own site is
linked on that page amid boilerplate/social noise. ``resolve_site`` picks it out
deterministically (prefer a link whose host matches the brand's name tokens,
else the first non-noise, non-social external link). This is the seed of the
Phase-F discovery agent — new suppliers become registry rows, never code.
"""

from __future__ import annotations

import csv
import re

from .db import normalize_domain

# hosts that appear on profile pages but are never the brand's own site:
# the directory itself, ticketing, analytics/trackers, CDNs, fonts, boilerplate.
_NOISE = ("indiadesignid.com", "gmpg.org", "bookmyshow.com", "gstatic.", "w3.org",
          "schema.org", "jquery", "wordpress.org", "wp.com", "gravatar.com", "fonts.",
          "cdn.", "youtu", "vimeo.com", "maps.", "googletagmanager", "google-analytics",
          "googleapis", "googleadservices", "googlesyndication", "doubleclick",
          "google.com", "cloudflare", "jsdelivr", "unpkg", "cdnjs", "bit.ly",
          "amazon.com", "amzn", "linktr.ee", "calendly", "typekit", "adobe.com")
_SOCIAL = ("instagram.com", "facebook.com", "twitter.com", "x.com", "linkedin.com",
           "pinterest.", "wa.me", "whatsapp", "t.me", "threads.net", "behance.net")
_HREF_RE = re.compile(r'href="(https?://[^"]+)"', re.I)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _is_candidate(host: str) -> bool:
    h = host.lower()
    return bool(h) and not any(n in h for n in _NOISE) and not any(s in h for s in _SOCIAL)


def resolve_site(html: str, brand: str = "") -> str | None:
    """Best-guess brand domain from a profile page's outbound links."""
    hosts: list[str] = []
    for u in _HREF_RE.findall(html or ""):
        h = normalize_domain(u)
        if _is_candidate(h) and h not in hosts:
            hosts.append(h)
    if not hosts:
        return None
    # prefer a host that shares a token with the brand name (strong signal)
    btok = set(_TOKEN_RE.findall(brand.lower()))
    for h in hosts:
        if btok & set(_TOKEN_RE.findall(h)):
            return h
    return hosts[0]


def resolve_exhibitors(in_csv: str, out_csv: str, fetcher, *, limit: int | None = None) -> dict:
    """Read (brand, profile_url), fetch each profile, resolve the brand domain,
    write (brand, domain, profile_url). Resumable-friendly: unresolved rows kept
    with an empty domain so they can be retried."""
    rows = list(csv.DictReader(open(in_csv)))
    if limit:
        rows = rows[:limit]
    out, resolved = [], 0
    for r in rows:
        dom = ""
        resp = fetcher.get(r["profile_url"])
        if resp.ok and resp.text:
            dom = resolve_site(resp.text, r["brand"]) or ""
        if dom:
            resolved += 1
        out.append({"brand": r["brand"], "domain": dom, "profile_url": r["profile_url"]})
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["brand", "domain", "profile_url"])
        w.writeheader(); w.writerows(out)
    return {"total": len(rows), "resolved": resolved, "out": out_csv}


def main(argv=None) -> int:
    import argparse
    import json
    import sys

    from .fetch import Fetcher
    ap = argparse.ArgumentParser(prog="mb-discover")
    ap.add_argument("--in", dest="inp", default="data/seed/india_design_id.csv")
    ap.add_argument("--out", default="data/seed/india_design_id_resolved.csv")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    stats = resolve_exhibitors(args.inp, args.out, Fetcher(min_interval=1.0, raw_dir=None),
                               limit=args.limit)
    print(json.dumps(stats), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
