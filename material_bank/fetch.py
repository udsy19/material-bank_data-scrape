"""Polite HTTP with curl_cffi (impersonate chrome131).

Guarantees every probe relies on:
  - per-domain spacing >= ~2s (injectable clock/sleep so tests don't wait),
  - exponential backoff on 429 / 5xx (honors Retry-After),
  - redirect capture: the final host is reported back (several low-confidence
    seed domains redirect to an India path or a different registrar),
  - content-addressed raw capture (gzip) so ambiguous rows have saved HTML for
    the probe-adjudicator subagent, and Stage-2 harvest can replay.

The session is injectable; unit tests pass a fake transport and never hit the
network.
"""

from __future__ import annotations

import gzip
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

IMPERSONATE = "chrome131"
DEFAULT_TIMEOUT = 20.0
MIN_INTERVAL = 2.0            # ~1 req / 2s per domain
RETRY_STATUSES = {429, 500, 502, 503, 504}
DEFAULT_RAW_DIR = Path(__file__).resolve().parent.parent / "raw" / "probe"


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").split(":")[0].lower().lstrip("www.")


@dataclass
class FetchResult:
    requested_url: str
    final_url: str | None = None
    final_host: str | None = None
    status_code: int | None = None
    text: str = ""
    content: bytes = b""
    headers: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    raw_path: str | None = None

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300

    @property
    def redirected_host(self) -> str | None:
        """Final host when it differs from what was requested, else None."""
        req = _host(self.requested_url)
        fin = _host(self.final_url or "")
        return fin if fin and fin != req else None


class Fetcher:
    def __init__(
        self,
        *,
        impersonate: str = IMPERSONATE,
        min_interval: float = MIN_INTERVAL,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        timeout: float = DEFAULT_TIMEOUT,
        raw_dir: Path | str | None = DEFAULT_RAW_DIR,
        session: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.impersonate = impersonate
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout
        self.raw_dir = Path(raw_dir) if raw_dir is not None else None
        self._session = session
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}

    def _ensure_session(self) -> Any:
        if self._session is None:
            from curl_cffi import requests  # imported lazily so tests stay offline

            self._session = requests.Session()
        return self._session

    def _respect_rate_limit(self, url: str) -> None:
        host = _host(url)
        last = self._last.get(host)
        if last is not None:
            wait = self.min_interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last[host] = self._clock()

    def _save_raw(self, host: str, content: bytes) -> str | None:
        if self.raw_dir is None or not content:
            return None
        try:
            digest = hashlib.sha256(content).hexdigest()
            dest = self.raw_dir / host / f"{digest}.gz"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():  # content-addressed => write-once
                dest.write_bytes(gzip.compress(content))
            return str(dest)
        except OSError:
            return None  # raw capture must never fail a probe

    @staticmethod
    def _retry_after(headers: dict[str, Any]) -> float | None:
        val = headers.get("Retry-After") or headers.get("retry-after")
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def get(self, url: str) -> FetchResult:
        session = self._ensure_session()
        result = FetchResult(requested_url=url)
        for attempt in range(self.max_retries + 1):
            self._respect_rate_limit(url)
            try:
                resp = session.get(
                    url,
                    impersonate=self.impersonate,
                    allow_redirects=True,
                    timeout=self.timeout,
                )
            except Exception as exc:  # curl_cffi raises a family of errors
                result.error = f"{type(exc).__name__}: {exc}"
                return result

            status = getattr(resp, "status_code", None)
            headers = dict(getattr(resp, "headers", {}) or {})
            if status in RETRY_STATUSES and attempt < self.max_retries:
                delay = self._retry_after(headers)
                if delay is None:
                    delay = self.backoff_base * (2 ** attempt)
                self._sleep(delay)
                continue

            content = getattr(resp, "content", b"") or b""
            final_url = getattr(resp, "url", url) or url
            result.status_code = status
            result.final_url = final_url
            result.final_host = _host(final_url)
            result.text = getattr(resp, "text", "") or ""
            result.content = content
            result.headers = headers
            result.raw_path = self._save_raw(_host(final_url), content)
            return result

        return result
