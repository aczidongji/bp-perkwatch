from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote_plus, urlsplit

from lxml import html as lxml_html

from ..config import get_settings
from ..fetch import Fetcher
from ..normalize import domain_of, normalize_url

# DDG/Google/SearXNG all wrap results in a redirect; unwrap to the real target.
_REDIRECT_PARAMS = ("uddg", "url", "q", "redirect_uri", "dest")


@dataclass
class DiscoveryHit:
    url: str
    title: str | None = None
    snippet: str | None = None
    query: str | None = None
    provider: str | None = None


def unwrap(target: str | None) -> str | None:
    if not target:
        return None
    try:
        query = dict(parse_qsl(urlsplit(target).query))
    except ValueError:
        return target
    for param in _REDIRECT_PARAMS:
        if query.get(param):
            return query[param]
    return target


def parse_duckduckgo(body: str, query: str) -> list[DiscoveryHit]:
    hits: list[DiscoveryHit] = []
    try:
        tree = lxml_html.fromstring(body)
    except Exception:
        return hits
    for anchor in tree.xpath('//a[contains(@class,"result__a")] | //h2[@class="result__title"]/a'):
        href = unwrap(anchor.get("href"))
        if not href or href.startswith("/"):
            continue
        title = re.sub(r"\s+", " ", anchor.text_content()).strip()
        hits.append(DiscoveryHit(url=href, title=title, query=query, provider="duckduckgo"))
    return hits


def parse_searxng(payload: dict, query: str) -> list[DiscoveryHit]:
    hits: list[DiscoveryHit] = []
    for result in payload.get("results", []) or []:
        url = unwrap(result.get("url"))
        if not url:
            continue
        hits.append(DiscoveryHit(
            url=url,
            title=result.get("title"),
            snippet=result.get("content"),
            query=query,
            provider="searxng",
        ))
    return hits


def discover(
    query: str,
    fetcher: Fetcher,
    *,
    max_results: int = 20,
    deny_domains: set[str] | None = None,
) -> tuple[list[DiscoveryHit], str | None]:
    """Run one keyword search. Tries a self-hosted/configured SearXNG first, then
    falls back to DuckDuckGo's HTML endpoint.

    Returns (hits, error). Never raises: search engines rate-limit and captcha
    automated traffic, and a blocked query must show up as a recorded error, not as
    a crashed collection run.
    """
    deny_domains = deny_domains or set()
    settings = get_settings()

    if settings.searxng_base_url:
        base = settings.searxng_base_url.rstrip("/")
        reply = fetcher.get(f"{base}/search?q={quote_plus(query)}&format=json", check_robots=False)
        if reply.ok:
            try:
                import json
                payload = json.loads(reply.body)
            except ValueError as exc:
                return [], f"searxng returned non-JSON ({exc}); is format=json enabled?"
            hits = parse_searxng(payload, query)
            if hits:
                return _deny(hits, deny_domains)[:max_results], None
            return [], "searxng returned no results"

    reply = fetcher.get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}", check_robots=False)
    if not reply.ok:
        return [], reply.error or f"HTTP {reply.status}"
    hits = parse_duckduckgo(reply.body, query)
    if not hits:
        return [], "duckduckgo returned no parsable results (likely rate-limited)"
    return _deny(hits, deny_domains)[:max_results], None


def _deny(hits: list[DiscoveryHit], deny_domains: set[str]) -> list[DiscoveryHit]:
    out = []
    for hit in hits:
        host = domain_of(normalize_url(hit.url) or hit.url) or ""
        if any(host == d or host.endswith("." + d) for d in deny_domains):
            continue
        out.append(hit)
    return out
