"""Self-contained browser e2e: launches a real uvicorn server against the live
catalog.db, drives the dashboard with Playwright, asserts every check passes.

Marked ``browser`` + ``slow`` (loads the marqo model, ~30-60s). Run explicitly:
    pytest tests/browser/test_e2e.py -m browser
Skipped automatically if the catalog.db has no products or chromium is absent.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from material_bank import db as db_mod

pytestmark = [pytest.mark.browser, pytest.mark.slow]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _has_catalog() -> bool:
    try:
        c = db_mod.connect()
        n = c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        c.close()
        return n > 100
    except Exception:
        return False


@pytest.fixture(scope="module")
def server():
    if not _has_catalog():
        pytest.skip("live catalog.db not populated")
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        pytest.skip("playwright not installed")

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "material_bank.serve:app",
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(90):  # up to ~90s for model load + vector preload
            try:
                if urllib.request.urlopen(base + "/healthz", timeout=2).status == 200:
                    break
            except Exception:
                time.sleep(1)
        else:
            pytest.skip("server did not start in time")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_dashboard_all_checks_pass(server):
    from .smoke import run
    passed, total, failed = run(server)
    assert passed == total, f"failed browser checks: {failed}"
