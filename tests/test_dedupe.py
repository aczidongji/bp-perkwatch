from __future__ import annotations

import sqlite3

from bp import store
from bp.models import Evidence, PerkFields


def add(conn, url, body, title="标题", source_id=None, published_at=None):
    return store.record_article(
        conn, url=url, title=title, content=body, summary=body[:200],
        source_id=source_id, published_at=published_at,
    )


def article_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) n FROM articles").fetchone()["n"]


class TestArticleLevelDedupe:
    """Levels 1 and 2 of the requirement: repeated crawling of one feed item, and
    different URLs that point at the same article."""

    def test_refetch_of_identical_item_is_duplicate(self, conn):
        first = add(conn, "https://a.com/x", "免费领 100 credits 的活动说明正文")
        second = add(conn, "https://a.com/x", "免费领 100 credits 的活动说明正文")
        assert first.action == "new"
        assert second.action == "duplicate"
        assert second.article_id == first.article_id
        assert article_count(conn) == 1

    def test_refetch_survives_tracking_parameter_change(self, conn):
        body = "免费领 100 credits 的活动说明正文"
        add(conn, "https://a.com/x", body)
        again = add(conn, "https://a.com/x?utm_source=weibo&scene=1", body)
        assert again.action == "duplicate"
        assert article_count(conn) == 1

    def test_same_content_at_different_url_becomes_alias_not_new_row(self, conn):
        """Different公众号/转载链接指向同一篇内容。"""
        body = "Cursor Pro 三个月免费，学生认证后即可领取，有效期至 2026-12-31。"
        original = add(conn, "https://first.com/post/123", body)
        repost = add(conn, "https://mp.weixin.qq.com/s/AbCdEf", body)

        assert repost.action == "alias"
        assert repost.article_id == original.article_id
        assert article_count(conn) == 1

        aliases = {r["url_norm"] for r in conn.execute(
            "SELECT url_norm FROM article_aliases WHERE article_id = ?", (original.article_id,))}
        assert "https://first.com/post/123" in aliases
        assert "https://mp.weixin.qq.com/s/abcdef".replace("abcdef", "AbCdEf") in aliases or \
               "https://mp.weixin.qq.com/s/AbCdEf" in aliases, aliases

    def test_alias_keeps_both_entry_urls_reachable(self, conn):
        body = "同一段正文，被两个站点原样转发，内容完全一致。" * 3
        a = add(conn, "https://aaa.com/1", body)
        add(conn, "https://bbb.com/2", body)
        urls = {r["url_norm"] for r in conn.execute(
            "SELECT url_norm FROM article_aliases WHERE article_id = ?", (a.article_id,))}
        assert len(urls) == 2, "去重不得丢失任何一个入口"

    def test_revised_page_at_same_url_becomes_revision_row(self, conn):
        """同一活动的规则更新：旧版本必须留着，新版本另起一行并链接过去。"""
        old = add(conn, "https://a.com/promo", "免费 100 credits，截止 2026-10-31。")
        new = add(conn, "https://a.com/promo", "免费 200 credits，截止 2026-12-31。")
        assert new.action == "revision"
        assert new.article_id != old.article_id
        assert article_count(conn) == 2
        row = conn.execute("SELECT prev_article_id FROM articles WHERE id = ?",
                           (new.article_id,)).fetchone()
        assert row["prev_article_id"] == old.article_id

    def test_new_revision_is_queued_for_analysis_again(self, conn):
        """A changed page carries new rules, so it must be re-analysed even though
        the URL was seen before."""
        add(conn, "https://a.com/promo", "免费 100 credits，截止 2026-10-31。")
        new = add(conn, "https://a.com/promo", "免费 200 credits，截止 2026-12-31。")
        state = conn.execute("SELECT analyze_state FROM articles WHERE id = ?",
                             (new.article_id,)).fetchone()["analyze_state"]
        assert state == "pending"

    def test_whitespace_only_edit_is_not_a_revision(self, conn):
        body = "免费 100 credits，截止 2026-10-31。"
        first = add(conn, "https://a.com/p", body)
        second = add(conn, "https://a.com/p", body + "\n\n  ")
        assert second.action == "duplicate"
        assert second.article_id == first.article_id

    def test_two_empty_articles_do_not_become_each_others_alias(self, conn):
        """正文为空时 content_hash 全都一样，此前两篇毫不相干的文章会互相挂上对方
        的 URL —— 追溯来源时一个陌生 URL 会莫名出现在另一条福利下面。"""
        first = add(conn, "https://a.com/x", "")
        second = add(conn, "https://b.com/y", "")
        assert first.action == "new" and second.action == "new"
        assert second.article_id != first.article_id
        aliases = {r["url_norm"] for r in conn.execute(
            "SELECT url_norm FROM article_aliases WHERE article_id = ?", (first.article_id,))}
        assert aliases == {"https://a.com/x"}, aliases

    def test_refetching_the_same_empty_article_is_still_a_duplicate(self, conn):
        add(conn, "https://a.com/x", "")
        assert add(conn, "https://a.com/x", "").action == "duplicate"
        assert article_count(conn) == 1

    def test_unnormalizable_url_still_dedupes(self, conn):
        first = add(conn, "weird://not a real url", "正文正文正文")
        second = add(conn, "weird://not a real url", "正文正文正文")
        assert second.action in ("duplicate", "alias")
        assert article_count(conn) == 1


def perk(product, benefit, entitlement, quota=None, ends=None, claim=None, rules=None,
         evidence_field="entitlement", quote=None, conf=0.8) -> PerkFields:
    return PerkFields(
        product=product, benefit_type=benefit, entitlement=entitlement, quota=quota,
        ends_at=ends, claim_url=claim, rules_url=rules, confidence=conf,
        evidence=[Evidence(field=evidence_field, quote=quote if quote is not None else entitlement)],
    )


def upsert(conn, p, article_id):
    return store.upsert_perk(conn, p, article_id,
                             evidence=[{"field": e.field, "quote": e.quote} for e in p.evidence])


def link_article(conn, url="https://a.com/1", body=None) -> int:
    """Distinct URLs must yield distinct articles, so the default body is unique."""
    return add(conn, url, body if body is not None else f"来自 {url} 的活动文章正文").article_id


class TestPerkLevelDedupe:
    """Levels 3 and 4: one benefit promoted across several accounts, and repeated
    promotion of the same product+benefit."""

    def test_identical_claim_from_two_sources_merges_into_one_perk(self, conn):
        a1 = link_article(conn, "https://first.com/1")
        a2 = link_article(conn, "https://second.com/2")
        p = perk("Cursor", "free_membership", "Cursor Pro 三个月", ends="2026-12-31",
                 claim="https://cursor.com/pricing")
        first = upsert(conn, p, a1)
        second = upsert(conn, p, a2)
        assert first.action == "created"
        assert second.action == "unchanged"
        assert first.perk_id == second.perk_id
        assert conn.execute("SELECT COUNT(*) n FROM perks").fetchone()["n"] == 1

    def test_a_repost_of_the_same_rules_does_not_move_the_perk_timestamp(self, conn):
        """一夜几百条重复条目。若每次 merge 都推高 updated_at，日报里「需要人工判断」
        的时间窗就永远开着，同一条福利会在之后每一封邮件里重复出现。"""
        p = perk("Cursor", "free_membership", "Cursor Pro 三个月",
                 claim="https://cursor.com/pricing")
        perk_id = upsert(conn, p, link_article(conn, "https://first.com/1")).perk_id
        conn.commit()
        before = conn.execute("SELECT updated_at, confidence FROM perks WHERE id = ?",
                              (perk_id,)).fetchone()
        assert upsert(conn, p, link_article(conn, "https://second.com/2")).action == "unchanged"
        conn.commit()
        after = conn.execute("SELECT updated_at, confidence FROM perks WHERE id = ?",
                             (perk_id,)).fetchone()
        assert after["updated_at"] == before["updated_at"]

    def test_a_higher_confidence_sighting_is_still_recorded(self, conn):
        p = perk("Cursor", "free_membership", "Cursor Pro 三个月",
                 claim="https://cursor.com/pricing", conf=0.6)
        perk_id = upsert(conn, p, link_article(conn, "https://first.com/1")).perk_id
        stronger = perk("Cursor", "free_membership", "Cursor Pro 三个月",
                        claim="https://cursor.com/pricing", conf=0.9)
        assert upsert(conn, stronger, link_article(conn, "https://second.com/2")).action == "unchanged"
        row = conn.execute("SELECT confidence FROM perks WHERE id = ?", (perk_id,)).fetchone()
        assert row["confidence"] == 0.9
        weaker = perk("Cursor", "free_membership", "Cursor Pro 三个月",
                      claim="https://cursor.com/pricing", conf=0.7)
        upsert(conn, weaker, link_article(conn, "https://third.com/3"))
        assert conn.execute("SELECT confidence FROM perks WHERE id = ?",
                            (perk_id,)).fetchone()["confidence"] == 0.9

    def test_merge_keeps_every_source(self, conn):
        ids = [link_article(conn, f"https://src{i}.com/{i}") for i in range(3)]
        p = perk("Cursor", "free_membership", "Cursor Pro 三个月",
                 claim="https://cursor.com/pricing")
        perk_id = upsert(conn, p, ids[0]).perk_id
        for aid in ids[1:]:
            upsert(conn, p, aid)
        linked = conn.execute("SELECT COUNT(*) n FROM perk_sources WHERE perk_id = ?",
                              (perk_id,)).fetchone()["n"]
        assert linked == 3

    def test_alias_url_product_spelling_variants_collapse(self, conn):
        """Cursor / Cursor AI / Cursor 编辑器 是同一个产品。"""
        a1 = link_article(conn, "https://a.com/1")
        a2 = link_article(conn, "https://a.com/2")
        r1 = upsert(conn, perk("Cursor", "free_credits", "100 次免费补全",
                               quota="100 次", claim="https://cursor.com/x"), a1)
        r2 = upsert(conn, perk("Cursor AI 编辑器", "free_credits", "100 次免费补全",
                               quota="100 次", claim="https://cursor.com/x"), a2)
        assert r1.perk_id == r2.perk_id
        assert conn.execute("SELECT COUNT(*) n FROM perks").fetchone()["n"] == 1

    def test_different_quotas_on_same_product_stay_separate(self, conn):
        """100 credits 和 1000 credits 是两个不同权益，不能归并成一条。"""
        a1 = link_article(conn, "https://a.com/1")
        a2 = link_article(conn, "https://a.com/2")
        r1 = upsert(conn, perk("Cursor", "free_credits", "免费额度", quota="100 credits",
                               claim="https://cursor.com/a"), a1)
        r2 = upsert(conn, perk("Cursor", "free_credits", "免费额度", quota="1000 credits",
                               claim="https://cursor.com/b"), a2)
        assert r1.perk_id != r2.perk_id
        assert conn.execute("SELECT COUNT(*) n FROM perks").fetchone()["n"] == 2

    def test_two_different_benefits_without_a_quota_do_not_overwrite_each_other(self, conn):
        """同产品、同类型、同一官方域名、两条都解析不出额度数字时，此前只凭域名就
        合并，后一条把前一条的 entitlement / claim_url 整条顶掉——一条真实存在的
        福利就这么没了。"""
        a1 = link_article(conn, "https://a.com/1")
        a2 = link_article(conn, "https://a.com/2")
        r1 = upsert(conn, perk("Cursor", "free_trial", "免费试用 Pro 版",
                               claim="https://cursor.com/a"), a1)
        r2 = upsert(conn, perk("Cursor", "free_trial", "免费领取会员资格",
                               claim="https://cursor.com/b"), a2)
        assert r1.perk_id != r2.perk_id
        kept = {r["entitlement"] for r in conn.execute("SELECT entitlement FROM perks")}
        assert kept == {"免费试用 Pro 版", "免费领取会员资格"}, "两条权益都必须还在"

    def test_the_same_benefit_reworded_still_merges(self, conn):
        """防过合并不能矫枉过正：同一件事换个说法仍然要合成一条。"""
        a1 = link_article(conn, "https://a.com/1")
        a2 = link_article(conn, "https://a.com/2")
        r1 = upsert(conn, perk("Cursor", "free_membership", "Cursor Pro 一个月",
                               claim="https://cursor.com/a"), a1)
        r2 = upsert(conn, perk("Cursor", "free_membership", "Cursor Pro 1 个月",
                               claim="https://cursor.com/b"), a2)
        assert r1.perk_id == r2.perk_id

    def test_a_sighting_with_no_entitlement_still_merges_onto_one_that_has_it(self, conn):
        """一方没有权益描述时无从比较，仍按额度/域名合并，且不得把已有的描述清空。"""
        a1 = link_article(conn, "https://a.com/1")
        a2 = link_article(conn, "https://a.com/2")
        r1 = upsert(conn, perk("Cursor", "free_membership", "Cursor Pro 三个月",
                               claim="https://cursor.com/p"), a1)
        thin = PerkFields(product="Cursor", benefit_type="free_membership",
                          entitlement=None, claim_url="https://cursor.com/p",
                          evidence=[Evidence(field="product", quote="Cursor")])
        r2 = upsert(conn, thin, a2)
        assert r1.perk_id == r2.perk_id
        assert conn.execute("SELECT entitlement FROM perks WHERE id = ?",
                            (r1.perk_id,)).fetchone()["entitlement"] == "Cursor Pro 三个月"

    def test_different_benefit_types_same_product_stay_separate(self, conn):
        a1 = link_article(conn, "https://a.com/1")
        a2 = link_article(conn, "https://a.com/2")
        r1 = upsert(conn, perk("Kimi", "free_membership", "免费会员", claim="https://kimi.com/m"), a1)
        r2 = upsert(conn, perk("Kimi", "student_discount", "学生五折", claim="https://kimi.com/m"), a2)
        assert r1.perk_id != r2.perk_id

    def test_rule_update_records_new_version_and_keeps_history(self, conn):
        a1 = link_article(conn, "https://a.com/1")
        a2 = link_article(conn, "https://a.com/2")
        p1 = perk("Cursor", "free_membership", "Cursor Pro 一个月",
                  ends="2026-10-31", claim="https://cursor.com/pricing")
        first = upsert(conn, p1, a1)
        p2 = perk("Cursor", "free_membership", "Cursor Pro 三个月",
                  ends="2026-12-31", claim="https://cursor.com/pricing")
        second = upsert(conn, p2, a2)

        assert second.action == "updated"
        assert second.perk_id == first.perk_id
        assert "ends_at" in second.changed_fields

        versions = conn.execute(
            "SELECT reason, snapshot FROM perk_versions WHERE perk_id = ? ORDER BY id",
            (first.perk_id,)).fetchall()
        assert [v["reason"] for v in versions] == ["created", "rule_update"]
        assert "2026-10-31" in versions[0]["snapshot"]
        assert "2026-12-31" in versions[1]["snapshot"]
        assert conn.execute("SELECT ends_at FROM perks WHERE id = ?",
                            (first.perk_id,)).fetchone()["ends_at"] == "2026-12-31"

    def test_newer_article_with_missing_fields_never_erases_known_values(self, conn):
        """转载文章信息更少时，已有的截止日期不能被 null 覆盖掉。"""
        a1 = link_article(conn, "https://a.com/1")
        a2 = link_article(conn, "https://a.com/2")
        upsert(conn, perk("Cursor", "free_membership", "Cursor Pro 一个月",
                          ends="2026-12-31", claim="https://cursor.com/pricing"), a1)
        upsert(conn, perk("Cursor", "free_membership", "Cursor Pro 一个月",
                          ends=None, claim="https://cursor.com/pricing"), a2)
        row = conn.execute("SELECT ends_at FROM perks WHERE product_norm LIKE '%cursor%'").fetchone()
        assert row["ends_at"] == "2026-12-31"

    def test_quota_wording_variants_collapse(self, conn):
        """100 Credits 与 100积分 应归并为同一额度。"""
        a1 = link_article(conn, "https://a.com/1")
        a2 = link_article(conn, "https://a.com/2")
        r1 = upsert(conn, perk("Windsurf", "free_credits", "免费额度", quota="100 Credits",
                               claim="https://windsurf.com/x"), a1)
        r2 = upsert(conn, perk("Windsurf", "free_credits", "免费额度", quota="100积分",
                               claim="https://windsurf.com/x"), a2)
        assert r1.perk_id == r2.perk_id

    def test_unidentified_benefit_type_does_not_collide_with_itself(self, conn):
        """benefit_type 为空的候选不能被 canonical_key 相互覆盖。"""
        a1 = link_article(conn, "https://a.com/1")
        a2 = link_article(conn, "https://a.com/2")
        p1 = PerkFields(product="Foo", benefit_type=None, entitlement="某种免费活动",
                        evidence=[Evidence(field="entitlement", quote="某种免费活动")])
        p2 = PerkFields(product="Foo", benefit_type=None, entitlement="另一种不同的活动",
                        evidence=[Evidence(field="entitlement", quote="另一种不同的活动")])
        r1 = upsert(conn, p1, a1)
        r2 = upsert(conn, p2, a2)
        assert r1.perk_id != r2.perk_id

    def test_perk_count_stays_one_after_repeated_identical_ingest(self, conn):
        """同一文章反复入库（模拟 RSS 重复投递）不应产生福利膨胀。"""
        for _ in range(5):
            aid = link_article(conn, "https://a.com/same", "同一篇正文内容，重复投递。")
            upsert(conn, perk("Cursor", "free_credits", "100 次补全", quota="100 次",
                              claim="https://cursor.com/p"), aid)
        assert conn.execute("SELECT COUNT(*) n FROM perks").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) n FROM perk_sources").fetchone()["n"] == 1


class TestCanonicalKey:
    def test_key_is_stable_for_same_inputs(self, conn):
        p = perk("Cursor", "free_credits", "100 次", quota="100 次", claim="https://cursor.com/a")
        assert store.canonical_key(p) == store.canonical_key(p)

    def test_key_ignores_url_tracking_noise(self, conn):
        p1 = perk("Cursor", "free_credits", "100 次", quota="100 次",
                  claim="https://cursor.com/a?utm_source=x")
        p2 = perk("Cursor", "free_credits", "100 次", quota="100 次", claim="https://cursor.com/a")
        assert store.claim_identity(p1) == store.claim_identity(p2) == "cursor.com"
