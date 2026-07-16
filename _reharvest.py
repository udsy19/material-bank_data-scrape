import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from material_bank import db
from material_bank.fetch import Fetcher
from material_bank.harvest.jsonld import harvest_jsonld
from material_bank.harvest.run import _registry_brand
DB = "/opt/material-bank/data/catalog.db"

ctl = db.connect(DB)
rows = ctl.execute(
    "SELECT * FROM suppliers WHERE scrape_tier='jsonld' AND status='active' "
    "AND notes LIKE '%india_design_id%' ORDER BY domain").fetchall()
ctl.close()
print(f"re-harvesting {len(rows)} jsonld suppliers with refresh=True", file=sys.stderr, flush=True)

def one(row):
    conn = db.connect(DB, check_same_thread=False)
    fetcher = Fetcher(min_interval=2.0, raw_dir=None)
    try:
        st = harvest_jsonld(conn, fetcher, domain=row["domain"], brand=_registry_brand(row),
                            categories=row["categories"] or "", sitemap_url=row["sitemap_url"],
                            base_host=row["final_host"] or row["domain"], refresh=True)
    except Exception as e:
        st = {"domain": row["domain"], "error": str(e)}
    conn.commit(); conn.close()
    print(f"  {row['domain']:26} {st.get('products',0)} products, {st.get('priced',0)} priced "
          f"{st.get('error','')}", file=sys.stderr, flush=True)
    return st

with ThreadPoolExecutor(max_workers=10) as pool:
    for f in as_completed([pool.submit(one, r) for r in rows]):
        f.result()
# re-group variants (titles changed) + re-arm re-titled products for re-enrichment
from material_bank import resolve
conn = db.connect(DB)
print("regroup:", resolve.assign_variant_groups(conn), file=sys.stderr, flush=True)
print("re-harvest done", file=sys.stderr, flush=True)
