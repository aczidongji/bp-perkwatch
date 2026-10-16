"""「禁止仅凭公众号文章或 LLM 判断就标记为"已确认"」的强制执行路径。

确认只能来自一条 official_domain_match=1、verdict='available'、且在有效期内的
verification_runs 记录。数据库触发器与 Python 两侧各拦一次。
"""
from __future__ import annotations

import pytest
from helpers import add_perk, article, authorise, perk

from bp import config, store, verify
from bp.fetch import Fetched, detect_gate
from bp.models import PerkFields

OFFICIAL_PAGE = "Cursor 新用户注册即送 1 个月 Pro 会员，免费试用，活动截止 2026 年 12 月 31 日。"
ENDED_PAGE = "Cursor 免费会员活动已结束，本页面仅存档。"
NO_OFFER_PAGE = "Cursor 2.0 发布：全新的 Agent 模式与更快的补全速度。"
LOGIN_PAGE = "Please log in to continue. 请登录以查看该内容。"
PAYWALL_PAGE = "Subscription required to read this article. 付费阅读。"


class FakeFetcher:
    """只回答预设页面，绝不联网。"""

    def __init__(self, pages: dict[str, dict]):
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str, *, check_robots: bool = True) -> Fetched:
        self.requested.append(url)
        page = self.pages.get(url, {})
        status = page.get("status", 200)
        body = page.get("body", "")
        return Fetched(
            url=url, final_url=page.get("final", url), status=status, body=body,
            error=page.get("error"), gate=detect_gate(body, status),
        )


def status_of(conn, pid):
    return conn.execute("SELECT status FROM perks WHERE id = ?", (pid,)).fetchone()["status"]


class TestDatabaseConfirmGuard:
    def test_direct_sql_update_to_confirmed_is_refused(self, conn):
        pid = add_perk(conn, perk(claim="https://cursor.com/a"))
        with pytest.raises(store.sqlite3.IntegrityError, match="CONFIRM_GUARD"):
            conn.execute("UPDATE perks SET status = 'confirmed' WHERE id = ?", (pid,))

    def test_insert_as_confirmed_is_refused(self, conn):
        with pytest.raises(store.sqlite3.IntegrityError, match="CONFIRM_GUARD"):
            conn.execute(
                "INSERT INTO perks (canonical_key, product, product_norm, benefit_type, "
                "status, first_seen_at, updated_at) VALUES ('k1','Cursor','cursor',"
                "'free_membership','confirmed','2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')"
            )

    def test_set_status_routes_a_refusal_into_needs_review(self, conn):
        pid = add_perk(conn, perk(claim="https://cursor.com/b"))
        assert store.set_status(conn, pid, "confirmed", "文章里说可以领", "llm") is False
        row = conn.execute("SELECT status, needs_review, review_reason FROM perks WHERE id = ?",
                           (pid,)).fetchone()
        assert row["status"] == "pending", "被拒绝的确认不得改动状态"
        assert row["needs_review"] == 1
        assert "拒绝直接确认" in row["review_reason"]

    def test_article_text_alone_never_authorises_confirmation(self, conn):
        """福利来自公众号文章、模型也很自信——这两样加起来仍然不够。"""
        pid = add_perk(conn, perk(claim="https://cursor.com/c", conf=0.99))
        assert store.set_status(conn, pid, "confirmed", "模型判定可用", "llm") is False
        assert status_of(conn, pid) == "pending"

    def test_a_third_party_host_never_authorises_confirmation(self, conn):
        pid = add_perk(conn, perk(claim="https://coupon-aggregator.com/deal/1"))
        authorise(conn, pid, "https://coupon-aggregator.com/deal/1", official=False)
        assert store.set_status(conn, pid, "confirmed", "第三方站点显示可领", "verification") is False

    def test_stale_official_check_is_refused(self, conn):
        pid = add_perk(conn, perk(claim="https://cursor.com/d"))
        authorise(conn, pid, "https://cursor.com/d", age_days=30)
        assert store.set_status(conn, pid, "confirmed", "一个月前的核验结果", "verification") is False

    def test_fresh_official_available_check_is_enough(self, conn):
        pid = add_perk(conn, perk(claim="https://cursor.com/e"))
        authorise(conn, pid, "https://cursor.com/e")
        assert store.set_status(conn, pid, "confirmed", "官方页面仍可领取", "verification") is True
        assert status_of(conn, pid) == "confirmed"

    def test_verdict_not_available_is_refused(self, conn):
        pid = add_perk(conn, perk(claim="https://cursor.com/f"))
        authorise(conn, pid, "https://cursor.com/f", verdict="unknown")
        assert store.set_status(conn, pid, "confirmed", "官方页面读取失败", "verification") is False

    def test_expired_is_allowed_without_any_verification(self, conn):
        """关闭一条过期福利不需要凭据，只有"确认"才需要。"""
        pid = add_perk(conn, perk(claim="https://cursor.com/g"))
        assert store.set_status(conn, pid, "expired", "截止日期已过", "scheduler") is True

    def test_freshness_window_is_read_from_meta(self, conn):
        from bp import db
        pid = add_perk(conn, perk(claim="https://cursor.com/h"))
        authorise(conn, pid, "https://cursor.com/h", age_days=2)
        db.set_meta(conn, "verification_max_age_days", "1")
        conn.commit()
        assert store.set_status(conn, pid, "confirmed", "窗口收紧到 1 天", "verification") is False


class TestVerificationRouting:
    def test_allowlisted_domain_still_offering_confirms_the_perk(self, conn):
        claim = "https://cursor.com/promo"
        pid = add_perk(conn, perk(claim=claim))
        fetcher = FakeFetcher({claim: {"body": OFFICIAL_PAGE}})
        results = verify.verify_perk(conn, pid, fetcher)
        assert [r.verdict for r in results] == ["available"]
        assert results[0].official_domain_match is True
        assert status_of(conn, pid) == "confirmed"

    def test_aggregator_host_cannot_confirm_even_when_the_page_looks_live(self, conn):
        claim = "https://deal-of-the-day.example/cursor-pro"
        pid = add_perk(conn, perk(claim=claim))
        fetcher = FakeFetcher({claim: {"body": OFFICIAL_PAGE}})
        results = verify.verify_perk(conn, pid, fetcher)
        assert results[0].official_domain_match is False
        assert status_of(conn, pid) == "pending", "转载站点的「看起来还能领」不是确认依据"
        row = conn.execute("SELECT needs_review, review_reason FROM perks WHERE id = ?", (pid,)).fetchone()
        assert row["needs_review"] == 1
        assert "第三方" in row["review_reason"]

    def test_search_corroboration_can_promote_an_unlisted_official_domain(self, conn, monkeypatch):
        claim = "https://foo.dev/free"
        pid = add_perk(conn, perk(product="Foo", claim=claim, entitlement="免费注册送额度"))
        from bp.collect.search import DiscoveryHit
        monkeypatch.setattr(verify, "discover",
                            lambda q, f, max_results=10: ([DiscoveryHit(url="https://foo.dev/pricing")], None))
        fetcher = FakeFetcher({claim: {"body": "Foo 新用户免费注册即送额度，free credits for new users。"}})
        results = verify.verify_perk(conn, pid, fetcher)
        assert results[0].official_domain_match is True
        assert status_of(conn, pid) == "confirmed"

    def test_search_that_does_not_rank_the_domain_keeps_it_pending(self, conn, monkeypatch):
        claim = "https://not-the-vendor.example/free"
        pid = add_perk(conn, perk(product="Foo", claim=claim, entitlement="免费注册送额度"))
        from bp.collect.search import DiscoveryHit
        monkeypatch.setattr(verify, "discover",
                            lambda q, f, max_results=10: ([DiscoveryHit(url="https://somethingelse.com/")], None))
        fetcher = FakeFetcher({claim: {"body": "Foo 免费注册送额度"}})
        results = verify.verify_perk(conn, pid, fetcher)
        assert results[0].official_domain_match is False
        assert status_of(conn, pid) == "pending"

    def test_page_saying_the_offer_ended_marks_expired(self, conn):
        claim = "https://cursor.com/promo"
        pid = add_perk(conn, perk(claim=claim))
        results = verify.verify_perk(conn, pid, FakeFetcher({claim: {"body": ENDED_PAGE}}))
        assert results[0].verdict == "expired"
        assert status_of(conn, pid) == "expired"

    def test_official_page_without_the_offer_anymore_marks_invalid(self, conn):
        claim = "https://cursor.com/promo"
        pid = add_perk(conn, perk(claim=claim))
        results = verify.verify_perk(conn, pid, FakeFetcher({claim: {"body": NO_OFFER_PAGE}}))
        assert results[0].verdict == "not_found"
        assert status_of(conn, pid) == "invalid"

    def test_404_on_the_official_page_marks_invalid(self, conn):
        claim = "https://cursor.com/promo"
        pid = add_perk(conn, perk(claim=claim))
        results = verify.verify_perk(conn, pid, FakeFetcher({claim: {"status": 404, "body": "Not Found"}}))
        assert results[0].verdict == "not_found"
        assert status_of(conn, pid) == "invalid"

    @pytest.mark.parametrize("body,expected_verdict", [
        (LOGIN_PAGE, "login_required"),
        (PAYWALL_PAGE, "paywall"),
        ("请完成人机验证后继续访问。", "captcha"),
    ])
    def test_gated_pages_are_recorded_and_stopped_at(self, conn, body, expected_verdict):
        """安全要求：遇到登录/付费/验证码只记录并停手，绝不尝试绕过。"""
        claim = "https://cursor.com/promo"
        pid = add_perk(conn, perk(claim=claim))
        results = verify.verify_perk(conn, pid, FakeFetcher({claim: {"body": body}}))
        assert results[0].verdict == expected_verdict
        assert status_of(conn, pid) == "dubious"
        row = conn.execute("SELECT needs_review, review_reason FROM perks WHERE id = ?", (pid,)).fetchone()
        assert row["needs_review"] == 1

    def test_network_error_leaves_the_perk_dubious_for_review(self, conn):
        claim = "https://cursor.com/promo"
        pid = add_perk(conn, perk(claim=claim))
        results = verify.verify_perk(conn, pid, FakeFetcher({claim: {"error": "timeout"}}))
        assert results[0].verdict == "error"
        assert status_of(conn, pid) == "dubious"

    def test_every_examined_url_is_written_to_verification_runs(self, conn):
        claim, rules = "https://cursor.com/promo", "https://cursor.com/terms"
        article_id = article(conn, "https://mp.weixin.qq.com/s/xyz")
        pid = store.upsert_perk(
            conn,
            PerkFields(product="Cursor", benefit_type="free_membership",
                       entitlement="注册即送 1 个月 Pro", claim_url=claim, rules_url=rules),
            article_id,
        ).perk_id
        conn.commit()
        fetcher = FakeFetcher({claim: {"body": OFFICIAL_PAGE}, rules: {"body": OFFICIAL_PAGE}})
        results = verify.verify_perk(conn, pid, fetcher)
        assert sorted(fetcher.requested) == sorted([claim, rules])
        assert len(results) == 2
        rows = conn.execute("SELECT url, verdict, official_domain_match FROM verification_runs "
                            "WHERE perk_id = ? ORDER BY url", (pid,)).fetchall()
        assert len(rows) == 2
        assert all(r["official_domain_match"] == 1 for r in rows)

    def test_verification_of_an_unknown_perk_is_a_no_op(self, conn):
        assert verify.verify_perk(conn, 9999, FakeFetcher({})) == []

    def test_allowlist_matches_subdomains(self, conn):
        assert verify.domain_is_official_for("cursor.com", "cursor") == "cursor"
        assert verify.registrable_domain("https://docs.cursor.com/x") == "cursor.com"
        assert verify.domain_is_official_for("evil-cursor.com", "cursor") is None


class TestVerdictBookkeeping:
    """核验的记账规则：last_verified_at 每次都动，updated_at 只在真的变了时才动。"""

    @staticmethod
    def _freeze(conn, pid):
        conn.execute("UPDATE perks SET updated_at = '2020-01-01T00:00:00Z' WHERE id = ?", (pid,))
        conn.commit()

    @staticmethod
    def _updated_at(conn, pid):
        return conn.execute("SELECT updated_at FROM perks WHERE id = ?", (pid,)).fetchone()["updated_at"]

    def test_a_sweep_that_changes_the_status_moves_updated_at(self, conn):
        claim = "https://cursor.com/promo"
        pid = add_perk(conn, perk(claim=claim))
        self._freeze(conn, pid)
        verify.verify_perk(conn, pid, FakeFetcher({claim: {"body": OFFICIAL_PAGE}}))
        assert status_of(conn, pid) == "confirmed"
        assert self._updated_at(conn, pid) != "2020-01-01T00:00:00Z"

    def test_a_sweep_that_changes_nothing_leaves_updated_at_alone(self, conn):
        """此前无条件推 updated_at，于是一条没被解决、也没发生变化的待核实福利
        每次核验都会被顶回日报的增量窗口。"""
        claim = "https://cursor.com/promo"
        pid = add_perk(conn, perk(claim=claim))
        verify.verify_perk(conn, pid, FakeFetcher({claim: {"body": OFFICIAL_PAGE}}))
        assert status_of(conn, pid) == "confirmed"
        self._freeze(conn, pid)
        verify.verify_perk(conn, pid, FakeFetcher({claim: {"body": OFFICIAL_PAGE}}))
        assert self._updated_at(conn, pid) == "2020-01-01T00:00:00Z"

    def test_last_verified_still_moves_on_a_no_change_sweep(self, conn):
        claim = "https://cursor.com/promo"
        pid = add_perk(conn, perk(claim=claim))
        verify.verify_perk(conn, pid, FakeFetcher({claim: {"body": OFFICIAL_PAGE}}))
        conn.execute("UPDATE perks SET last_verified_at = '2020-01-01T00:00:00Z' WHERE id = ?", (pid,))
        conn.commit()
        verify.verify_perk(conn, pid, FakeFetcher({claim: {"body": OFFICIAL_PAGE}}))
        assert conn.execute("SELECT last_verified_at FROM perks WHERE id = ?",
                            (pid,)).fetchone()["last_verified_at"] != "2020-01-01T00:00:00Z"


class TestRecoveryFromAWrongVerdict:
    """核验回答的是"此刻官方页面说了什么"，不是"第一次怎么说"。"""

    def test_a_perk_marked_invalid_is_recovered_by_a_fresh_official_check(self, conn):
        claim = "https://cursor.com/promo"
        pid = add_perk(conn, perk(claim=claim))
        assert store.set_status(conn, pid, "invalid", "当时页面没有这个活动", "verification") is True
        verify.verify_perk(conn, pid, FakeFetcher({claim: {"body": OFFICIAL_PAGE}}))
        assert status_of(conn, pid) == "confirmed"

    def test_a_perk_marked_dubious_is_recovered_too(self, conn):
        claim = "https://cursor.com/promo"
        pid = add_perk(conn, perk(claim=claim))
        verify.verify_perk(conn, pid, FakeFetcher({claim: {"error": "timeout"}}))
        assert status_of(conn, pid) == "dubious"
        verify.verify_perk(conn, pid, FakeFetcher({claim: {"body": OFFICIAL_PAGE}}))
        assert status_of(conn, pid) == "confirmed"

    def test_an_expired_perk_is_not_resurrected_by_a_live_page(self, conn):
        """expired 是按 ends_at 判的，当晚 sweep_expired 会再判一次；在这里恢复只会
        造成 已确认 ↔ 已过期 每晚来回翻。"""
        claim = "https://cursor.com/promo"
        pid = add_perk(conn, perk(claim=claim))
        store.set_status(conn, pid, "expired", "截止日期已过", "scheduler")
        conn.commit()
        verify.verify_perk(conn, pid, FakeFetcher({claim: {"body": OFFICIAL_PAGE}}))
        assert status_of(conn, pid) == "expired"


class TestFreshnessWindowSync:
    def test_the_database_guard_follows_the_setting(self, conn, monkeypatch, test_env):
        """触发器读 meta，而 meta 只在 init_db 写过一次 —— 改了 .env 之后应用和
        守卫会用两个不同的窗口。"""
        from bp import db
        monkeypatch.setenv("VERIFICATION_MAX_AGE_DAYS", "3")
        config.get_settings.cache_clear()
        db.set_meta(conn, "verification_max_age_days", "30")
        conn.commit()
        verify.sync_verification_window(conn)
        assert db.get_meta(conn, "verification_max_age_days") == "3"


class TestCandidateDomain:
    def test_claim_and_rules_on_one_domain_is_the_strongest_signal(self, conn):
        domain, reason = verify._candidate_official_domain(conn, {
            "claim_url": "https://bar.io/free", "rules_url": "https://bar.io/terms",
            "product_norm": "bar",
        })
        assert domain == "bar.io"
        assert "share" in reason

    def test_weixin_links_are_never_treated_as_the_vendor_domain(self, conn):
        domain, _reason = verify._candidate_official_domain(conn, {
            "claim_url": "https://mp.weixin.qq.com/s/abc", "rules_url": None,
            "product_norm": "bar",
        })
        assert domain is None

    def test_no_links_at_all_yields_no_candidate(self, conn):
        domain, reason = verify._candidate_official_domain(conn, {
            "claim_url": None, "rules_url": None, "product_norm": "bar"})
        assert domain is None and "no candidate" in reason


class TestLiveRunFindings:
    """Each case here was found by running the verifier against real vendor pages."""

    def test_http_403_is_reported_as_a_gate_not_a_broken_link(self, conn):
        """chatgpt.com/auth/login answers 403: that is a door we do not open."""
        claim = "https://chatgpt.com/auth/login"
        pid = add_perk(conn, perk(product="ChatGPT", claim=claim))
        results = verify.verify_perk(conn, pid, FakeFetcher({claim: {"status": 403, "body": ""}}))
        assert results[0].verdict == "login_required"
        assert results[0].page_accessible is False
        assert status_of(conn, pid) == "dubious"
        row = conn.execute("SELECT detail FROM verification_runs WHERE perk_id = ?", (pid,)).fetchone()
        assert "not bypassed" in row["detail"]

    def test_a_plain_500_is_still_reported_as_a_server_error(self, conn):
        claim = "https://cursor.com/promo"
        pid = add_perk(conn, perk(claim=claim))
        results = verify.verify_perk(conn, pid, FakeFetcher({claim: {"status": 503, "body": ""}}))
        assert results[0].verdict == "error"

    def test_one_page_used_as_both_links_is_fetched_once(self, conn):
        """Pricing pages are commonly both the claim entry and the rules page."""
        same = "https://cursor.com/pricing"
        article_id = article(conn, "https://mp.weixin.qq.com/s/same")
        pid = store.upsert_perk(
            conn,
            PerkFields(product="Cursor", benefit_type="free_membership",
                       entitlement="注册即送 1 个月 Pro", claim_url=same, rules_url=same),
            article_id,
        ).perk_id
        conn.commit()
        fetcher = FakeFetcher({same: {"body": OFFICIAL_PAGE}})
        results = verify.verify_perk(conn, pid, fetcher)
        assert fetcher.requested == [same]
        assert len(results) == 1
        assert conn.execute("SELECT COUNT(*) FROM verification_runs WHERE perk_id = ?",
                           (pid,)).fetchone()[0] == 1

    def test_unparsable_host_is_explained_instead_of_printing_none(self, conn):
        claim = "https://blog.aggregator.example/free"
        pid = add_perk(conn, perk(product="SomeAI", claim=claim))
        fetcher = FakeFetcher({claim: {"error": "ConnectError"}})
        verify.verify_perk(conn, pid, fetcher)
        detail = conn.execute("SELECT detail FROM verification_runs WHERE perk_id = ?",
                              (pid,)).fetchone()["detail"]
        assert "None" not in detail
        assert status_of(conn, pid) != "confirmed"
