"""robots.txt parsing and path gating.

Pure logic over text — the actual GET is injected — so it tests offline.
We send a Chrome user-agent (curl_cffi impersonation), which matches no named
bot group, so we honor the most general ``*`` rules: the strict, polite reading.

Semantics of ``robots_ok`` (the registry column):
  True  -> robots retrievable (or absent, per standard = allow-all) AND our UA
           is permitted at the site root.
  False -> robots disallows the site root for us; we do not probe further.
  None  -> could not be fetched due to an error (recorded separately).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

# We match against the general group; see module docstring.
UA_TOKEN = "*"


@dataclass
class Robots:
    base_url: str
    fetched: bool
    text: str = ""
    sitemaps: list[str] = field(default_factory=list)
    _parser: RobotFileParser | None = None

    def can_fetch(self, path: str) -> bool:
        # No robots.txt (or empty) => allow-all per the standard.
        if not self.fetched or not self.text.strip() or self._parser is None:
            return True
        return self._parser.can_fetch(UA_TOKEN, urljoin(self.base_url, path))


def parse_robots(base_url: str, text: str | None, *, fetched: bool) -> Robots:
    """Build a :class:`Robots` from raw robots.txt text.

    ``fetched=False`` (network error) or empty text yields a permissive object,
    but the caller decides how to record ``robots_ok`` for that case.
    """
    text = text or ""
    if not fetched or not text.strip():
        return Robots(base_url=base_url, fetched=fetched, text=text)

    parser = RobotFileParser()
    parser.parse(text.splitlines())

    sitemaps: list[str] = []
    site_maps = parser.site_maps()  # None or list
    if site_maps:
        sitemaps = list(site_maps)
    else:
        # RobotFileParser only exposes Sitemap: when it recognizes the grammar;
        # fall back to a direct scan so we never miss one.
        for line in text.splitlines():
            if line.strip().lower().startswith("sitemap:"):
                url = line.split(":", 1)[1].strip()
                if url and url not in sitemaps:
                    sitemaps.append(url)

    return Robots(base_url=base_url, fetched=True, text=text, sitemaps=sitemaps, _parser=parser)
