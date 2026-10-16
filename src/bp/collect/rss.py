from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import feedparser

from ..fetch import Fetcher
from .web import html_to_text, looks_like_html
from ..timeutil import iso, to_utc


@dataclass
class RssItem:
    url: str
    title: str | None = None
    content: str | None = None
    summary: str | None = None
    published_at: str | None = None
    author: str | None = None


def _published(entry: dict[str, Any]) -> str | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        stamp = entry.get(key)
        if stamp:
            try:
                return iso(to_utc(datetime(*stamp[:6])))
            except (TypeError, ValueError):
                continue
    for key in ("published", "updated"):
        if entry.get(key):
            return str(entry[key])
    return None


def _body(entry: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (full content, summary). feedparser spreads the body across
    content/encoded/summary depending on the feed dialect."""
    content = None
    for chunk in entry.get("content", []) or []:
        value = chunk.get("value")
        if value and (content is None or len(value) > len(content)):
            content = value
    if not content:
        content = entry.get("encoded")
    summary = entry.get("summary")
    # Many feeds ship the whole body as HTML. Storing that verbatim wastes model
    # tokens and turns every quoted evidence fragment into markup.
    if looks_like_html(content):
        content = html_to_text(content) or content
    if looks_like_html(summary):
        summary = html_to_text(summary) or summary
    if content and not summary:
        summary = content[:800]
    return content, summary


def fetch_rss(url: str, fetcher: Fetcher, *, max_items: int = 40) -> tuple[list[RssItem], str | None, dict[str, str]]:
    """Parse one feed. Raises nothing: a broken feed returns an error string for the
    caller to store against the source, so a single bad URL cannot end a run."""
    reply = fetcher.get(url)
    if not reply.ok:
        return [], reply.error or f"HTTP {reply.status}", {}

    parsed = feedparser.parse(reply.body)
    if parsed.bozo and not parsed.entries:
        return [], f"feed parse error: {parsed.bozo_exception}", {}

    items: list[RssItem] = []
    for entry in parsed.entries[:max_items]:
        link = entry.get("link") or entry.get("id") or ""
        if not link:
            continue
        content, summary = _body(entry)
        author = entry.get("author") or (entry.get("author_detail") or {}).get("name")
        items.append(RssItem(
            url=link,
            title=entry.get("title"),
            content=content,
            summary=summary,
            published_at=_published(entry),
            author=author,
        ))

    headers = {k: v for k, v in reply.headers.items() if k.lower() in ("etag", "last-modified")}
    return items, None, headers
