"""Collection layer: RSS, single-page web fetch, and keyword discovery."""

from .rss import fetch_rss, RssItem
from .web import fetch_article, WebArticle
from .search import discover, DiscoveryHit

__all__ = [
    "fetch_rss", "RssItem",
    "fetch_article", "WebArticle",
    "discover", "DiscoveryHit",
]
