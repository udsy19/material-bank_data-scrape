"""Probe CLI: resumable, capped-concurrency, coverage-reporting.

Resumability: ``--all`` skips rows already probed (``probed_at`` set); a crash at
domain 60/175 resumes from 61. ``--force`` re-probes. Concurrency runs the
network-bound classify() across domains; each domain gets its own Fetcher, so
the ~1 req/2s spacing is preserved per domain. DB writes happen on the main
thread (SQLite connections are not shared across threads).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import db
from .fetch import Fetcher
from .models import ProbeResult
from .probe import classify, write_result

_REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = _REPO_ROOT / "reports"


def select_domains(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    category: str | None = None,
    domain: str | None = None,
    status: str = "active",
) -> list[str]:
    """Domains to probe. Skips already-probed rows unless ``force``."""
    where = ["status = ?"]
    params: list[object] = [status]
    if domain:
        where.append("domain = ?")
        params.append(db.normalize_domain(domain))
    if category:
        where.append("categories LIKE ?")
        params.append(f"%{category}%")
    if not force:
        where.append("probed_at IS NULL")
    sql = f"SELECT domain FROM suppliers WHERE {' AND '.join(where)} ORDER BY domain"
    return [r["domain"] for r in conn.execute(sql, params)]


def probe_domains(
    conn: sqlite3.Connection,
    domains: list[str],
    *,
    workers: int = 8,
    fetcher_factory: Callable[[], Fetcher] = Fetcher,
    on_result: Callable[[ProbeResult], None] | None = None,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    if not domains:
        return results

    def work(d: str) -> ProbeResult:
        return classify(d, fetcher_factory())

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(work, d): d for d in domains}
        for fut in as_completed(futures):
            d = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # never let one domain kill the run
                from .models import ProbeStatus

                result = ProbeResult(domain=d, probe_status=ProbeStatus.ERROR,
                                     probed_at=db.now_iso())
                result.note("probe", "exception", error=f"{type(exc).__name__}: {exc}")
            write_result(conn, result)   # main thread only
            results.append(result)
            if on_result:
                on_result(result)
    return results


def coverage_report(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT * FROM suppliers").fetchall()
    probed = [r for r in rows if r["probed_at"]]
    tiers = Counter(r["scrape_tier"] for r in probed)
    statuses = Counter(r["probe_status"] for r in probed)
    prices = Counter(r["price_published"] for r in probed)
    sku_total = sum(r["sku_estimate"] or 0 for r in probed)
    return {
        "suppliers_total": len(rows),
        "probed": len(probed),
        "unprobed": len(rows) - len(probed),
        "tiers": dict(tiers),
        "probe_status": dict(statuses),
        "price_published": dict(prices),
        "priced_yes": prices.get("yes", 0),
        "sku_estimate_total": sku_total,
    }


def format_report(rep: dict) -> str:
    lines = [
        "# Stage-1 probe coverage",
        "",
        f"- suppliers in registry: **{rep['suppliers_total']}**",
        f"- probed: **{rep['probed']}**  |  unprobed: {rep['unprobed']}",
        f"- SKU estimate (sum): **{rep['sku_estimate_total']:,}**",
        "",
        "## Harvest tier",
    ]
    lines += [f"  - {k or '(unclassified)'}: {v}" for k, v in sorted(rep["tiers"].items(), key=lambda x: (x[0] is None, x[0]))]
    lines += ["", "## Probe status"]
    lines += [f"  - {k}: {v}" for k, v in sorted(rep["probe_status"].items())]
    lines += ["", "## Price published"]
    lines += [f"  - {k}: {v}" for k, v in sorted(rep["price_published"].items())]
    return "\n".join(lines)


def _save_report(text: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"probe_coverage_{stamp}.md"
    path.write_text(text, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mb-probe", description="Stage-1 domain probe")
    parser.add_argument("--all", action="store_true", help="probe the whole registry")
    parser.add_argument("--domain", help="probe a single domain")
    parser.add_argument("--category", help="probe rows whose categories match")
    parser.add_argument("--force", action="store_true", help="re-probe already-probed rows")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None, help="cap number of domains (debug)")
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    parser.add_argument("--report-only", action="store_true", help="print coverage, do not probe")
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    db.migrate(conn)
    if db.get_schema_version(conn) is None:
        print("schema not initialized", file=sys.stderr)
        return 2
    seeded = db.seed(conn)  # idempotent; preserves probe columns

    if args.report_only:
        print(format_report(coverage_report(conn)))
        return 0

    domains = select_domains(conn, force=args.force, category=args.category, domain=args.domain)
    if args.limit:
        domains = domains[: args.limit]

    print(f"registry: {seeded} suppliers seeded; probing {len(domains)} "
          f"({'forced' if args.force else 'skipping already-probed'})", file=sys.stderr)

    done = 0
    total = len(domains)

    def progress(r: ProbeResult) -> None:
        nonlocal done
        done += 1
        tier = r.scrape_tier.value if r.scrape_tier else r.probe_status.value
        print(f"[{done}/{total}] {r.domain} -> {tier} "
              f"(price={r.price_published.value}, sku~{r.sku_estimate})", file=sys.stderr)

    probe_domains(conn, domains, workers=args.workers, on_result=progress)

    report = format_report(coverage_report(conn))
    path = _save_report(report)
    print(report)
    print(f"\nsaved: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
