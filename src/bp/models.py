from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import timeutil
from .normalize import normalize_text

BenefitType = Literal[
    "free_membership",   # 免费会员 / 会员时长
    "free_credits",      # 免费额度 / 用量 / token
    "redemption_code",   # 兑换码 / 邀请码 / 卡密
    "student_discount",  # 学生优惠
    "free_trial",        # 限时试用
    "discount",          # 一般促销折扣
    "giveaway",          # 抽奖 / 赠送活动
    "other",
]


class Evidence(BaseModel):
    """A verbatim quote the model claims supports one of its asserted fields.

    ``field`` names which assertion the quote backs, so the mechanical checker in
    :mod:`bp.extraction` can invalidate a specific claim without discarding the
    whole extraction.
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(description="Top-level field this quote supports, e.g. 'ends_at'.")
    quote: str = Field(min_length=2, description="Verbatim substring copied from the article.")


class PerkFields(BaseModel):
    """One claimed benefit. Every field is required but nullable: a value the
    article does not state must be reported as null, never guessed."""

    model_config = ConfigDict(extra="forbid")

    product: str | None = Field(default=None, description="产品/服务名称，原文中出现的写法。")
    benefit_type: BenefitType | None = None
    entitlement: str | None = Field(default=None, description="权益内容描述，例如 1 个月 Pro。")
    quota: str | None = Field(default=None, description="额度/数量，例如 200 次、500k token。")
    starts_at: str | None = Field(default=None, description="活动开始日期 YYYY-MM-DD，未提及则 null。")
    ends_at: str | None = Field(default=None, description="活动截止日期 YYYY-MM-DD，未提及则 null。")
    claim_url: str | None = Field(default=None, description="领取入口链接，原文没有则 null。")
    rules_url: str | None = Field(default=None, description="官方规则/条款链接，原文没有则 null。")
    region: str | None = Field(default=None, description="适用地区，例如 中国大陆 / 全球。")
    eligibility: str | None = Field(default=None, description="新老用户限制原文表述。")
    new_users_only: bool | None = None
    student_verification_required: bool | None = None
    payment_method_required: bool | None = None
    invite_required: bool | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)

    @field_validator("starts_at", "ends_at", mode="before")
    @classmethod
    def _empty_string_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if not value or value.lower() in {"null", "none", "n/a", "无", "未知"}:
                return None
        return value

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _coerce_date(cls, value: str | None) -> str | None:
        """Normalise whatever date spelling the model produced into YYYY-MM-DD so
        downstream expiry arithmetic is reliable. Unparseable -> None (fail open,
        never invent a date)."""
        if value is None:
            return None
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y.%m.%d"):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                continue
        return None

    @field_validator("claim_url", "rules_url")
    @classmethod
    def _blank_url_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: Any) -> Any:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        # Models sometimes answer 0-100 rather than 0-1.
        return number / 100.0 if number > 1.0 else number


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_promotion: bool = Field(
        description="这篇文章是否包含任何免费/优惠/试用/兑换码类信息。"
    )
    perks: list[PerkFields] = Field(default_factory=list)
    notes: str | None = Field(default=None, description="无法结构化的重要补充说明。")

    @field_validator("perks")
    @classmethod
    def _cap_perks(cls, value: list[PerkFields]) -> list[PerkFields]:
        return value[:12]


# 每一个会被展示给用户、且模型可能凭空填出来的字段都必须在这里。漏掉一个就等于给
# 它开了一道免检门：region / eligibility / starts_at 之前不在此列，于是模型写的
# 「适用地区：中国大陆」「新老用户限制：仅限新用户」不需要任何引文就能进库、进邮件。
EVIDENCE_REQUIRED_FIELDS = {
    "product", "benefit_type", "entitlement", "quota", "starts_at", "ends_at",
    "claim_url", "rules_url", "region", "eligibility", "new_users_only",
    "student_verification_required", "payment_method_required", "invite_required",
}


def extraction_json_schema() -> dict[str, Any]:
    """Self-contained JSON Schema for :class:`ExtractionResult`, shaped for
    OpenAI-compatible ``response_format=json_schema`` strict mode: every property
    listed as required, ``additionalProperties`` closed, ``$ref`` inlined and
    ``title`` noise removed."""
    schema = ExtractionResult.model_json_schema()
    defs = schema.pop("$defs", {})
    schema = _inline_refs(schema, defs)
    _prepare_strict(schema)
    return schema


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node and len(node) == 1:
            name = node["$ref"].rsplit("/", 1)[-1]
            return _inline_refs(copy.deepcopy(defs.get(name, {})), defs)
        return {k: _inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def _prepare_strict(node: Any) -> None:
    if not isinstance(node, dict):
        return
    node.pop("title", None)
    if node.get("type") == "object":
        properties = node.get("properties", {})
        node["required"] = list(properties.keys())
        node["additionalProperties"] = False
    for value in node.values():
        _prepare_strict(value)


def evidence_holds(quote: str, source_text: str) -> bool:
    """Mechanical anti-fabrication check: the quote must really occur in the text."""
    needle = normalize_text(quote)
    if len(needle) < 2:
        return False
    haystack = normalize_text(source_text)
    if needle in haystack:
        return True
    # Tolerate the model trimming an ellipsis out of a longer passage.
    parts = [p.strip() for p in needle.split("...") if len(p.strip()) >= 2]
    return bool(parts) and all(p in haystack for p in parts)


def strip_unsupported_fields(perk: PerkFields, source_text: str) -> tuple[PerkFields, list[str]]:
    """Drop every asserted value whose supporting quote cannot be found in the
    article. This is what enforces "缺少关键证据时不得编造字段" as behaviour rather
    than as an instruction to the model.

    Returns the sanitised perk and the list of field names that were removed.
    """
    verified: set[str] = set()
    rejected_quotes: list[str] = []
    for item in perk.evidence:
        if evidence_holds(item.quote, source_text):
            verified.add(item.field.strip())
        else:
            rejected_quotes.append(item.quote[:120])

    dropped: list[str] = []
    data = perk.model_dump()
    for field_name in EVIDENCE_REQUIRED_FIELDS:
        value = data.get(field_name)
        # 只放过"没有主张"。False 也是一条主张——"不需要绑卡""不需要学生认证"和
        # "需要"一样是模型在陈述事实；用 `value in (None, False, ...)` 判断会让 False
        # 免检过关，再被模板和导出渲染成确定的"否"，等于把模型的猜测当事实讲给用户。
        if value is None or value == "" or value == []:
            continue
        if field_name not in verified:
            data[field_name] = None
            dropped.append(field_name)

    data["evidence"] = [e for e in data["evidence"] if e["field"].strip() in verified]
    if dropped:
        current = data.get("confidence")
        # Unsupported claims must cap, and never raise, the reported confidence.
        data["confidence"] = 0.4 if current is None else min(float(current), 0.4)

    cleaned = PerkFields.model_validate(data)
    return cleaned, dropped


KEY_ALIASES = {
    "description": "entitlement",
    "details": "entitlement",
    "benefit_description": "entitlement",
    "offer": "entitlement",
    "provider": "product",
    "vendor": "product",
    "company": "product",
    "service": "product",
    "brand": "product",
}


def _rename_aliases(node: dict[str, Any], model: type[BaseModel]) -> None:
    """Move a synonym onto its real field before anything is discarded.

    Without server-side schema enforcement the model answers ``description`` for the
    entitlement and ``provider`` for the product. Those are naming mismatches, not
    invented facts, so mapping them back keeps a real extraction alive; the value still
    has to pass the verbatim-quote gate for the field it lands in.
    """
    known = set(model.model_fields)
    for key, target in list(KEY_ALIASES.items()):
        if key not in node or target not in known:
            continue
        value = node.pop(key)
        if node.get(target) in (None, "", []):
            node[target] = value


def _keep_only(node: dict[str, Any], model: type[BaseModel]) -> list[str]:
    _rename_aliases(node, model)
    known = set(model.model_fields)
    stray = [key for key in node if key not in known]
    for key in stray:
        node.pop(key)
    return stray


def drop_stray_keys(payload: dict[str, Any]) -> list[str]:
    """Rename the model's synonyms, then remove keys we have no field for.

    ``json_object`` mode ships no schema, so DeepSeek routinely adds ``title`` /
    ``name`` / ``price`` next to the fields we asked for. Refusing the whole article
    over one decorative key would throw away the extraction that did land — measured
    on the Copilot plans page, where five real perks produced zero rows because the
    entitlement arrived under ``description``. Everything that remains is validated
    and evidence-checked exactly as before.
    """
    stray = [f"<root>.{key}" for key in _keep_only(payload, ExtractionResult)]
    for perk in payload.get("perks") or []:
        if not isinstance(perk, dict):
            continue
        stray += [f"perks[].{key}" for key in _keep_only(perk, PerkFields)]
        for item in perk.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            field = item.get("field")
            if isinstance(field, str) and field.strip() in KEY_ALIASES:
                item["field"] = KEY_ALIASES[field.strip()]
            stray += [f"perks[].evidence[].{key}" for key in _keep_only(item, Evidence)]
    return stray


def parse_extraction(raw: str | None) -> ExtractionResult:
    """Parse model output, tolerating code fences and stray prose. Raises on
    unusable JSON so the caller can mark the article ``failed`` rather than
    silently losing data."""
    if not raw or not raw.strip():
        raise ValueError("empty model response")
    text = raw.strip()
    if text.startswith("```"):
        text = text[3:]
        if text[:4].lower() == "json":
            text = text[4:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model response")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("model returned a JSON array, not an object")
    drop_stray_keys(payload)
    if "is_promotion" not in payload:
        # An answer with perks in it plainly is a promotion; an answer without any is
        # an empty one. This reads the model's own output shape, not the article, so it
        # cannot invent a fact the source did not state.
        payload["is_promotion"] = bool(payload.get("perks"))
    return ExtractionResult.model_validate(payload)
