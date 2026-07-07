from datetime import timedelta

import pytest

from material_bank import db as db_mod
from material_bank import jobs
from material_bank.jobs import _now


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db", check_same_thread=False)
    db_mod.migrate(c)
    yield c
    c.close()


def test_enqueue_idempotent(conn):
    jobs.enqueue(conn, "harvest", "a.com")
    jobs.enqueue(conn, "harvest", "a.com")  # no dupe
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 1


def test_claim_and_complete(conn):
    jobs.enqueue(conn, "harvest", "a.com")
    job = jobs.claim(conn, "harvest")
    assert job["target"] == "a.com" and job["status"] == "running"
    assert jobs.claim(conn, "harvest") is None  # nothing else claimable
    jobs.complete(conn, job["id"], {"products": 5})
    assert jobs.counts(conn, "harvest") == {"pending": 0, "running": 0, "done": 1, "failed": 0}


def test_priority_order(conn):
    jobs.enqueue(conn, "harvest", "low.com", priority=0)
    jobs.enqueue(conn, "harvest", "high.com", priority=10)
    assert jobs.claim(conn, "harvest")["target"] == "high.com"


def test_fail_reschedules_with_backoff(conn):
    jobs.enqueue(conn, "harvest", "a.com", max_attempts=4)
    job = jobs.claim(conn, "harvest")
    status = jobs.fail(conn, job["id"], "boom")
    assert status == "pending"
    row = conn.execute("SELECT attempts, next_run_at, last_error FROM pipeline_jobs WHERE id=?",
                       (job["id"],)).fetchone()
    assert row["attempts"] == 1 and row["last_error"] == "boom" and row["next_run_at"] is not None
    # not claimable yet (backoff in the future)
    assert jobs.claim(conn, "harvest") is None
    # ...but claimable once the backoff elapses
    future = _now() + timedelta(hours=2)
    assert jobs.claim(conn, "harvest", now=future)["id"] == job["id"]


def test_dead_letters_after_max_attempts(conn):
    jobs.enqueue(conn, "harvest", "bad.com", max_attempts=3)
    future = _now() + timedelta(days=1)
    for expected in ("pending", "pending", "failed"):
        job = jobs.claim(conn, "harvest", now=future)
        assert job is not None
        # base=0 so the retry is immediately re-claimable at the same instant
        assert jobs.fail(conn, job["id"], "still broken", base=0, now=future) == expected
    assert jobs.counts(conn, "harvest")["failed"] == 1
    dl = jobs.dead_letters(conn, "harvest")
    assert dl[0]["target"] == "bad.com" and dl[0]["attempts"] == 3


def test_retry_dead_rearms(conn):
    jobs.enqueue(conn, "harvest", "bad.com", max_attempts=1)
    job = jobs.claim(conn, "harvest")
    jobs.fail(conn, job["id"], "x")  # -> failed (max_attempts=1)
    assert jobs.counts(conn, "harvest")["failed"] == 1
    assert jobs.retry_dead(conn, "harvest") == 1
    assert jobs.counts(conn, "harvest")["pending"] == 1
    assert jobs.claim(conn, "harvest") is not None


def test_requeue_stale_running(conn):
    jobs.enqueue(conn, "harvest", "a.com")
    job = jobs.claim(conn, "harvest")  # -> running
    # simulate a crashed worker: backdate updated_at
    conn.execute("UPDATE pipeline_jobs SET updated_at=? WHERE id=?",
                 ((_now() - timedelta(hours=2)).isoformat(), job["id"]))
    conn.commit()
    assert jobs.requeue_stale_running(conn) == 1
    assert jobs.claim(conn, "harvest")["id"] == job["id"]  # reclaimable


def test_enqueue_due_refreshes_tier_aware(conn):
    from datetime import timedelta
    now = _now()
    def ago(d): return (now - timedelta(days=d)).isoformat()
    # (domain, tier, priced, last_harvest_days_ago)
    rows = [
        ("shop_2d.com",   "shopify",     "yes", 2),   # fast(1d) -> DUE
        ("shop_12h.com",  "shopify",     "yes", 0.5), # fast(1d) -> not due
        ("big_3d.com",    "jsonld",      "yes", 3),   # jsonld(7d) -> not due (giant, spare it)
        ("big_10d.com",   "jsonld",      "yes", 10),  # jsonld(7d) -> DUE
        ("spec_10d.com",  "tier3",       "no",  10),  # slow(30d) -> not due
        ("spec_40d.com",  "tier3",       "no",  40),  # slow(30d) -> DUE
    ]
    for dom, tier, priced, days in rows:
        conn.execute("INSERT INTO suppliers(brand,domain,status,scrape_tier,price_published,last_harvest) "
                     "VALUES(?,?,?,?,?,?)", (dom, dom, "active", tier, priced, ago(days)))
        jobs.enqueue(conn, "harvest", dom)
        j = jobs.claim(conn, "harvest"); jobs.complete(conn, j["id"])
    conn.commit()

    n = jobs.enqueue_due_refreshes(conn, now=now)   # defaults: 1/7/30 days
    assert n == 3
    due = {r[0] for r in conn.execute("SELECT target FROM pipeline_jobs WHERE status='pending'")}
    assert due == {"shop_2d.com", "big_10d.com", "spec_40d.com"}


def test_reset_rearms_done_job(conn):
    jobs.enqueue(conn, "harvest", "a.com")
    j = jobs.claim(conn, "harvest"); jobs.complete(conn, j["id"])
    jobs.enqueue(conn, "harvest", "a.com", reset=True)
    assert jobs.counts(conn, "harvest")["pending"] == 1
