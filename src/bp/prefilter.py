from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT
from .models import PerkFields
from .normalize import normalize_text

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

# 三道关键词闸门，缺一不可：
#   1. trigger   —— "免费/折扣/额度"这类价格信号。只说明文章谈到了钱。
#   2. subject   —— "AI/模型/Copilot"这类主体信号。只说明谈的是这个领域。
#   3. campaign  —— "领取/兑换/限时/学生/折扣"这类**获取与边界**信号。
# 第 3 道是后来补的：只有 1+2 时，DeepMind 的模型发布稿、Cloudflare 的产品博客
# 全都命中（它们当然会写 free 和 AI），于是模型被要求从产品发布稿里"找福利"，
# 找出来的就是"Free plan includes 0.5 GB"这种套餐说明。
CAMPAIGN_TERMS: tuple[str, ...] = (
    "领取", "领用", "兑换", "兑换码", "邀请码", "优惠码", "赠送", "免费送", "免费领",
    "免费获得", "免费试用", "免费使用", "免费开通", "免费申请", "注册送", "认证送",
    "新用户", "学生", "教师", "限时", "限量", "截止", "折扣", "优惠", "促销",
    "白嫖", "补贴", "福利", "试用", "申请",
    "% off", "%off", "折", "coupon", "redeem", "promo", "giveaway", "discount",
    "sign up", "signup", "student", "free of charge",
)

# 常驻套餐的说明用语。"Free plan includes X" / "每月 X" / "limited to X" 描述的是
# 产品的固有属性：任何人在任何时候注册都有，写在定价页上，不是活动。
PLAN_TERMS: tuple[str, ...] = (
    "free plan", "free tier", "免费版", "免费套餐", "free users", "includes",
    "included", "每月", "per month", "limited to", "上限", "限制为", "订阅",
    "subscription", "定价", "pricing", "套餐", "per day", "每天", "每年", "per year",
)

# A free-of-charge signal must be present for an article to be worth a model call.
# Splitting "price word" from "AI/product word" keeps a gaming leak or a hardware
# review from being paid for twice.
TRIGGER_TERMS: tuple[str, ...] = (
    "免费", "限免", "0元", "白嫖", "试用", "体验", "额度", "赠送", "送的", "福利",
    "优惠", "折扣", "学生", "教育", "认证送", "兑换码", "邀请码", "兑换", "领取",
    "注册送", "新用户", "限时", "活动", "补贴", "credits", "free", "trial", "discount",
    "student", "promo", "coupon", "redeem", "giveaway", "$0", "no cost",
)

SUBJECT_TERMS: tuple[str, ...] = (
    "ai", "大模型", "模型", "智能", "助手", "copilot", "chatgpt", "gpt", "claude",
    "gemini", "llm", "aigc", "agent", "算法", "算力", "token", "会员", "pro",
    "订阅", "subscription", "cursor", "windsurf", "notion", "github", "绘图",
    "生图", "写作", "翻译", "编程", "编辑器", "云服务", "api",
)

# Strong negative signals: an article dominated by these is not a perk announcement.
STRONG_NEGATIVE: tuple[str, ...] = (
    "招聘", "岗位", "简历", "面试", "讣告", "拍卖", "中标", "股市", "基金净值",
    "js_error", "404",
)

_KeywordSets = tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...],
                     tuple[str, ...], tuple[str, ...]]

_CACHE: dict[Path, _KeywordSets] = {}


def _load_overrides(path: Path) -> _KeywordSets:
    if path in _CACHE:
        return _CACHE[path]
    triggers, subjects, negatives = TRIGGER_TERMS, SUBJECT_TERMS, STRONG_NEGATIVE
    campaign, plan = CAMPAIGN_TERMS, PLAN_TERMS
    if path.exists():
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        triggers = tuple(str(x).lower() for x in raw.get("trigger_terms", triggers))
        subjects = tuple(str(x).lower() for x in raw.get("subject_terms", subjects))
        negatives = tuple(str(x).lower() for x in raw.get("strong_negative", negatives))
        campaign = tuple(str(x).lower() for x in raw.get("campaign_terms", campaign))
        plan = tuple(str(x).lower() for x in raw.get("plan_terms", plan))
    _CACHE[path] = (triggers, subjects, negatives, campaign, plan)
    return _CACHE[path]


@dataclass
class PrefilterVerdict:
    worth_analysing: bool
    matched: list[str]
    score: float


def score(text: str | None, *, overrides: Path | None = None) -> PrefilterVerdict:
    """Zero-cost keyword gate run before any model call.

    Skipping irrelevant articles here is the single biggest lever on the DeepSeek
    bill, so the gate is intentionally cheap. It asks three questions, and all three
    must be answered yes:

    * does the article talk about something free or discounted?
    * is it about AI products?
    * does it say **how to get it, or when it ends**?

    The third one is what keeps product announcements out. A DeepMind model launch or
    a Cloudflare changelog certainly contains "free" and "AI" — without the campaign
    question those went to the model, which then dutifully reported the contents of
    the vendor's free tier as a benefit.
    """
    triggers, subjects, negatives, campaign, _plan = _load_overrides(
        overrides or (PROJECT_ROOT / "config" / "keywords.toml")
    )
    hay = normalize_text(text)
    if not hay:
        return PrefilterVerdict(False, ["empty"], 0.0)

    hits = [t for t in triggers if t in hay]
    subj = [s for s in subjects if s in hay]
    neg = [n for n in negatives if n in hay]
    camp = [c for c in campaign if c in hay]

    if not hits:
        return PrefilterVerdict(False, ["no trigger term"], 0.0)
    if not subj:
        return PrefilterVerdict(False, hits, 0.2)
    if not camp:
        return PrefilterVerdict(False, hits + ["no campaign term"], 0.25)
    if neg and len(hits) < 3:
        return PrefilterVerdict(False, ["negative: " + ",".join(neg[:3])], 0.1)

    value = min(1.0, 0.25 + 0.15 * len(hits) + 0.1 * len(subj) + 0.1 * len(camp))
    return PrefilterVerdict(value >= 0.4, hits[:8] + camp[:4], round(value, 3))


def looks_like_promotion(perk: "PerkFields") -> bool:
    """Is this extracted thing a campaign, or just the contents of a pricing plan?

    The model self-reports ``is_promotion`` and is reliably over-eager: measured on
    the real database it turned "GitHub Copilot Free users are limited to 2000
    completions" and "Free plan includes 0.5 GB of ingestion per day" into benefits.
    Those are standing properties of a product — permanent, unconditional, printed on
    the pricing page — and a reader cannot act on them, so they are not perks.

    A real perk carries a boundary of its own: the words say how to get it
    (领取 / 兑换 / 学生认证 / 折扣), or it has a start/end date, or it has a claim
    entry. A quantity on its own is not enough.
    """
    triggers, _subjects, _negatives, campaign, plan = _load_overrides(
        PROJECT_ROOT / "config" / "keywords.toml"
    )
    text = normalize_text(" ".join(
        x for x in (perk.entitlement, perk.quota, perk.eligibility) if x
    ))
    has_campaign = any(term in text for term in campaign)
    has_window = bool(perk.starts_at or perk.ends_at)
    if has_campaign or has_window:
        return True
    # 只剩一个领取入口：除非它描述的确实是常驻套餐，否则仍然算活动。
    if perk.claim_url or perk.rules_url:
        return not any(term in text for term in plan)
    return False


def has_date_like(text: str | None) -> bool:
    return bool(text and re.search(r"20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}", text))
