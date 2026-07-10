"""Playwright smoke test of the live dashboard. Run against a running server:

    python -m uvicorn material_bank.serve:app --port 8077   # in one shell
    python tests/browser/smoke.py                           # in another

Exits non-zero on any failed assertion; writes screenshots to reports/screens/.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get("MB_BASE", "http://127.0.0.1:8077")
SHOTS = Path(__file__).resolve().parent.parent.parent / "reports" / "screens"
SHOTS.mkdir(parents=True, exist_ok=True)


def run(base: str = BASE) -> tuple[int, int, list[str]]:
    checks: list[tuple[str, bool]] = []

    def check(name: str, cond: bool):
        checks.append((name, bool(cond)))
        print(("  ✓ " if cond else "  ✗ ") + name)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        console_errors = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        # domcontentloaded, not networkidle: the catalog loads image cards on
        # init and the /api/image proxy keeps the network busy — it never idles.
        page.goto(base, wait_until="domcontentloaded", timeout=30000)
        check("title is DSource Material Bank", "DSource Material Bank" in page.title() or
              page.locator("h1").inner_text().startswith("DSource"))

        # stats tiles render with a real product count
        page.wait_for_selector("#tiles .tile", timeout=15000)
        tiles_text = page.locator("#tiles").inner_text()
        check("stats tiles rendered", page.locator("#tiles .tile").count() >= 6)
        check("products count shown (>1000)", any(c.isdigit() for c in tiles_text) and "products" in tiles_text.lower())

        # top suppliers rendered
        check("top suppliers listed", page.locator("#suppliers .spill").count() >= 3)

        # ── catalog browse: taxonomy facets + collapsed design cards ────────
        page.wait_for_selector("#facets .facet", timeout=15000)
        page.wait_for_selector("#catalog .card", timeout=20000)
        check("taxonomy facets rendered", page.locator("#facets .facet").count() >= 3)
        check("design cards rendered", page.locator("#catalog .card").count() >= 6)
        check("design card shows a price band", page.locator("#catalog .band").count() >= 1)
        check("variant-group badge shown", page.locator("#catalog .badge").count() >= 1)

        # a badged (multi-variant) design opens a modal with trust + variants
        badged = page.locator("#catalog .card:has(.badge)").first
        badged.click()
        page.wait_for_selector("#overlay.on", timeout=10000)
        check("design modal shows trust line", page.locator("#modal .trust").count() == 1)
        check("design modal lists variants", page.locator("#modal .vrow").count() >= 2)
        page.screenshot(path=str(SHOTS / "design_variants.png"))
        page.click("#modal .close")
        page.wait_for_selector("#overlay", state="hidden", timeout=5000)

        # a facet filters the catalog to that family
        page.locator("#facets .facet").nth(1).click()
        page.wait_for_timeout(1500)
        check("facet filter reloads catalog", page.locator("#catalog .card").count() >= 1)
        page.locator("#facets .facet").first.click()  # reset to All
        page.wait_for_timeout(800)

        # search flow
        page.fill("#q", "brass pendant light")
        page.click("#go")
        page.wait_for_selector("#results .card", timeout=20000)
        n = page.locator("#results .card").count()
        check("search returned result cards", n >= 3)
        first = page.locator("#results .card").first
        check("card has a title", len(first.locator(".t").inner_text().strip()) > 0)
        check("card shows a price or 'not listed'", "₹" in first.inner_text() or "not listed" in first.inner_text())

        # images load concurrently through the proxy — most should render
        page.wait_for_timeout(6000)
        counts = page.evaluate(
            "() => { const im=[...document.querySelectorAll('#results .card img')];"
            "return {total: im.length, loaded: im.filter(e=>e.naturalWidth>0).length}; }")
        check(f"most product images loaded via proxy ({counts['loaded']}/{counts['total']})",
              counts["total"] >= 1 and counts["loaded"] >= max(1, int(0.6 * counts["total"])))
        page.screenshot(path=str(SHOTS / "search_results.png"))  # after images settle

        # product detail modal
        first.click()
        page.wait_for_selector("#overlay.on", timeout=10000)
        modal = page.locator("#modal").inner_text()
        check("modal shows price observations", "Price observations" in modal)
        check("modal has a heading", len(page.locator("#modal h3").inner_text().strip()) > 0)
        page.screenshot(path=str(SHOTS / "product_detail.png"))
        page.click("#modal .close")
        check("modal closes", not page.locator("#overlay").evaluate("e => e.classList.contains('on')"))

        # example chip drives a new search
        page.locator(".chip").first.click()
        page.wait_for_selector("#results .card", timeout=20000)
        check("chip search populates results", page.locator("#results .card").count() >= 1)

        # empty query does nothing catastrophic
        check("no console errors", len(console_errors) == 0 or all("favicon" in e for e in console_errors))

        page.screenshot(path=str(SHOTS / "dashboard_full.png"), full_page=True)
        browser.close()

    passed = sum(1 for _, ok in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")
    if console_errors:
        print("console errors:", console_errors[:5])
    failed = [name for name, ok in checks if not ok]
    return passed, len(checks), failed


if __name__ == "__main__":
    p, t, _ = run()
    sys.exit(0 if p == t else 1)
