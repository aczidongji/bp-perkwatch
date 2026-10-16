"""模型 JSON 解析与反捏造闸门。

用户要求里最硬的两条——「禁止仅凭公众号文章或 LLM 判断就标记为已确认」
「缺少关键证据时不得编造字段」——在这里由代码强制执行，不依赖提示词是否被听话。
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError, InternalServerError, RateLimitError
from pydantic import ValidationError

from bp import llm, prefilter
from bp.models import (
    EVIDENCE_REQUIRED_FIELDS,
    Evidence,
    PerkFields,
    evidence_holds,
    extraction_json_schema,
    parse_extraction,
    strip_unsupported_fields,
)
from bp.llm import Extractor, _invented_urls, off_peak_window_closed
from bp import timeutil

ARTICLE = (
    "Cursor 今天宣布面向所有新用户放开 Pro 会员：注册即送 1 个月 Pro，"
    "活动截止 2026 年 12 月 31 日。领取入口 https://cursor.com/promo ，"
    "详细规则见 https://cursor.com/terms 。学生认证后还可享五折。"
)

VALID = json.dumps({
    "is_promotion": True,
    "perks": [{
        "product": "Cursor",
        "benefit_type": "free_membership",
        "entitlement": "注册即送 1 个月 Pro",
        "quota": "1 个月",
        "starts_at": None,
        "ends_at": "2026-12-31",
        "claim_url": "https://cursor.com/promo",
        "rules_url": "https://cursor.com/terms",
        "region": None,
        "eligibility": "新用户",
        "new_users_only": True,
        "student_verification_required": True,
        "payment_method_required": None,
        "invite_required": None,
        "confidence": 0.9,
        "evidence": [
            {"field": "product", "quote": "Cursor"},
            {"field": "benefit_type", "quote": "Pro 会员"},
            {"field": "entitlement", "quote": "注册即送 1 个月 Pro"},
            {"field": "quota", "quote": "1 个月 Pro"},
            {"field": "ends_at", "quote": "活动截止 2026 年 12 月 31 日"},
            {"field": "claim_url", "quote": "https://cursor.com/promo"},
            {"field": "rules_url", "quote": "https://cursor.com/terms"},
            {"field": "new_users_only", "quote": "新用户"},
            {"field": "student_verification_required", "quote": "学生认证"},
            {"field": "eligibility", "quote": "面向所有新用户"},
        ],
    }],
    "notes": None,
}, ensure_ascii=False)


class TestParseModelJson:
    def test_plain_object_parses(self):
        result = parse_extraction(VALID)
        assert result.is_promotion is True
        assert result.perks[0].product == "Cursor"
        assert result.perks[0].benefit_type == "free_membership"

    def test_code_fenced_json_parses(self):
        """模型经常无视“只输出 JSON”，把结果包在 ```json 里。"""
        result = parse_extraction("```json\n" + VALID + "\n```")
        assert len(result.perks) == 1

    def test_surrounding_prose_is_ignored(self):
        result = parse_extraction("好的，以下是抽取结果：\n" + VALID + "\n\n以上。")
        assert result.perks[0].ends_at == "2026-12-31"

    def test_fenced_block_without_newline(self):
        assert parse_extraction("```" + VALID + "```").perks[0].product == "Cursor"

    @pytest.mark.parametrize("raw", [None, "", "   ", "这是一个没有 JSON 的回答",
                                     "只有前半截 { 没有收尾"])
    def test_unusable_output_raises(self, raw):
        with pytest.raises(ValueError):
            parse_extraction(raw)

    def test_truncated_json_raises_rather_than_half_matching(self):
        with pytest.raises(ValueError):
            parse_extraction(VALID[: len(VALID) // 2])

    def test_missing_is_promotion_is_inferred_from_the_models_own_perks(self):
        """json_object 模式下模型经常只回 perks。这个判断读的是模型自己的输出结构，
        不是文章内容，因此不会编造原文没有的事实。"""
        broken = json.loads(VALID)
        del broken["is_promotion"]
        assert parse_extraction(json.dumps(broken, ensure_ascii=False)).is_promotion is True

        empty = json.loads(VALID)
        empty["perks"] = []
        del empty["is_promotion"]
        assert parse_extraction(json.dumps(empty, ensure_ascii=False)).is_promotion is False

    def test_evidence_without_its_quote_raises(self):
        broken = json.loads(VALID)
        del broken["perks"][0]["evidence"][0]["quote"]
        with pytest.raises(ValidationError):
            parse_extraction(json.dumps(broken, ensure_ascii=False))

    def test_perk_field_the_model_left_out_is_stored_as_null_not_guessed(self):
        """Schema 保证键都在；真的缺键时也不得补出一个值。"""
        broken = json.loads(VALID)
        for key in ("quota", "region", "ends_at"):
            del broken["perks"][0][key]
        result = parse_extraction(json.dumps(broken, ensure_ascii=False))
        assert result.perks[0].quota is None
        assert result.perks[0].region is None
        assert result.perks[0].ends_at is None
        assert result.perks[0].product == "Cursor"

    def test_stray_keys_are_dropped_instead_of_losing_the_article(self):
        """DeepSeek 不接受 json_schema 严格模式，json_object 下它会自己加 title / name。
        这些键不对应任何可核验字段，为它们判整篇失败是把真数据扔掉。"""
        broken = json.loads(VALID)
        broken["perks"][0]["probably_a_hallucination"] = "xxx"
        broken["perks"][0]["title"] = "Cursor 学生礼包"
        broken["perks"][0]["evidence"][0]["why"] = "因为"
        broken["notes_zh"] = "多余"
        result = parse_extraction(json.dumps(broken, ensure_ascii=False))
        assert result.perks[0].product == "Cursor"
        assert not hasattr(result.perks[0], "title")
        assert "probably_a_hallucination" not in result.perks[0].model_dump()

    def test_dropping_stray_keys_reports_what_was_dropped(self):
        from bp.models import drop_stray_keys

        payload = json.loads(VALID)
        payload["perks"][0]["probably_not_a_field"] = "x"
        assert drop_stray_keys(payload) == ["perks[].probably_not_a_field"]

    def test_the_models_synonyms_are_moved_onto_the_real_fields(self):
        """真实跑出来的形状：权益写在 description、产品写在 provider。
        键名对不上是命名问题不是编造，归位之后仍然要过逐字引文闸门。"""
        from bp.models import drop_stray_keys

        payload = json.loads(VALID)
        perk = payload["perks"][0]
        del perk["entitlement"], perk["product"]
        perk["description"] = "注册即送 1 个月 Pro"
        perk["provider"] = "Cursor"
        perk["evidence"] = [{"field": "description", "quote": "注册即送 1 个月 Pro"},
                            {"field": "provider", "quote": "Cursor 今天宣布"}]
        drop_stray_keys(payload)
        assert perk["entitlement"] == "注册即送 1 个月 Pro"
        assert perk["product"] == "Cursor"
        assert {e["field"] for e in perk["evidence"]} == {"entitlement", "product"}, \
            "引文的字段标签也要一起归位，否则回溯会误判成无据"

    def test_a_real_entitlement_is_not_overwritten_by_a_synonym(self):
        from bp.models import drop_stray_keys

        payload = json.loads(VALID)
        payload["perks"][0]["description"] = "模型自己另写的一句"
        drop_stray_keys(payload)
        assert payload["perks"][0]["entitlement"] != "模型自己另写的一句"

    def test_a_real_field_error_is_still_fatal(self):
        """宽容只针对多余键：字段值本身错了必须仍然判失败，不然防线就白设了。"""
        broken = json.loads(VALID)
        broken["perks"][0]["product"] = {"not": "a string"}
        with pytest.raises(ValidationError):
            parse_extraction(json.dumps(broken, ensure_ascii=False))

    def test_illegal_benefit_type_raises(self):
        broken = json.loads(VALID)
        broken["perks"][0]["benefit_type"] = "free_lunch_forever"
        with pytest.raises(ValidationError):
            parse_extraction(json.dumps(broken, ensure_ascii=False))

    def test_is_promotion_false_must_not_leak_perks_into_the_pipeline(self):
        payload = json.loads(VALID)
        payload["is_promotion"] = False
        result = parse_extraction(json.dumps(payload, ensure_ascii=False))
        assert result.perks, "schema 允许带着 perks 回来"
        # analyse() 只在 is_promotion 为真时才消费 perks。
        outcomes = [p for p in (result.perks if result.is_promotion else [])]
        assert outcomes == []

    def test_evidence_quote_too_short_is_rejected(self):
        broken = json.loads(VALID)
        broken["perks"][0]["evidence"][0]["quote"] = "a"
        with pytest.raises(ValidationError):
            parse_extraction(json.dumps(broken, ensure_ascii=False))

    def test_too_many_perks_are_capped(self):
        payload = json.loads(VALID)
        one = payload["perks"][0]
        payload["perks"] = [{**one, "entitlement": f"第 {i} 个活动"} for i in range(40)]
        assert len(parse_extraction(json.dumps(payload, ensure_ascii=False)).perks) == 12


class TestDateAndNumberCoercion:
    @pytest.mark.parametrize("value,expected", [
        ("2026-12-31", "2026-12-31"),
        ("2026/12/31", "2026-12-31"),
        ("2026年12月31日", "2026-12-31"),
        ("2026.12.31", "2026-12-31"),
        ("  2026-12-31  ", "2026-12-31"),
    ])
    def test_date_spellings_normalise(self, value, expected):
        assert PerkFields(ends_at=value).ends_at == expected

    @pytest.mark.parametrize("value", [None, "", "null", "N/A", "无", "未知",
                                       "月底", "下周", "长期有效"])
    def test_absent_or_relative_date_becomes_null_never_a_guess(self, value):
        """规则 2：相对表述必须落成 null，宁可没有截止日期也不编一个。"""
        assert PerkFields(ends_at=value).ends_at is None

    def test_confidence_reported_on_o_to_100_scale_is_rescaled(self):
        assert PerkFields(confidence=80).confidence == 0.8

    def test_confidence_already_on_the_right_scale_is_untouched(self):
        assert PerkFields(confidence=0.8).confidence == 0.8

    def test_blank_url_becomes_none(self):
        assert PerkFields(claim_url="   ").claim_url is None


class TestAntiFabrication:
    def test_quote_must_occur_in_the_source(self):
        assert evidence_holds("注册即送 1 个月 Pro", ARTICLE) is True
        assert evidence_holds("注册即送 3 个月 Pro", ARTICLE) is False
        assert evidence_holds("  注册即送 1 个月 Pro  ", ARTICLE) is True, "空白不参与比对"

    def test_ellipsis_tolerates_a_trimmed_passage(self):
        assert evidence_holds("活动截止 …… 详细规则见", ARTICLE) is True

    def test_genuine_fields_survive(self):
        p = PerkFields(
            product="Cursor", benefit_type="free_membership",
            entitlement="注册即送 1 个月 Pro", ends_at="2026-12-31",
            claim_url="https://cursor.com/promo", confidence=0.9,
            evidence=[
                Evidence(field="product", quote="Cursor"),
                Evidence(field="benefit_type", quote="Pro 会员"),
                Evidence(field="entitlement", quote="注册即送 1 个月 Pro"),
                Evidence(field="ends_at", quote="活动截止 2026 年 12 月 31 日"),
                Evidence(field="claim_url", quote="https://cursor.com/promo"),
            ],
        )
        cleaned, dropped = strip_unsupported_fields(p, ARTICLE)
        assert dropped == []
        assert cleaned.ends_at == "2026-12-31"
        assert cleaned.confidence == 0.9

    def test_fabricated_quote_nulls_that_field_and_caps_confidence(self):
        p = PerkFields(
            product="Cursor", benefit_type="free_membership",
            entitlement="注册即送 6 个月 Pro", ends_at="2027-01-01",
            confidence=0.95,
            evidence=[Evidence(field="entitlement", quote="注册即送 6 个月 Pro")],
        )
        cleaned, dropped = strip_unsupported_fields(p, ARTICLE)
        assert cleaned.entitlement is None, "无证据的字段必须置空，不能留着展示"
        assert "entitlement" in dropped
        assert cleaned.confidence <= 0.4, "被削掉证据的结果不能还顶着高可信度"

    def test_every_evidence_required_field_is_checkable(self):
        """闸门字段表里的每一项，在没有引文时都必须真的被剔掉。"""
        p = PerkFields(**{
            "product": "某产品", "benefit_type": "other", "entitlement": "某种权益",
            "quota": "500 次", "starts_at": "2026-11-01", "ends_at": "2026-12-31",
            "claim_url": "https://cursor.com/pricing",
            "rules_url": "https://cursor.com/legal",
            "region": "中国大陆", "eligibility": "仅限新用户",
            "new_users_only": True, "student_verification_required": True,
            "payment_method_required": True, "invite_required": True,
        })
        cleaned, dropped = strip_unsupported_fields(p, ARTICLE)
        assert set(dropped) == EVIDENCE_REQUIRED_FIELDS
        for name in EVIDENCE_REQUIRED_FIELDS:
            assert getattr(cleaned, name) is None
        assert cleaned.confidence == 0.4, "全被剔空的结果必须降到待核实档"

    def test_a_low_confidence_is_never_raised_by_cleaning(self):
        p = PerkFields(product="Cursor", entitlement="不存在的说法", confidence=0.1,
                       evidence=[Evidence(field="entitlement", quote="不存在的说法")])
        cleaned, _ = strip_unsupported_fields(p, ARTICLE)
        assert cleaned.confidence == 0.1

    def test_region_and_eligibility_need_evidence_like_any_other_claim(self):
        """这两个字段此前不在闸门表里：模型凭先验写的「适用地区 / 新老用户限制」
        不需要任何引文就能进库、进邮件、进导出。"""
        p = PerkFields(product="Cursor", region="中国大陆", eligibility="仅限新用户",
                       starts_at="2026-11-01",
                       evidence=[Evidence(field="product", quote="Cursor")])
        cleaned, dropped = strip_unsupported_fields(p, ARTICLE)
        assert {"region", "eligibility", "starts_at"} <= set(dropped)
        assert cleaned.region is None and cleaned.eligibility is None
        assert cleaned.starts_at is None

    def test_supported_region_and_eligibility_survive(self):
        p = PerkFields(product="Cursor", region="中国大陆", eligibility="面向所有新用户",
                       evidence=[Evidence(field="eligibility", quote="面向所有新用户")])
        cleaned, dropped = strip_unsupported_fields(p, ARTICLE)
        assert cleaned.eligibility == "面向所有新用户"
        assert "eligibility" not in dropped
        assert cleaned.region is None, "有主张没引文，仍然是置空"

    def test_a_false_boolean_without_evidence_is_not_presented_as_a_fact(self):
        """"不需要绑卡"和"需要绑卡"一样是模型在陈述事实。False 此前因为被当成
        "空值"而免检，导出时渲染成确定的「否」，把没依据的断言讲成了事实。"""
        p = PerkFields(product="Cursor", entitlement="注册即送 1 个月 Pro",
                       new_users_only=False, student_verification_required=False,
                       payment_method_required=False, invite_required=False,
                       evidence=[Evidence(field="entitlement", quote="注册即送 1 个月 Pro"),
                                 Evidence(field="product", quote="Cursor")])
        cleaned, dropped = strip_unsupported_fields(p, ARTICLE)
        assert {"new_users_only", "student_verification_required",
                "payment_method_required", "invite_required"} <= set(dropped)
        assert cleaned.new_users_only is None, "无据的 False 必须落成未说明，不是「否」"
        assert cleaned.payment_method_required is None

    def test_a_false_boolean_with_evidence_is_kept(self):
        p = PerkFields(product="Cursor", new_users_only=False,
                       evidence=[Evidence(field="new_users_only", quote="面向所有新用户"),
                                 Evidence(field="product", quote="Cursor")])
        cleaned, dropped = strip_unsupported_fields(p, ARTICLE)
        assert cleaned.new_users_only is False
        assert dropped == []

    def test_invented_url_is_detected(self):
        p = PerkFields(product="Cursor", claim_url="https://cursor.com/pricing",
                       rules_url="https://cursor.com/terms")
        assert _invented_urls(p, ARTICLE) == ["claim_url"]

    def test_urls_present_in_the_article_are_kept(self):
        p = PerkFields(product="Cursor", claim_url="https://cursor.com/promo",
                       rules_url="https://cursor.com/terms")
        assert _invented_urls(p, ARTICLE) == []


class TestAnalyserGates:
    """Extractor.analyse 在拿到模型输出后必须过两道机械闸门。"""

    def _run(self, monkeypatch, raw: str, text: str = ARTICLE,
             title: str = "Cursor 免费送会员") -> llm.AnalysisReport:
        ex = Extractor(api_key="")
        monkeypatch.setattr(ex, "_call", lambda *_a, **_k: (raw, {"prompt": 10, "completion": 5, "cached": 0}))
        return ex.analyse(article_id=1, article_text=text, title=title)

    def test_supported_extraction_passes_through(self, monkeypatch):
        report = self._run(monkeypatch, VALID)
        assert report.error is None
        assert report.is_promotion is True
        assert report.outcomes[0].perk.claim_url == "https://cursor.com/promo"
        assert report.outcomes[0].dropped_fields == []

    def test_invented_fields_are_stripped_before_reaching_the_database(self, monkeypatch):
        payload = json.loads(VALID)
        payload["perks"][0]["evidence"] = [
            e for e in payload["perks"][0]["evidence"] if e["field"] != "quota"
        ] + [{"field": "quota", "quote": "10 亿次调用"}]
        report = self._run(monkeypatch, json.dumps(payload, ensure_ascii=False))
        assert "quota" in report.outcomes[0].dropped_fields
        assert report.outcomes[0].perk.quota is None

    def test_fabricated_url_never_survives(self, monkeypatch):
        payload = json.loads(VALID)
        payload["perks"][0]["claim_url"] = "https://cursor.com/free-stuff"
        payload["perks"][0]["evidence"] = [
            e for e in payload["perks"][0]["evidence"] if e["field"] != "claim_url"
        ]
        report = self._run(monkeypatch, json.dumps(payload, ensure_ascii=False))
        outcome = report.outcomes[0]
        assert outcome.perk.claim_url is None
        assert "claim_url" in (outcome.dropped_fields + outcome.fabricated)

    def test_a_quote_taken_from_the_title_is_accepted(self, monkeypatch):
        """prompt 里带着标题（_user_prompt），所以从标题摘的引文本来就是可核验的。
        此前闸门只拿正文比对，这类字段会被误判成无据而删除。"""
        payload = json.loads(VALID)
        payload["perks"][0]["evidence"] = [
            {"field": "product", "quote": "Cursor 免费送会员"},   # 只出现在标题里
        ]
        report = self._run(monkeypatch, json.dumps(payload, ensure_ascii=False))
        assert "product" not in report.outcomes[0].dropped_fields
        assert report.outcomes[0].perk.product == "Cursor"

    def test_a_url_that_only_appears_in_the_title_survives(self, monkeypatch):
        """标题里给出的领取链接：此前既被判成编造，又因为引文在正文里找不到而被删。
        它其实是原文里逐字存在的、有引文支撑的真链接。"""
        payload = json.loads(VALID)
        payload["perks"][0]["claim_url"] = "https://cursor.com/free-stuff"
        payload["perks"][0]["evidence"] = [
            e for e in payload["perks"][0]["evidence"] if e["field"] != "claim_url"
        ] + [{"field": "claim_url", "quote": "https://cursor.com/free-stuff"}]
        report = self._run(monkeypatch, json.dumps(payload, ensure_ascii=False),
                           text=ARTICLE, title="领取入口 https://cursor.com/free-stuff")
        outcome = report.outcomes[0]
        assert outcome.perk.claim_url == "https://cursor.com/free-stuff"
        assert outcome.fabricated == []
        assert "claim_url" not in outcome.dropped_fields

    def test_a_url_in_neither_body_nor_title_is_still_called_fabricated(self, monkeypatch):
        """修标题漏字不等于放宽：正文和标题里都没有的链接照旧判编造。"""
        payload = json.loads(VALID)
        payload["perks"][0]["claim_url"] = "https://cursor.com/invented"
        payload["perks"][0]["evidence"] = [
            e for e in payload["perks"][0]["evidence"] if e["field"] != "claim_url"
        ]
        report = self._run(monkeypatch, json.dumps(payload, ensure_ascii=False),
                           text=ARTICLE, title="另一篇无关的标题")
        assert report.outcomes[0].perk.claim_url is None
        assert "claim_url" in report.outcomes[0].fabricated

    def test_unparseable_output_is_reported_not_swallowed(self, monkeypatch):
        report = self._run(monkeypatch, "抱歉，我无法完成这个任务。")
        assert report.error is not None
        assert "not parseable" in report.error
        assert report.outcomes == []

    def test_short_text_is_refused_before_anything_is_trusted(self, monkeypatch):
        ex = Extractor(api_key="")
        calls = []
        monkeypatch.setattr(ex, "_call", lambda *_a: calls.append(1) or ("{}", {}))
        report = ex.analyse(article_id=1, article_text="太短了。", title=None)
        assert report.error and not calls, "正文过短不应消耗模型调用"

    def test_model_error_is_recorded_on_the_article(self, monkeypatch):
        ex = Extractor(api_key="")

        def boom(*_a, **_k):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(ex, "_call", boom)
        report = ex.analyse(article_id=1, article_text=ARTICLE, title=None)
        assert report.error.startswith("RuntimeError")


class TestStrictSchema:
    def test_schema_is_self_contained(self):
        schema = extraction_json_schema()
        assert "$defs" not in schema and "$ref" not in json.dumps(schema)

    def test_every_object_closes_additional_properties_and_lists_all_required(self):
        schema = extraction_json_schema()
        seen = 0
        stack = [schema]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            if node.get("type") == "object":
                seen += 1
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            stack.extend(node.values())
        assert seen >= 3, "至少应覆盖结果对象、福利对象与 evidence 对象"

    def test_perk_object_exposes_every_claimed_field(self):
        props = extraction_json_schema()["properties"]["perks"]["items"]["properties"]
        assert {"product", "benefit_type", "entitlement", "quota", "ends_at",
                "claim_url", "rules_url", "confidence", "evidence"} <= set(props)

    def test_nullable_fields_are_declared_nullable(self):
        props = extraction_json_schema()["properties"]["perks"]["items"]["properties"]
        assert props["ends_at"] != {"type": "string"}, "未提及必须能表达为 null"


class TestOffPeakWindow:
    def test_peak_hours_are_refused(self):
        moment = datetime(2026, 10, 15, 10, 30, tzinfo=timeutil.BEIJING)
        assert timeutil.is_deepseek_peak(moment) is True
        assert off_peak_window_closed(moment.astimezone(timeutil.UTC)) is True

    def test_evening_after_peak_is_open(self):
        moment = datetime(2026, 10, 15, 19, 0, tzinfo=timeutil.BEIJING)
        assert off_peak_window_closed(moment.astimezone(timeutil.UTC)) is False

    def test_weekend_is_always_off_peak(self):
        saturday = datetime(2026, 10, 17, 10, 30, tzinfo=timeutil.BEIJING)
        assert timeutil.is_deepseek_peak(saturday.astimezone(timeutil.UTC)) is False

    def test_morning_gap_between_deadline_and_next_start_is_refused(self):
        moment = datetime(2026, 10, 15, 9, 30, tzinfo=timeutil.BEIJING)
        assert off_peak_window_closed(moment.astimezone(timeutil.UTC)) is True

    def test_a_night_batch_still_stops_when_it_spills_past_the_deadline(self):
        """18:05 起来的那一批跑到早上 8 点必须收手，不能撞进早高峰。"""
        start = datetime(2026, 10, 15, 18, 10, tzinfo=timeutil.BEIJING).astimezone(timeutil.UTC)
        spilled = datetime(2026, 10, 16, 8, 30, tzinfo=timeutil.BEIJING).astimezone(timeutil.UTC)
        assert off_peak_window_closed(spilled, batch_start=start) is True
        middle = datetime(2026, 10, 16, 3, 0, tzinfo=timeutil.BEIJING).astimezone(timeutil.UTC)
        assert off_peak_window_closed(middle, batch_start=start) is False

    def test_a_daytime_catchup_batch_is_not_refused_in_a_real_off_peak_hour(self):
        """隔夜欠账的补跑：12:30 是真实的半价时段，此前也被 08:00-18:00 整段拒掉，
        结果补跑一条都没分析，还把当晚的水位写成了"已完成"。"""
        moment = datetime(2026, 10, 15, 12, 30, tzinfo=timeutil.BEIJING).astimezone(timeutil.UTC)
        assert timeutil.is_deepseek_peak(moment) is False, "前提：这个时段确实是低谷"
        assert off_peak_window_closed(moment, batch_start=moment) is False

    def test_a_daytime_catchup_still_stops_when_peak_starts(self):
        start = datetime(2026, 10, 15, 12, 0, tzinfo=timeutil.BEIJING).astimezone(timeutil.UTC)
        peak = datetime(2026, 10, 15, 14, 30, tzinfo=timeutil.BEIJING).astimezone(timeutil.UTC)
        assert off_peak_window_closed(peak, batch_start=start) is True


class TestPrefilter:
    """送模型前的零成本关键词闸门，直接决定账单大小。"""

    def test_promotional_article_passes(self):
        assert prefilter.score(ARTICLE).worth_analysing is True

    def test_pure_news_without_free_signal_is_skipped(self):
        text = "某公司本周发布新版模型，参数规模翻倍，评测分数领先业界，并在大会上公布了路线图。"
        assert prefilter.score(text).worth_analysing is False

    def test_recruitment_post_is_skipped(self):
        text = "招聘：AI 算法工程师岗位，简历投递面试安排详见正文，公司提供免费班车与下午茶。"
        assert prefilter.score(text).worth_analysing is False

    def test_free_signal_without_ai_subject_is_skipped(self):
        assert prefilter.score("本文免费领取鸡蛋，限今天。").worth_analysing is False

    def test_empty_text_is_skipped(self):
        assert prefilter.score("").worth_analysing is False
        assert prefilter.score(None).worth_analysing is False

    def test_matched_terms_are_reported_for_debugging(self):
        assert prefilter.score(ARTICLE).matched


def http_error(cls, status: int):
    import httpx

    request = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
    response = httpx.Response(status, request=request, json={"error": {"message": "nope"}})
    return cls(f"Error code: {status}", response=response, body=None)


class FakeChat:
    """替掉 openai 客户端：按脚本依次返回结果或抛异常，并记录每次调用的参数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def completion(content: str, *, finish_reason: str = "stop", prompt: int = 2600,
               out: int = 400, cached: int = 2400):
    from types import SimpleNamespace

    message = SimpleNamespace(content=content, reasoning_content=None)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=out,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )
    return SimpleNamespace(choices=[choice], usage=usage, id="fake", model="deepseek-flash")


class TestModelRequestShape:
    """这三条都是 2026-10-15 第一次真实调用 DeepSeek 才暴露出来的。"""

    def analyser(self, script):
        chat = FakeChat(script)
        ex = Extractor(api_key="k")
        ex._client = SimpleNamespace(chat=SimpleNamespace(completions=chat))
        return ex, chat

    def test_reasoning_is_switched_off(self):
        ex, chat = self.analyser([completion(VALID)])
        ex.analyse(1, ARTICLE, "Cursor 免费送会员")
        assert chat.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}

    def test_strict_schema_400_falls_back_and_still_bills_correctly(self):
        """旧的兜底路径把 usage 丢了，账单会显示 0 token，等于成本不可见。"""
        ex, chat = self.analyser([
            http_error(BadRequestError, 400),
            completion(VALID),
        ])
        report = ex.analyse(1, ARTICLE, "Cursor 免费送会员")
        assert report.error is None
        assert chat.calls[1]["response_format"] == {"type": "json_object"}
        assert report.usage == {"prompt": 2600, "completion": 400, "cached": 2400}

    def test_the_400_is_paid_once_not_once_per_article(self):
        ex, chat = self.analyser([
            http_error(BadRequestError, 400),
            completion(VALID), completion(VALID), completion(VALID),
        ])
        for _ in range(3):
            assert ex.analyse(1, ARTICLE, "Cursor 免费送会员").error is None
        assert [call["response_format"]["type"] for call in chat.calls] == \
            ["json_schema"] + ["json_object"] * 3

    def test_truncation_is_named_as_truncation(self):
        ex, _chat = self.analyser([completion("", finish_reason="length")])
        report = ex.analyse(1, ARTICLE, "Cursor 免费送会员")
        assert report.error is not None
        assert "截断" in report.error
        assert "empty model response" not in report.error

    def test_a_4xx_is_never_retried(self):
        from bp.llm import _retryable

        assert _retryable(http_error(BadRequestError, 400)) is False
        assert _retryable(http_error(RateLimitError, 429)) is True
        assert _retryable(http_error(InternalServerError, 503)) is True
