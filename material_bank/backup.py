"""Disaster-proof backup: dump the irreplaceable tables, verify restorability.

What's dumped (ESSENTIAL_TABLES): suppliers, products, price_observation,
quarantine, pipeline_jobs, harvest_history, schema_version — everything the
system cannot re-derive. What's excluded: embeddings (~400MB, recomputed by the
embed worker in ~1h) and the FTS index (rebuilt by trigger/rebuild_fts). This
keeps a full-catalog dump ~tens of MB gzipped — small enough to push to GitHub.

Every dump is verified by actually restoring it into a temp database and
checking row counts — a backup that can't restore is not a backup.

Both directions stream through the sqlite3 CLI in fixed-size chunks: the dump
SQL is ~600MB of text at 250k products, and holding it in a Python str (plus
executescript's copy on the verify side) OOM-killed the backup on the 8GB VPS.
Memory stays flat no matter how large the catalog grows.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import db as db_mod

ESSENTIAL_TABLES = (
    "schema_version", "suppliers", "products", "price_observation",
    "quarantine", "pipeline_jobs", "harvest_history",
)

_CHUNK = 1 << 20  # 1MB


def dump_essential(db_path: str | Path, out_gz: str | Path) -> dict:
    """Dump essential tables (schema+data) to a gzipped SQL file via the
    sqlite3 CLI (portable, preserves DDL/constraints), streamed chunk-by-chunk."""
    db_path, out_gz = Path(db_path), Path(out_gz)
    out_gz.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["sqlite3", str(db_path)] + [f".dump {t}" for t in ESSENTIAL_TABLES]
    sql_bytes = 0
    markers = {b"CREATE TABLE": False, b"products": False}
    tail = b""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with gzip.open(out_gz, "wb") as fh:
            while True:
                chunk = proc.stdout.read(_CHUNK)
                if not chunk:
                    break
                sql_bytes += len(chunk)
                probe = tail + chunk  # overlap so a marker split across chunks still hits
                for m in markers:
                    if not markers[m] and m in probe:
                        markers[m] = True
                tail = chunk[-32:]
                fh.write(chunk)
        stderr = proc.stderr.read()
        rc = proc.wait(timeout=1800)
    except BaseException:
        proc.kill()
        out_gz.unlink(missing_ok=True)
        raise
    if rc != 0:
        out_gz.unlink(missing_ok=True)
        raise RuntimeError(f"sqlite3 .dump failed: {stderr[:300].decode(errors='replace')}")
    if not all(markers.values()):
        out_gz.unlink(missing_ok=True)
        raise RuntimeError("dump looks empty/invalid — refusing to write")
    return {"path": str(out_gz), "bytes": out_gz.stat().st_size,
            "sql_bytes": sql_bytes}


def restore(dump_gz: str | Path, new_db_path: str | Path) -> dict:
    """Rebuild a working catalog.db from an essential dump.

    Streams the dumped SQL into the sqlite3 CLI (-bail: first error aborts with
    a nonzero exit), then recreates the derived structures the dump deliberately
    omits (embeddings table empty — the embed worker refills it; FTS index
    rebuilt immediately so keyword search works on boot).
    """
    new_db_path = Path(new_db_path)
    if new_db_path.exists():
        raise FileExistsError(f"{new_db_path} exists — refusing to overwrite")
    proc = subprocess.Popen(["sqlite3", "-bail", str(new_db_path)],
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with gzip.open(dump_gz, "rb") as fh:
            try:
                shutil.copyfileobj(fh, proc.stdin, _CHUNK)
                proc.stdin.close()
            except BrokenPipeError:
                pass  # sqlite3 bailed early; rc/stderr below say why
        stderr = proc.stderr.read()
        rc = proc.wait(timeout=3600)
    except BaseException:
        proc.kill()
        raise
    if rc != 0:
        raise RuntimeError(f"restore failed: {stderr[:300].decode(errors='replace')}")
    conn = db_mod.connect(new_db_path)
    # derived structures (IF NOT EXISTS; migrations won't re-run since
    # schema_version rows came with the dump)
    conn.executescript(db_mod._EMBEDDINGS_DDL)
    conn.executescript(db_mod._FTS_DDL)
    conn.commit()
    fts_rows = db_mod.rebuild_fts(conn)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ESSENTIAL_TABLES if t != "schema_version"}
    counts["schema_version"] = db_mod.get_schema_version(conn)
    counts["fts_rows"] = fts_rows
    conn.close()
    return counts


def verify(dump_gz: str | Path) -> dict:
    """Prove the dump restores: rebuild into a temp db, return counts."""
    with tempfile.TemporaryDirectory() as td:
        return restore(dump_gz, Path(td) / "verify.db")


def main(argv=None) -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(prog="mb-backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump"); d.add_argument("out"); d.add_argument("--db", default=str(db_mod.DEFAULT_DB_PATH))
    v = sub.add_parser("verify"); v.add_argument("dump_gz")
    r = sub.add_parser("restore"); r.add_argument("dump_gz"); r.add_argument("new_db")
    args = ap.parse_args(argv)

    if args.cmd == "dump":
        info = dump_essential(args.db, args.out)
        info["verified"] = verify(args.out)
        print(json.dumps(info, default=str), file=sys.stderr)
    elif args.cmd == "verify":
        print(json.dumps(verify(args.dump_gz), default=str), file=sys.stderr)
    else:
        print(json.dumps(restore(args.dump_gz, args.new_db), default=str), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
