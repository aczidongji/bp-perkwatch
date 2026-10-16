from __future__ import annotations

import pytest

from bp.normalize import (
    content_hash,
    domain_of,
    normalize_text,
    normalize_url,
    registrable_domain,
)


class TestUrlNormalization:
    """Identity for "same article reached twice"."""

    def test_tracking_parameters_are_stripped(self):
        a = normalize_url("https://techcrunch.com/2026/10/01/x/?utm_source=twitter&utm_campaign=q3")
        b = normalize_url("https://techcrunch.com/2026/10/01/x/")
        assert a == b == "https://techcrunch.com/2026/10/01/x"

    @pytest.mark.parametrize("url", [
        "https://cursor.com/changelog?spm=a2c6h.12873639.article-detail.1",
        "https://cursor.com/changelog?scm=1001.8005.267750.0",
        "https://cursor.com/changelog",
    ])
    def test_chinese_platform_tracking(self, url):
        assert normalize_url(url) == "https://cursor.com/changelog"

    def test_case_and_www_and_default_port_normalised(self):
        """Scheme and host are case-insensitive; the path is NOT (RFC 3986), so
        /News and /news must not collide into one article."""
        assert normalize_url("https://WWW.OpenAI.com:443/news/?x=1") == \
               normalize_url("https://openai.com/news?x=1")

    def test_path_case_is_significant(self):
        assert normalize_url("https://a.com/News") != normalize_url("https://a.com/news")

    def test_query_order_does_not_matter(self):
        assert normalize_url("https://a.com/p?b=2&a=1") == normalize_url("https://a.com/p?a=1&b=2")

    def test_trailing_slash_ignored_but_root_kept(self):
        assert normalize_url("https://a.com/blog/") == "https://a.com/blog"
        assert normalize_url("https://a.com/") == "https://a.com/"

    def test_wechat_short_link_collapses_across_query_variants(self):
        """A WeChat article is reachable via many query strings of one /s/ slug."""
        variants = [
            "https://mp.weixin.qq.com/s/AbCdEf123",
            "https://mp.weixin.qq.com/s/AbCdEf123?__biz=MzI5&mid=224&idx=1&sn=ff",
            "https://mp.weixin.qq.com/s/AbCdEf123?scene=1&sharer_sharetime=123",
        ]
        normalised = {normalize_url(v) for v in variants}
        assert normalised == {"https://mp.weixin.qq.com/s/AbCdEf123"}

    def test_wechat_different_article_stays_different(self):
        assert normalize_url("https://mp.weixin.qq.com/s/AAA") != \
               normalize_url("https://mp.weixin.qq.com/s/BBB")

    def test_non_url_input_falls_back_not_crashes(self):
        assert normalize_url("mp.weixin.qq.com/s/XYZ") == "https://mp.weixin.qq.com/s/XYZ"
        assert normalize_url("") is None
        assert normalize_url(None) is None
        assert normalize_url("   ") is None

    def test_fragment_dropped(self):
        assert normalize_url("https://a.com/p#section") == "https://a.com/p"

    def test_real_content_query_preserved(self):
        """Only tracking noise is removed; a query that selects different content
        must stay, or two different pages would collide."""
        assert normalize_url("https://a.com/p?page=2") != normalize_url("https://a.com/p?page=3")


class TestContentHash:
    def test_whitespace_and_case_insensitive(self):
        assert content_hash("标题", "Hello   World") == content_hash("不同标题", "hello world\n")

    def test_title_changes_do_not_change_the_hash(self):
        """A repost under a new headline is still the same article."""
        body = "本次活动提供 Cursor Pro 一个月免费额度，需在 10 月底前领取。"
        assert content_hash("【限时】Cursor 免费", body) == content_hash("Cursor 白嫖攻略", body)

    def test_single_character_difference_detected(self):
        assert content_hash("t", "免费领 100 credits") != content_hash("t", "免费领 1000 credits")

    def test_empty_body_still_hashable(self):
        assert content_hash(None, None) == content_hash("", "")


class TestDomains:
    @pytest.mark.parametrize("url,expected", [
        ("https://docs.cursor.com/account", "cursor.com"),
        ("https://foo.bar.github.io/x", "bar.github.io"),
        ("https://mp.weixin.qq.com/s/abc", "qq.com"),
    ])
    def test_registrable_domain(self, url, expected):
        assert registrable_domain(url) == expected

    def test_subdomains_share_a_registrable_domain(self):
        assert registrable_domain("https://blog.openai.com/news") == \
               registrable_domain("https://openai.com/news")

    def test_garbage_returns_none_not_exception(self):
        assert registrable_domain("not a url") is None
        assert registrable_domain(None) is None

    def test_domain_of(self):
        assert domain_of("https://WWW.Example.COM:443/a") == "www.example.com"


class TestNormalizeText:
    def test_nfkc_fullwidth(self):
        assert normalize_text("Ｃｕｒｓｏｒ　Ｐｒｏ") == "cursor pro"

    def test_zero_width_removed(self):
        assert normalize_text("免​费") == "免费"

    def test_none_safe(self):
        assert normalize_text(None) == ""
