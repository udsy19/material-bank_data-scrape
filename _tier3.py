import sys
from material_bank import db, resolve
from material_bank.harvest.tier3 import harvest_tier3
from material_bank.harvest.run import _registry_brand
DB = "/opt/material-bank/data/catalog.db"
CAP = 400   # representative sample per supplier (deepen priority ones later; full crawl = days)

ctl = db.connect(DB)
rows = ctl.execute(
    "SELECT * FROM suppliers WHERE scrape_tier='tier3' AND status='active' "
    "AND notes LIKE '%india_design_id%' "
    "ORDER BY COALESCE(sku_estimate,0) DESC").fetchall()
ctl.close()
print(f"tier3 harvest: {len(rows)} suppliers, cap {CAP}/supplier (serial, one browser)", file=sys.stderr, flush=True)

conn = db.connect(DB, check_same_thread=False)
done = yielded = 0
for row in rows:
    dom = row["domain"]
    try:
        st = harvest_tier3(conn, domain=dom, brand=_registry_brand(row),
                           categories=row["categories"] or "",
                           sitemap_url=row["sitemap_url"] or f"https://{dom}/sitemap.xml",
                           limit=CAP, wait_ms=2000)
        n = st.get("products", 0)
        if n: yielded += 1
        conn.execute("UPDATE suppliers SET last_harvest=?, last_yield=(SELECT COUNT(*) FROM products WHERE supplier_domain=?) WHERE domain=?",
                     (db.now_iso(), dom, dom))
        conn.commit()
        print(f"  [{done+1}/{len(rows)}] {dom:30} products={n} candidates={st.get('candidates',0)}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"  [{done+1}/{len(rows)}] {dom:30} ERROR {type(e).__name__}: {str(e)[:70]}", file=sys.stderr, flush=True)
    done += 1
print(f"tier3 done: {yielded}/{len(rows)} suppliers yielded products", file=sys.stderr, flush=True)
print("regroup:", resolve.assign_variant_groups(conn), file=sys.stderr, flush=True)
