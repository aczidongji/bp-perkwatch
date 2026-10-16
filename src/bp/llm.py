from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, BadRequestError, RateLimitError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from . import timeutil
from .config import get_settings
from .models import (
    ExtractionResult,
    PerkFields,
    evidence_holds,
    extraction_json_schema,
    parse_extraction,
    strip_unsupported_fields,
)
from .normalize import normalize_text

MAX_ARTICLE_CHARS = 24_000

# deepseek-flash thinks before it answers, and the thinking comes out of max_tokens.
# Measured on a real article: 1600/1600 tokens spent on reasoning, finish_reason=length
# and content="" — indistinguishable from a dead API. Switching reasoning off produces the
# same JSON for 370 completion tokens instead of 5727, which is also what makes the
# nightly batch affordable.
EXTRA_BODY = {"thinking": {"type": "disabled"}}


class TruncatedResponse(RuntimeError):
    """The model ran out of output tokens. Distinct from a parse failure: the answer
    was never written, so raising the ceiling or shortening the article is the fix."""


def _text_of(completion: Any) -> str:
    choice = completion.choices[0]
    content = choice.message.content or ""
    if content.strip():
        return content
    if choice.finish_reason == "length":
        usage = getattr(completion, "usage", None)
        raise TruncatedResponse(
            "模型输出被 max_tokens 截断，正文为空"
            f"（completion_tokens={getattr(usage, 'completion_tokens', '?')}）"
        )
    return ""


def _usage_of(completion: Any) -> dict[str, int]:
    usage = getattr(completion, "usage", None)
    if not usage:
        return {}
    details = getattr(usage, "prompt_tokens_details", None)
    return {
        "prompt": usage.prompt_tokens or 0,
        "completion": usage.completion_tokens or 0,
        "cached": getattr(details, "cached_tokens", 0) or 0,
    }


def _retryable(exc: BaseException) -> bool:
    """Retry transport failures, 429s and 5xx. Never retry a 4xx: an unsupported
    ``response_format`` is answered identically every time, and three attempts of it
    only add latency before the fallback runs."""
    if isinstance(exc, (RateLimitError, APITimeoutError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        return bool(exc.status_code) and exc.status_code >= 500
    return False

# Kept as one static block and sent as the system message so DeepSeek's prompt
# cache can hit it: cached input costs a small fraction of uncached input, and the
# article body is the only part that changes between calls.
SYSTEM_PROMPT = """你是一个严格的信息抽取器。你的任务是从一篇中文或英文文章中，抽取"AI 产品福利"相关条款。

福利范围：免费会员、免费额度/用量、兑换码/邀请码、学生优惠、限时试用、赠送算力、明确价格的促销活动。

必须遵守的规则：
1. 只允许使用文章中出现的信息。文章没有写的字段一律输出 null，绝对不要推测、补全或从你的先验知识里填写。
2. ends_at / starts_at 只有在文章明确写出日期时才填。日期统一输出 YYYY-MM-DD。文章只写"月底""下周"这类相对表述时，输出 null。
3. claim_url 和 rules_url 必须是文章里逐字出现的链接。文章没给链接就输出 null。不要自己拼官网地址。
4. 每个非 null 的关键字段都必须在 evidence 里给出一段逐字摘自原文的引文（field 填该字段名）。benefit_type 和四个布尔字段的引文，请摘录支持这一判断的那段原文，而不是字段值本身。引文必须能在原文中一字不差地找到，不得改写、翻译、合并或补全。没有合格引文时，请把该字段置为 null 而不是编造引文。product 和 entitlement 是整条福利的立足点，必须各自带一条引文，否则这条会被整条丢弃。
5. benefit_type 只能取：free_membership / free_credits / redemption_code / student_discount / free_trial / discount / giveaway / other。
6. new_users_only、student_verification_required、payment_method_required、invite_required 为布尔值；文章未提及则 null。
7. is_promotion 为 false 时 perks 必须为空数组。纯新闻、评测、教程、观点、招聘类文章不是促销。
8. 同一篇文章里的不同福利活动，输出为 perks 数组里的多个元素，不要合并。
9. confidence 是 0 到 1 的小数，代表你对整条抽取结果的确信程度。

输出对象的键名必须逐字用下面这些，不要新增、不要换名、不要翻译键名：
顶层对象：is_promotion / perks / notes。
perks 数组的每个元素：product（产品名）/ benefit_type / entitlement（权益内容，具体到什么免费、多少额度、多长时间）/ quota（额度数量）/ starts_at / ends_at / claim_url（领取入口）/ rules_url（官方规则）/ region / eligibility / new_users_only / student_verification_required / payment_method_required / invite_required / confidence / evidence。
evidence 数组的每个元素：field（被支持的字段名）/ quote（原文逐字引文）。
不要把权益写进 description、不要把产品名写进 provider 或 title——这两个字段就叫 entitlement 和 product。

你只输出符合上述结构的 JSON 对象，不要输出任何解释文字。"""


@dataclass
class PerkOutcome:
    perk: PerkFields
    dropped_fields: list[str] = field(default_factory=list)
    fabricated: list[str] = field(default_factory=list)


@dataclass
class AnalysisReport:
    article_id: int
    is_promotion: bool
    outcomes: list[PerkOutcome] = field(default_factory=list)
    raw_notes: str | None = None
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None


class Extractor:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.settings = settings
        self.model = model or settings.deepseek_model
        if api_key is not None:
            self.api_key = api_key
        else:
            self.api_key = settings.deepseek_api_key
        self._client = None
        self._strict_supported = True

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.settings.deepseek_base_url,
                timeout=self.settings.http_timeout_seconds * 3,
            )
        return self._client

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def _call(self, article_text: str, title: str | None) -> tuple[str, dict[str, int]]:
        if not self._strict_supported:
            return self._call_lenient(article_text, title)
        schema = extraction_json_schema()
        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                temperature=0,
                max_tokens=self.settings.deepseek_max_output_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "perk_extraction", "schema": schema, "strict": True},
                },
                extra_body=EXTRA_BODY,
                messages=self._messages(article_text, title),
            )
        except BadRequestError:
            # Remember the refusal: the 400 is deterministic, so paying one rejected
            # request per batch beats paying one per article.
            self._strict_supported = False
            raise
        return _text_of(completion), _usage_of(completion)

    @staticmethod
    def _user_prompt(article_text: str, title: str | None) -> str:
        trimmed = article_text.strip()
        if len(trimmed) > MAX_ARTICLE_CHARS:
            trimmed = trimmed[:MAX_ARTICLE_CHARS] + "\n…[文章在此截断]"
        header = f"标题：{title}\n\n" if title else ""
        return (
            "请从下面这篇完整文章中抽取福利信息。"
            "所有字段的 evidence 引文必须逐字摘自该文本。\n\n"
            f"--- 文章开始 ---\n{header}{trimmed}\n--- 文章结束 ---\n"
        )

    def _messages(self, article_text: str, title: str | None, *, ask_json: bool = False) -> list[dict]:
        prompt = self._user_prompt(article_text, title)
        if ask_json:
            # ``json_object`` mode has no schema to follow, so spell out the shape.
            prompt += "\n\n输出必须是一个 JSON 对象，键为 is_promotion / perks / notes。"
        return [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}]

    def analyse(self, article_id: int, article_text: str, title: str | None) -> AnalysisReport:
        """One article in, verified perk claims out.

        Two mechanical anti-fabrication gates run after the model answers:
        :func:`strip_unsupported_fields` removes any claim whose quoted evidence
        cannot be located in the source text, and URLs are checked to appear
        verbatim, because a confidently invented ``https://vendor.com/pricing`` is
        exactly the failure this system must not propagate.
        """
        report = AnalysisReport(article_id=article_id, is_promotion=False, model=self.model)
        if not article_text or len(article_text.strip()) < 120:
            report.error = "article text too short to analyse"
            return report

        try:
            raw, usage = self._call(article_text, title)
        except BadRequestError:
            # strict schema unsupported -> degrade to plain JSON once, then validate.
            raw, usage = self._call_lenient(article_text, title)
        except Exception as exc:  # network, auth, rate limit after retries
            report.error = f"{type(exc).__name__}: {str(exc)[:250]}"
            return report

        report.usage = usage
        try:
            result: ExtractionResult = parse_extraction(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            report.error = f"model output not parseable: {str(exc)[:200]}"
            return report

        report.is_promotion = result.is_promotion
        report.raw_notes = result.notes

        # 两道闸门要比对的必须是"模型实际看到的那份文本"。prompt 里还拼了标题
        # （_user_prompt），只拿正文比对的话，从标题里摘的引文会被判成无据而误删
        # 字段，标题里出现的链接会被判成编造。
        source_text = f"{title}\n\n{article_text}" if title else article_text
        for perk in result.perks if result.is_promotion else []:
            cleaned, dropped = strip_unsupported_fields(perk, source_text)
            fabricated = _invented_urls(perk, source_text)
            if fabricated:
                cleaned = cleaned.model_copy(update={
                    **{name: None for name in fabricated},
                    "confidence": min(cleaned.confidence or 0.4, 0.35),
                })
            report.outcomes.append(PerkOutcome(
                perk=cleaned,
                dropped_fields=dropped,
                fabricated=fabricated,
            ))
        return report

    def _call_lenient(self, article_text: str, title: str | None) -> tuple[str, dict[str, int]]:
        """Some OpenAI-compatible endpoints reject ``strict`` schemas — DeepSeek answers
        400 "This response_format type is unavailable now" — so fall back to
        ``json_object`` and rely on Pydantic validation, which is the stronger gate
        anyway."""
        completion = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            max_tokens=self.settings.deepseek_max_output_tokens,
            response_format={"type": "json_object"},
            extra_body=EXTRA_BODY,
            messages=self._messages(article_text, title, ask_json=True),
        )
        return _text_of(completion), _usage_of(completion)


def off_peak_window_closed(now=None, *, batch_start=None) -> bool:
    """True when the batch should stop spending on model calls.

    Two different situations used to share one rule, and the shared rule was wrong:

    * The nightly batch starts at ``pipeline_start_hour_bj`` and must stop before
      ``llm_batch_deadline_hour_bj``, so it does not spill into the morning peak.
    * A catch-up batch that starts during the day (the machine was off last night)
      never crossed a night. Refusing it in the whole 08:00-18:00 block also refused
      12:00-14:00, which is genuine off-peak, so the catch-up analysed nothing at all.

    ``batch_start`` says which one this is: a batch that began inside the night
    window gets the deadline, a daytime batch is bounded by the peak check alone.
    """
    settings = get_settings()
    moment = now or timeutil.now_utc()
    if timeutil.is_deepseek_peak(moment):
        return True
    started = timeutil.bj_dt(batch_start or moment)
    night_batch = (started.hour >= settings.pipeline_start_hour_bj
                   or started.hour < settings.llm_batch_deadline_hour_bj)
    if not night_batch:
        return False
    bj = timeutil.bj_dt(moment)
    return settings.llm_batch_deadline_hour_bj <= bj.hour < settings.pipeline_start_hour_bj


def _invented_urls(perk: PerkFields, source_text: str) -> list[str]:
    """URLs the model produced that do not literally occur in the article."""
    hay = normalize_text(source_text)
    invented = []
    for name in ("claim_url", "rules_url"):
        value = getattr(perk, name)
        if value and normalize_text(value) not in hay:
            invented.append(name)
    return invented
