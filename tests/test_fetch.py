"""Fetch layer: robots.txt scoping, gate detection, and the no-bypass rules.

These are the guarantees the safety constraints depend on, so they are tested
against a fake transport instead of a live site.
"""
from __future__ import annotations

import pytest

from bp.collect.web import MIN_USABLE_CHARS, WebArticle
from bp.fetch import Fetcher, detect_gate


class Reply:
    def __init__(self, status=200, text="", url="https://example.com/"):
        self.status_code = status
        self.text = text
        self.url = url
        self.headers = {}


class FakeClient:
    """Serves a canned body per host+path and records every requested URL."""

    def __init__(self, pages: dict[str, Reply]):
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url, headers=None):
        self.requested.append(url)
        if url in self.pages:
            return self.pages[url]
        return Reply(404, "not found", url)

    def close(self):
        pass


def fetcher_with(pages: dict[str, Reply]) -> Fetcher:
    fetcher = Fetcher(user_agent="bp-test/0.1")
    fetcher._client = FakeClient(pages)  # bypass the context manager, no network
    return fetcher


ROBOTS_GEMINI = """User-agent: *
Allow: /app/download
Disallow: /app/
Disallow: /chat/
"""

ROBOTS_GOOGLE_COM = """User-agent: *
Disallow: /
"""


def test_robots_is_scoped_to_the_host_not_the_registrable_domain():
    """gemini.google.com and google.com publish different rules.

    Collapsing them to one registrable domain made the student page look
    forbidden, silently dropping a live source.
    """
    fetcher = fetcher_with({
        "https://gemini.google.com/robots.txt": Reply(200, ROBOTS_GEMINI),
        "https://gemini.google.com/students": Reply(200, "<html>ok</html>"),
    })
    assert fetcher.allowed_by_robots("https://gemini.google.com/students") is True
    assert "https://gemini.google.com/robots.txt" in fetcher._client.requested
    assert "https://google.com/robots.txt" not in fetcher._client.requested


def test_robots_blocks_the_path_the_site_disallows():
    fetcher = fetcher_with({
        "https://gemini.google.com/robots.txt": Reply(200, ROBOTS_GEMINI),
    })
    assert fetcher.allowed_by_robots("https://gemini.google.com/chat/history") is False
    assert fetcher.allowed_by_robots("https://gemini.google.com/app/download") is True


def test_host_that_disallows_everything_is_refused_without_being_fetched():
    fetcher = fetcher_with({
        "https://lobste.rs/robots.txt": Reply(200, "User-agent: *\nDisallow: /\n"),
    })
    reply = fetcher.get("https://lobste.rs/rss")
    assert reply.error == "disallowed by robots.txt"
    assert fetcher._client.requested == ["https://lobste.rs/robots.txt"]


def test_unparsable_robots_file_is_treated_as_permissive():
    fetcher = fetcher_with({
        "https://a.com/robots.txt": Reply(500, "boom"),
        "https://a.com/page": Reply(200, "<html>hi</html>"),
    })
    assert fetcher.get("https://a.com/page").status == 200


def test_robots_file_is_fetched_once_per_host():
    fetcher = fetcher_with({
        "https://a.com/robots.txt": Reply(200, "User-agent: *\nAllow: /\n"),
        "https://a.com/one": Reply(200, "<html>1</html>"),
        "https://a.com/two": Reply(200, "<html>2</html>"),
    })
    fetcher.get("https://a.com/one")
    fetcher.get("https://a.com/two")
    assert fetcher._client.requested.count("https://a.com/robots.txt") == 1


@pytest.mark.parametrize("body,expected", [
    ("<p>请先登录后继续阅读。</p>", "login_required"),
    ("<p>Please log in to continue</p>", "login_required"),
    ("<p>Verify you are human</p>", "captcha"),
    ("<p>人机验证</p>", "captcha"),
    ("<p>Subscribe to read this story</p>", "paywall"),
    ("<p>付费阅读，开通会员后可读</p>", "paywall"),
    ("<p>Normal article about free credits</p>", None),
])
def test_detect_gate(body, expected):
    assert detect_gate(body, 200) == expected


def test_gate_is_reported_instead_of_being_worked_around():
    """A gated page returns a marker and no retry with credentials."""
    fetcher = fetcher_with({
        "https://v.com/offer": Reply(200, "<html><p>请登录</p></html>"),
    })
    reply = fetcher.get("https://v.com/offer")
    assert reply.gate == "login_required"
    assert fetcher._client.requested == ["https://v.com/robots.txt", "https://v.com/offer"]


def test_relative_url_is_normalised_to_https():
    fetcher = fetcher_with({"https://a.com/x": Reply(200, "<html>ok</html>")})
    assert fetcher.get("a.com/x").status == 200


def test_empty_url_never_touches_the_network():
    fetcher = fetcher_with({})
    reply = fetcher.get("")
    assert reply.error == "empty url"
    assert fetcher._client.requested == []


def make_article(**kwargs) -> WebArticle:
    defaults = dict(url="https://a.com/1", final_url="https://a.com/1", status=200)
    defaults.update(kwargs)
    return WebArticle(**defaults)


def test_js_shell_page_is_not_usable():
    """A 200 with a nav-menu-sized body must not be reported as a working source."""
    assert make_article(content="Academy | Courses").usable is False


def test_article_at_the_threshold_is_usable():
    assert make_article(content="字" * MIN_USABLE_CHARS).usable is True


def test_summary_can_rescue_a_missing_body():
    assert make_article(content="", summary="feed summary").usable is True


def test_fetch_article_reports_the_short_page_as_an_error():
    from bp.collect.web import fetch_article

    html = "<html><body><p>" + "短" * 30 + "</p></body></html>"
    fetcher = fetcher_with({"https://a.com/js": Reply(200, html)})
    page = fetch_article("https://a.com/js", fetcher)
    assert page.usable is False
    assert "JavaScript" in (page.error or "")


FEED_ITEM = """<div><p>Cursor 新用户注册即送 2000 次免费补全，活动截止 2026 年 12 月 31 日。</p>
<p>另一段正文，包含 <a href="https://cursor.com/promo">领取入口</a>。</p>
<script>var tracking = "noisy";</script></div>"""


def test_html_fragment_becomes_clean_text():
    from bp.collect.web import html_to_text

    text = html_to_text(FEED_ITEM)
    assert "<" not in text and "&" not in text
    assert "Cursor 新用户注册即送 2000 次免费补全" in text
    assert "另一段正文" in text
    assert "tracking" not in text          # script bodies must not become "content"
    assert "\n" in text                     # paragraph breaks survive for the extractor


def test_doubly_escaped_entities_become_the_characters_they_mean():
    """有的中文 feed 用 &quot; 包住引号，留在正文里会污染引文回溯。"""
    from bp.collect.web import decode_entities, html_to_text

    assert decode_entities('主打&quot;轻、快、稳&quot;') == '主打"轻、快、稳"'
    text = html_to_text('<p>主打&amp;quot;轻、快、稳&amp;quot;，全库运行。</p>')
    assert '"轻、快、稳"' in text
    assert "&quot;" not in text
    assert "&amp;" not in text


def test_looks_like_html_only_matches_real_markup():
    from bp.collect.web import looks_like_html

    assert looks_like_html("<p>正文</p>") is True
    assert looks_like_html("纯文本正文，没有标签") is False
    assert looks_like_html("a < b 且 c > d 但不是标签") is True  # 保守判定，转一遍也无害
    assert looks_like_html("") is False


def test_rss_body_strips_feed_markup_end_to_end():
    from bp.collect.rss import _body

    content, summary = _body({"content": [{"value": FEED_ITEM}], "summary": None})
    assert "<p>" not in content
    assert "2000 次免费补全" in content
    assert "<p>" not in (summary or "")


def test_feed_links_survive_so_claim_url_can_be_quoted():
    """claim_url 必须能逐字引自正文；正文里没有链接，官方核验就永远无据可依。"""
    from bp.collect.web import html_to_text

    text = html_to_text('<p>领取入口见 <a href="https://cursor.com/promo">这里</a>，'
                         '规则见 <a href="https://cursor.com/terms">条款</a>。</p>')
    assert "https://cursor.com/promo" in text
    assert "https://cursor.com/terms" in text
    assert "<a " not in text


def test_page_extraction_keeps_href_targets():
    """链接写在正文里才会被留下；单独成段的链接会被 trafilatura 判成导航噪声。"""
    paragraphs = "".join(
        f'<p>第 {i} 条说明，注册即送 {2000 * i} 次免费补全，活动截止日期为 2026 年 12 月 31 日。'
        f'学生验证入口在 <a href="https://cursor.com/students{i}">这里</a>，'
        f'条款见 <a href="https://cursor.com/terms{i}">规则页</a>。</p>'
        for i in range(1, 6)
    )
    html = f'<html><head><title>活动</title></head><body><article>{paragraphs}</article></body></html>'
    fetcher = fetcher_with({"https://a.com/promo": Reply(200, html)})
    from bp.collect.web import fetch_article

    page = fetch_article("https://a.com/promo", fetcher)
    assert "https://cursor.com/students" in (page.content or "")
    assert "https://cursor.com/terms" in (page.content or "")
    assert page.usable is True
