"""Deterministic company-info extraction from a supplier's OWN website.

The "who supplies it" layer. Pulls company/procurement fields — legal name,
business phones/emails, registered address, GSTIN/CIN, social, logo — out of the
markup a supplier already publishes, with provenance. No LLM: measured fields
come from deterministic extraction only (prime directive).

Ordered strategy, first hit per field wins but lower tiers still corroborate:
  1. schema.org JSON-LD  Organization / LocalBusiness / Corporation / Store
  2. mailto: / tel: links  (highest-confidence channel signal)
  3. footer / contact-page regex  (GSTIN, CIN, phone, email, pincode)

Legal guardrails (see VISION/legal read): this runs ONLY against the supplier's
own registered domain; it never extracts an individual person's name, and every
value carries {source, basis, confidence, observed_at}. GSTIN/CIN spans are
masked before the phone/pincode scan so a tax id's digit runs aren't misread.
"""

from __future__ import annotations

import json
import re

# ── regexes (India-specific), each anchored to avoid mid-digit-run matches ────
_RE_MOBILE = re.compile(r"(?<!\d)(?:\+91[-\s]?|91[-\s]?|0)?([6-9]\d{9})(?!\d)")
_RE_LANDLINE = re.compile(r"(?<!\d)(?:\+91[-\s]?)?\(?0(\d{2,4})\)?[-\s]?(\d{6,8})(?!\d)")
_RE_TOLLFREE = re.compile(r"\b(1[89]00[-\s]?\d{2,4}[-\s]?\d{3,4})\b")
_RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_RE_GSTIN = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
_RE_CIN = re.compile(r"\b([LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\b")
_RE_PIN_LABELLED = re.compile(r"(?:PIN\s*(?:CODE)?[:\-\s]*)([1-9]\d{5})\b", re.I)
_RE_PIN_BARE = re.compile(r"(?<!\d)([1-9]\d{5})(?!\d)")
_RE_TEL_HREF = re.compile(r'href=["\']tel:([^"\']+)', re.I)
_RE_MAIL_HREF = re.compile(r'href=["\']mailto:([^"\'?]+)', re.I)
_RE_YEAR = re.compile(r"\b(?:since|established|estd\.?|est\.?)\s*(?:in\s*)?(\d{4})\b", re.I)
_RE_JSONLD = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)

_SOCIAL_HOSTS = {
    "facebook.com": "facebook", "instagram.com": "instagram", "twitter.com": "twitter",
    "x.com": "twitter", "linkedin.com": "linkedin", "youtube.com": "youtube",
    "pinterest.com": "pinterest",
}
# email local-parts that clearly denote a business channel (kept at higher confidence)
_ROLE_LOCALS = {
    "info", "sales", "care", "customercare", "support", "orders", "order", "contact",
    "enquiry", "enquiries", "inquiry", "hello", "help", "helpdesk", "marketing",
    "business", "dealer", "dealers", "export", "exports", "admin", "connect",
    "mail", "office", "reachus", "feedback", "service", "services", "corporate",
}
_ADDR_CONTEXT = re.compile(
    r"regd|register|corporate|office|road|nagar|marg|street|plot|floor|tower|"
    r"industrial|estate|sector|phase|building|complex", re.I)
# cue that a nearby number belongs to a named individual, not the company channel
_PERSONAL_CUE = re.compile(r"\b(mr|ms|mrs|contact person|personal|attn)\b\.?[^\d]*$", re.I)


def _norm_phone(raw: str) -> str | None:
    """Keep only the digits that make an Indian number; drop noise."""
    d = re.sub(r"[^\d]", "", raw)
    if d.startswith("91") and len(d) == 12:
        d = d[2:]
    if d.startswith("0") and len(d) in (11, 12):
        d = d.lstrip("0")
    if len(d) == 10 and d[0] in "6789":
        return d
    if d.startswith(("1800", "1860")) and 10 <= len(d) <= 12:
        return d
    if 10 <= len(d) <= 11:                     # landline w/ std code
        return d
    return None


def _jsonld_nodes(html: str) -> list:
    out = []
    for m in _RE_JSONLD.finditer(html or ""):
        try:
            out.append(json.loads(m.group(1).strip(), strict=False))
        except (ValueError, TypeError):
            pass
    return out


_ORG_TYPES = {"organization", "localbusiness", "corporation", "store", "onlinestore"}


def _find_org(node):
    """Depth-first search for an Organization-family node (walks @graph/lists)."""
    if isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and x.lower() in _ORG_TYPES for x in types):
            return node
        for v in node.values():
            r = _find_org(v)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_org(v)
            if r is not None:
                return r
    return None


def _social(urls) -> dict:
    out: dict[str, str] = {}
    for u in urls if isinstance(urls, list) else [urls]:
        if not isinstance(u, str):
            continue
        for host, name in _SOCIAL_HOSTS.items():
            if host in u and name not in out:
                out[name] = u
    return out


def _from_jsonld(org: dict, out: dict, prov: dict, url: str) -> None:
    def put(field, value, conf):
        if value and field not in out:
            out[field] = value
            prov[field] = {"source": url, "basis": "observed", "confidence": conf}

    put("legal_name", (org.get("legalName") or org.get("name") or "").strip() or None, 0.95)
    tel = _norm_phone(str(org.get("telephone"))) if org.get("telephone") else None
    if tel:
        out.setdefault("phones", [])
        if tel not in out["phones"]:
            out["phones"].append(tel)
            prov.setdefault("phones", {"source": url, "basis": "observed", "confidence": 0.95})
    email = org.get("email")
    if isinstance(email, str) and _RE_EMAIL.fullmatch(email.strip()):
        out.setdefault("emails", [])
        if email.strip().lower() not in out["emails"]:
            out["emails"].append(email.strip().lower())
            prov.setdefault("emails", {"source": url, "basis": "observed", "confidence": 0.95})
    addr = org.get("address")
    addr = addr[0] if isinstance(addr, list) and addr else addr
    if isinstance(addr, dict):
        parts = [addr.get(k) for k in ("streetAddress", "addressLocality",
                                       "addressRegion", "postalCode")]
        full = ", ".join(str(p) for p in parts if p)
        put("address", full or None, 0.95)
        put("city", (addr.get("addressLocality") or "").strip() or None, 0.95)
        put("state", (addr.get("addressRegion") or "").strip() or None, 0.9)
        pin = str(addr.get("postalCode") or "").strip()
        if _RE_PIN_BARE.fullmatch(pin):
            put("pincode", pin, 0.95)
    logo = org.get("logo")
    logo = logo.get("url") if isinstance(logo, dict) else logo
    put("logo_url", logo if isinstance(logo, str) else None, 0.9)
    put("year_established", (str(org.get("foundingDate"))[:4]
                             if org.get("foundingDate") else None), 0.9)
    soc = _social(org.get("sameAs"))
    if soc:
        out.setdefault("social", {}).update(soc)
        prov.setdefault("social", {"source": url, "basis": "observed", "confidence": 0.9})


def extract_company(html: str, url: str) -> dict:
    """Return {field: value, ..., '_provenance': {field: {...}}} for one page.

    Only company-level fields; never an individual's name. Multi-valued channels
    (phones, emails, social) accumulate; scalars keep the highest-tier hit.
    """
    html = html or ""
    out: dict = {}
    prov: dict = {}

    # tier 1 — JSON-LD Organization
    for node in _jsonld_nodes(html):
        org = _find_org(node)
        if org:
            _from_jsonld(org, out, prov, url)

    # tier 2 — mailto:/tel: links (explicit channels the site itself wired up)
    phones = set(out.get("phones", []))
    emails = set(out.get("emails", []))
    for raw in _RE_TEL_HREF.findall(html):
        p = _norm_phone(raw)
        if p:
            phones.add(p)
    for raw in _RE_MAIL_HREF.findall(html):
        e = raw.strip().lower()
        if _RE_EMAIL.fullmatch(e):
            emails.add(e)
    if phones - set(out.get("phones", [])):
        prov.setdefault("phones", {"source": url, "basis": "observed", "confidence": 0.9})
    if emails - set(out.get("emails", [])):
        prov.setdefault("emails", {"source": url, "basis": "observed", "confidence": 0.9})

    # tier 3 — regex over text. Mask GSTIN/CIN spans first so their digit runs
    # aren't misread as phones/pincodes.
    masked = html
    gstins = _RE_GSTIN.findall(html)
    cins = _RE_CIN.findall(html)
    if gstins and "gstin" not in out:
        out["gstin"] = gstins[0]
        prov["gstin"] = {"source": url, "basis": "observed", "confidence": 0.8}
    if cins and "cin" not in out:
        out["cin"] = cins[0]
        prov["cin"] = {"source": url, "basis": "observed", "confidence": 0.85}
    for span in set(gstins) | set(cins):
        masked = masked.replace(span, " ")
    # drop <script>/<style> bodies so analytics/JS numbers aren't read as phones
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", masked, flags=re.S | re.I)
    for raw in _RE_TOLLFREE.findall(text):
        p = _norm_phone(raw)
        if p:
            phones.add(p)
    for m in _RE_MOBILE.finditer(text):
        pre = text[max(0, m.start() - 25):m.start()]
        if _PERSONAL_CUE.search(pre):        # "Mr. / contact person / personal" -> skip
            continue
        p = _norm_phone(m.group(1))
        if p:
            phones.add(p)
    for m in _RE_LANDLINE.finditer(text):
        p = _norm_phone(m.group(0))
        if p:
            phones.add(p)
    for e in _RE_EMAIL.findall(text):
        e = e.strip().lower()
        # skip asset/tracking emails masquerading (e.g. sentry, example)
        if not e.endswith((".png", ".jpg", ".gif", ".webp")) and "example.com" not in e:
            emails.add(e)
    for m in _RE_PIN_LABELLED.finditer(masked):
        if "pincode" not in out:
            out["pincode"] = m.group(1)
            prov["pincode"] = {"source": url, "basis": "observed", "confidence": 0.75}
    if "year_established" not in out:
        y = _RE_YEAR.search(masked)
        if y and 1900 <= int(y.group(1)) <= 2026:
            out["year_established"] = y.group(1)
            prov["year_established"] = {"source": url, "basis": "observed", "confidence": 0.6}

    if phones:
        out["phones"] = sorted(phones)
    if emails:
        # keep only same-domain-ish or role-based business emails; that keeps
        # sales@/info@ and drops third-party/tracking addresses
        biz = [e for e in emails if e.split("@")[0] in _ROLE_LOCALS
               or e.split("@")[1].split(".")[0] in url]
        out["emails"] = sorted(biz or emails)
        prov.setdefault("emails", {"source": url, "basis": "observed", "confidence": 0.7})

    out["_provenance"] = prov
    return out


def merge_company(pages: list[dict]) -> dict:
    """Merge per-page extractions into one supplier record (union channels,
    keep the highest-confidence scalar). Input: list of extract_company results."""
    merged: dict = {}
    prov: dict = {}
    for page in pages:
        page_prov = page.get("_provenance", {})
        for k, v in page.items():
            if k == "_provenance" or v in (None, "", [], {}):
                continue
            if k in ("phones", "emails"):
                merged.setdefault(k, [])
                for item in v:
                    if item not in merged[k]:
                        merged[k].append(item)
                prov.setdefault(k, page_prov.get(k))
            elif k == "social":
                merged.setdefault("social", {}).update(v)
                prov.setdefault("social", page_prov.get("social"))
            elif k not in merged or (page_prov.get(k, {}).get("confidence", 0)
                                     > prov.get(k, {}).get("confidence", 0)):
                merged[k] = v
                prov[k] = page_prov.get(k)
    merged["_provenance"] = {k: v for k, v in prov.items() if v}
    return merged
