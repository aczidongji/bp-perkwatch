from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass

import trafilatura
from lxml import html as lxml_html

from ..fetch import Fetcher, Fetched


MIN_USABLE_CHARS = 200


def decode_entities(text: str) -> str:
    """Feed and page bodies carry quotes as ``&quot;`` (and lxml leaves the doubled
    ``&amp;quot;`` one level short). One pass is what the author meant. Both extraction
    routes go through here so the evidence quote and the stored text agree."""
    return html_module.unescape(text)


@dataclass
class WebArticle:
    url: str
    final_url: str
    title: str | None = None
    content: str | None = None
    summary: str | None = None
    published_at: str | None = None
    error: str | None = None
    gate: str | None = None
    status: int = 0

    @property
    def usable(self) -> bool:
        # A JS-rendered page can return 200 with a nav-menu-sized "article". Treating
        # that as usable hides a dead source behind an OK, so the body must carry
        # enough text to be worth a model call at all.
        return len(self.content or "") >= MIN_USABLE_CHARS or bool(self.summary)


def _meta_publish(raw_html: str) -> str | None:
    patterns = (
        r'property="article:published_time"\s+content="([^"]+)"',
        r'content="([^"]+)"\s+property="article:published_time"',
        r'name="date(?:published)?"\s+content="([^"]+)"',
        r'<time[^>]+datetime="([^"]+)"',
    )
    for pattern in patterns:
        match = re.search(pattern, raw_html, re.I)
        if match:
            return match.group(1)
    return None


def _plain(raw_html: str) -> str:
    """Last-resort body: strip tags so the summary at least survives."""
    try:
        document = lxml_html.fromstring(raw_html)
        for tag in document.xpath("//script | //style | //noscript"):
            tag.getparent().remove(tag)
        return re.sub(r"\s+", " ", document.text_content()).strip()[:20_000]
    except Exception:
        return ""


def looks_like_html(value: str | None) -> bool:
    return bool(value) and "<" in value and ">" in value


def html_to_text(raw_html: str) -> str:
    """Turn an HTML fragment (a feed's ``content:encoded``) into model-worthy text.

    Storing the markup costs tokens, pollutes the evidence traceback with tags, and
    makes a quoted "sentence" look like ``</p><blockquote>&quot;…``. Paragraph breaks
    and links are kept — links because ``claim_url`` has to be quotable verbatim: a page
    carrying 42 links whose extract carries 2 can never be verified against the vendor.
    """
    try:
        extracted = trafilatura.extract(raw_html, output_format="txt",
                                       include_links=True, include_comments=False)
    except Exception:
        extracted = None
    if extracted and extracted.strip():
        return decode_entities(extracted.strip())[:20_000]
    try:
        document = lxml_html.fromstring(raw_html)
    except Exception:
        return decode_entities(re.sub(r"<[^>]+>", " ", raw_html)).strip()[:20_000]
    for tag in document.xpath("//script | //style | //noscript"):
        tag.getparent().remove(tag)
    for tag in document.xpath("//br"):
        tag.tail = (tag.tail or "") + "\n"
    for tag in document.xpath("//p | //div | //li | //tr | //h1 | //h2 | //h3"):
        if tag.text and not tag.text.startswith("\n"):
            tag.text = "\n" + tag.text
    text = re.sub(r"[ \t]+", " ", document.text_content())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return decode_entities(text.strip())[:20_000]


def fetch_article(url: str, fetcher: Fetcher, *, include_html: bool = False) -> WebArticle:
    """Fetch and main-content-extract one page.

    trafilatura covers ordinary article pages without needing a headless browser.
    When it comes back empty the caller still gets the stripped text (recorded with
    an explicit ``error``) rather than a silent loss.
    """
    reply: Fetched = fetcher.get(url)
    if reply.error:
        return WebArticle(url=url, final_url=url, error=reply.error, status=reply.status or 0,
                          gate=reply.gate)
    if not reply.body:
        return WebArticle(url=url, final_url=reply.final_url, status=reply.status,
                          error="empty response body", gate=reply.gate)
    if reply.status >= 400:
        return WebArticle(url=url, final_url=reply.final_url, status=reply.status,
                          error=f"HTTP {reply.status}", gate=reply.gate)

    title: str | None = None
    try:
        meta = trafilatura.extract_metadata(reply.body)
        title = decode_entities(meta.title) if meta and meta.title else None
    except Exception:
        meta = None

    try:
        body = trafilatura.extract(
            reply.body,
            url=url,
            include_comments=False,
            include_tables=True,
            include_links=True,
            favor_recall=True,
            no_fallback=False,
        )
    except Exception as exc:  # extraction must never abort the run
        body = None
        extract_error = f"trafilatura raised {type(exc).__name__}"
    else:
        extract_error = None

    fallback = _plain(reply.body) if not body else ""
    content = (decode_entities(body) if body else None) or (fallback or None)
    error = extract_error or (None if body else "main-content extraction failed; stored fallback text")
    if error is None and content and len(content) < MIN_USABLE_CHARS:
        error = f"only {len(content)} chars extracted (page likely needs JavaScript)"

    return WebArticle(
        url=url,
        final_url=reply.final_url,
        title=title or _title_from_html(reply.body),
        content=content,
        summary=(fallback[:800] if fallback else None),
        published_at=_meta_publish(reply.body),
        error=error,
        gate=reply.gate,
        status=reply.status,
    )


def _title_from_html(raw_html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.I | re.S)
    if not match:
        return None
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return decode_entities(title)[:300] or None
