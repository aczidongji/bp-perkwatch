"""入库前的最后一道闸门：模型说"这是福利"，不等于它就是福利。

is_promotion 是模型自报的。实测它把两类东西都判成了促销：
  · 产品发布稿（WeatherNext 气象模型、Threat Signals、DeepMind 科学组合）
  · **常驻套餐说明**（"Free plan includes 0.5 GB"、"Free users are limited to
    2000 completions"、"Max 含 $100/month credits"）—— 有额度、看着很具体，
    其实是写在定价页上的产品固有属性，没有获取门槛也没有时间边界。
这道闸门只看机械信号，不听模型自述。
"""
from __future__ import annotations

import pytest

from bp import pipeline, store
from bp.llm import AnalysisReport, PerkOutcome
from bp.models import Evidence, PerkFields
from bp.prefilter import looks_like_promotion

try:
    from helpers import add_perk, authorise
except ImportError:  # pragma: no cover - pytest inserts tests/ on sys.path
    add_perk = authorise = None


class StubExtractor:
    """只回抽取结果，不碰网络。"""

    def __init__(self, perks):
        self.perks = perks
        self.calls = 0

    def analyse(self, article_id, article_text, title):
        self.calls += 1
        return AnalysisReport(
            article_id=article_id, is_promotion=True,
            outcomes=[PerkOutcome(perk=p) for p in self.perks],
        )


def _article(conn, url: str) -> int:
    body = "某 AI 产品限时免费领取，注册即送额度，正文需要足够长才会被送进模型。" * 12
    return store.record_article(conn, url=url, title="某产品活动", content=body).article_id


def _perk(product: str, entitlement: str, **kw) -> PerkFields:
    claim = kw.pop("claim", None)
    if claim:
        kw["claim_url"] = claim
    return PerkFields(
        product=product, benefit_type=kw.pop("benefit_type", "free_credits"),
        entitlement=entitlement, confidence=0.8,
        evidence=[Evidence(field="product", quote=product),
                  Evidence(field="entitlement", quote=entitlement)], **kw,
    )


def _run(conn, *perks) -> pipeline.RunCounts:
    _article(conn, f"https://a.example/{abs(hash(tuple(p.entitlement or '' for p in perks)))}")
    return pipeline.analyse_pending(conn, force=True, extractor=StubExtractor(list(perks)))


class TestLooksLikePromotion:
    """真福利自带边界：写清怎么拿到 / 有起止时间 / 有领取入口。
    只有额度、没有边界的，是定价页上的产品属性。"""

    def test_a_claim_entry_is_enough(self):
        assert looks_like_promotion(_perk("Cursor", "送 1 个月 Pro",
                                          claim_url="https://cursor.com/promo"))

    def test_a_deadline_is_enough(self):
        assert looks_like_promotion(_perk("Cursor", "送 1 个月 Pro", ends_at="2026-12-31"))

    def test_how_to_get_it_in_words_is_enough(self):
        assert looks_like_promotion(_perk("Kimi", "学生认证后可免费使用"))
        assert looks_like_promotion(_perk("某大会", "订阅者可享受 20% 折扣"))
        assert looks_like_promotion(_perk("Kiro", "免费使用一整年，无需信用卡"))

    def test_a_quantity_alone_is_not_enough(self):
        """一条额度说明不了任何事：它是"套餐里有什么"，不是"你能去领什么"。"""
        assert not looks_like_promotion(_perk("Cursor", "每月免费额度", quota="2000 次"))

    def test_a_standing_free_tier_is_not_a_promotion(self):
        """真实库里的第 8 条：Free plan includes 0.5 GB of ingestion per day。"""
        assert not looks_like_promotion(
            _perk("Cloudflare Traces", "Free plan includes 0.5 GB of ingestion per day",
                  quota="0.5 GB per day"))

    def test_a_usage_limit_is_not_a_promotion(self):
        """真实库里的第 9 条：Free users are limited to 2000 completions —— 是限制不是福利。"""
        assert not looks_like_promotion(
            _perk("GitHub Copilot",
                  "Free users are limited to 2000 completions and 50 chat requests",
                  quota="2000 completions"))

    def test_a_paid_plan_entitlement_is_not_a_perk(self):
        """真实库里的第 10/27 条：Max 套餐含 $100/month credits —— 要花钱买的。"""
        assert not looks_like_promotion(
            _perk("GitHub Copilot", "includes $100/month in GitHub AI Credits",
                  quota="$100/month"))

    def test_a_bare_product_description_is_not(self):
        assert not looks_like_promotion(
            _perk("WeatherNext", "可在免费的公共 Colab notebook 中运行"))
        assert not looks_like_promotion(_perk("Threat Signals", "将开源报告转成结构化数据"))
        assert not looks_like_promotion(_perk("某短剧", "剧集全部免费，但会插入广告"))

    def test_a_plan_entry_link_without_any_boundary_is_still_rejected(self):
        """有链接也不能救：链接指向定价页，文本仍然只是套餐内容。"""
        assert not looks_like_promotion(
            _perk("Cloudflare", "Free plan includes 0.5 GB of ingestion per day",
                  quota="0.5 GB per day", claim_url="https://cloudflare.com/plans"))


class TestActionabilityGate:
    def test_a_product_announcement_is_counted_and_dropped(self, conn):
        counts = _run(conn, _perk("WeatherNext", "可在免费的公共 Colab notebook 中运行"))
        assert counts.perks_unactionable == 1
        assert counts.perks_created == 0
        assert conn.execute("SELECT COUNT(*) n FROM perks").fetchone()["n"] == 0

    def test_a_free_tier_description_is_counted_and_dropped(self, conn):
        counts = _run(conn, _perk("Cloudflare Traces",
                                  "Free plan includes 0.5 GB of ingestion per day",
                                  quota="0.5 GB per day"))
        assert counts.perks_unactionable == 1
        assert conn.execute("SELECT COUNT(*) n FROM perks").fetchone()["n"] == 0

    def test_a_real_offer_with_a_claim_url_is_stored(self, conn):
        counts = _run(conn, _perk("Cursor", "注册即送 1 个月 Pro",
                                  claim_url="https://cursor.com/promo"))
        assert counts.perks_created == 1
        assert counts.perks_unactionable == 0

    def test_a_real_offer_with_a_deadline_is_stored(self, conn):
        counts = _run(conn, _perk("Cursor", "送 1 个月 Pro", ends_at="2026-12-31"))
        assert counts.perks_created == 1
        assert counts.perks_unactionable == 0

    def test_a_student_offer_with_no_link_no_date_is_still_stored(self, conn):
        """没有链接、没有日期、没有额度，但它写明了怎么拿到。字段少不等于不是福利，
        不能因为字段少就把真福利丢掉。"""
        counts = _run(conn, _perk("Kimi", "学生认证后可免费使用",
                                  benefit_type="student_discount"))
        assert counts.perks_created == 1
        assert counts.perks_unactionable == 0

def status_of(conn, pid):
    return conn.execute("SELECT status FROM perks WHERE id = ?", (pid,)).fetchone()["status"]


class TestRepromotionGate:
    """`bp gate`：词表可以改，改完已入库的旧条目要能重新判，否则被误判成福利的
    套餐说明会天天回到邮件里（needs_review 现在是持续列出的）。"""

    def test_a_plan_tier_already_stored_is_rejected(self, conn):
        pid = add_perk(conn, _perk("Cloudflare Traces",
                                   "Free plan includes 0.5 GB of ingestion per day",
                                   quota="0.5 GB per day"))
        result = pipeline.apply_promotion_gate(conn)
        assert result["checked"] == 1 and result["gated"] == 1
        assert status_of(conn, pid) == "invalid"
        reason = conn.execute("SELECT review_reason FROM perks WHERE id = ?",
                              (pid,)).fetchone()["review_reason"]
        assert "不是促销" in reason

    def test_a_real_perk_survives_the_recheck(self, conn):
        pid = add_perk(conn, _perk("Kimi", "学生认证后可免费使用",
                                   benefit_type="student_discount"))
        assert pipeline.apply_promotion_gate(conn)["gated"] == 0
        assert status_of(conn, pid) == "pending"

    def test_the_recheck_takes_the_junk_out_of_the_mail(self, conn):
        from bp import lifecycle, report
        from datetime import date

        pid = add_perk(conn, _perk("GitHub Copilot",
                                   "Free users are limited to 2000 completions",
                                   quota="2000 completions"))
        lifecycle.flag_incomplete(conn)   # 真实流程里 tick 先打标
        conn.commit()
        before = report.generate(conn, on_day=date(2026, 10, 15), write_file=False)
        first_day = [p["id"] for p in before.sections["new_found"]]
        assert pid in first_day, "第一次它作为「新发现」露面"

        pipeline.apply_promotion_gate(conn)
        # 用固定且更早的时间当送达水位：同一次秒内的时间戳字符串相等，
        # 而增量窗口是严格大于，会让这条"已失效"刚好被漏掉。
        conn.execute("UPDATE reports SET sent_at = ? WHERE day = ?",
                     ("2026-10-15T00:00:00Z", before.day))
        conn.commit()
        after = report.generate(conn, on_day=date(2026, 10, 16), write_file=False)
        still_listed = [p["id"] for key in ("new_found", "needs_review", "confirmed_live",
                                            "expiring_soon", "rule_updates")
                        for p in after.sections[key]]
        assert pid not in still_listed, "误判条目不该再天天回来"
        # 但不会凭空蒸发：它以"本周期内过期或失效"出现一次，原因写在里面
        expired = [p for p in after.sections["recently_expired"] if p["id"] == pid]
        assert expired, "失效要露面一次，而不是悄悄消失"
        assert "不是促销" in expired[0]["review_reason"]

    def test_confirmed_perks_are_never_reclassified(self, conn):
        """官方页面确认过的福利，不能因为关键词表收紧就被一句"不是促销"推翻。"""
        claim = "https://kiro.dev/free"
        pid = add_perk(conn, _perk("Kiro", "免费使用一整年", claim=claim))
        authorise(conn, pid, claim)
        store.set_status(conn, pid, "confirmed", "官方页面仍可领取", "verification")
        conn.commit()
        assert pipeline.apply_promotion_gate(conn)["gated"] == 0
        assert status_of(conn, pid) == "confirmed"

    def test_dry_run_reports_without_writing(self, conn):
        pid = add_perk(conn, _perk("Cloudflare Traces",
                                   "Free plan includes 0.5 GB per day", quota="0.5 GB"))
        result = pipeline.apply_promotion_gate(conn, dry_run=True)
        assert result["gated"] == 1 and result["items"][0]["id"] == pid
        assert status_of(conn, pid) == "pending", "dry-run 不得改数据"

    def test_it_is_idempotent(self, conn):
        add_perk(conn, _perk("Cloudflare Traces", "Free plan includes 0.5 GB per day",
                             quota="0.5 GB"))
        assert pipeline.apply_promotion_gate(conn)["gated"] == 1
        assert pipeline.apply_promotion_gate(conn)["gated"] == 0, "判过的不会再判一次"

    def test_an_interrupted_run_is_closed_instead_of_staying_in_progress(self, conn, monkeypatch):
        """Ctrl-C / SIGTERM 不走 except Exception，此前 runs 里会留下一行
        finished_at 为空、没有计数也没有错误的幽灵运行，而它会被当成"最近一次"。"""

        def interrupt(_conn, **_kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(pipeline, "collect", interrupt)
        with pytest.raises(KeyboardInterrupt):
            pipeline.run_once(conn)
        row = conn.execute("SELECT finished_at, error FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row["finished_at"] is not None
        assert "中断" in row["error"]

    def test_a_successful_run_keeps_its_own_result(self, conn, monkeypatch):
        monkeypatch.setattr(pipeline, "collect", lambda _conn, **_kw: pipeline.RunCounts())
        monkeypatch.setattr(pipeline, "analyse_pending", lambda _conn, **_kw: pipeline.RunCounts())
        monkeypatch.setattr(pipeline.lifecycle, "sweep_expired", lambda _c, **_kw: [])
        monkeypatch.setattr(pipeline, "verify_new", lambda _c, **_kw: 0)
        pipeline.run_once(conn)
        row = conn.execute("SELECT finished_at, error, counts FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row["finished_at"] and row["counts"]
        assert not row["error"], "正常结束不能被记成中断"

    def test_the_article_is_still_marked_done_when_everything_is_dropped(self, conn):
        """丢弃发生在入库之后的分析阶段，文章不能因此卡在 pending 里反复送模型。"""
        _run(conn, _perk("某大会", "现场提供免费咖啡"))
        states = {r["analyze_state"] for r in conn.execute("SELECT analyze_state FROM articles")}
        assert states == {"done"}
