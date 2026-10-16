from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import timeutil
from .config import PROJECT_ROOT
from .models import PerkFields
from .normalize import (
    content_hash,
    domain_of,
    normalize_text,
    normalize_url,
    registrable_domain,
)

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

# Matches "100 credits", "500k token", "2000次", "1个月" so differently worded
# quotas collapse onto the same benefit identity.
_QUOTA = re.compile(r"(\d[\d,\.]*\s?[kKmM万千]?)\s*([a-zA-Z\u4e00-\u9fff]+)?")
_UNITS = {
    "credits": "credit", "credit": "credit", "积分": "credit", "点数": "credit",
    "tokens": "token", "token": "token", "次": "request", "requests": "request",
    "request": "request", "queries": "request", "条": "item", "天": "day",
    "days": "day", "day": "day", "个月": "month", "months": "month", "month": "month",
    "小时": "hour", "hours": "hour", "元": "cny", "美元": "usd", "usd": "usd",
}

_alias_cache: dict[str, str] | None = None


def product_aliases() -> dict[str, str]:
    """Optional user-editable alias map so 'Cursor 编辑器' and 'cursor' collapse."""
    global _alias_cache
    if _alias_cache is not None:
        return _alias_cache
    path = PROJECT_ROOT / "config" / "aliases.toml"
    table: dict[str, str] = {}
    if path.exists():
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        for canonical, names in raw.items():
            if isinstance(names, str):
                names = [names]
            table[canonical.strip().lower()] = canonical
            for name in names or []:
                table[str(name).strip().lower()] = canonical
    _alias_cache = table
    return table


def normalize_product(product: str | None) -> str:
    if not product:
        return ""
    text = normalize_text(product)
    text = re.sub(r"[·・\-—_、,，.。/]+", " ", text)
    text = re.sub(r"\b(official|app|ai|editor|tool|平台|官方|智能体|助手)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for probe in (text, text.replace(" ", "")):
        if probe in product_aliases():
            return product_aliases()[probe].strip().lower()
    return text


def quota_signature(quota: str | None, entitlement: str | None) -> str:
    """Digits + canonical unit tokens, so '100 Credits' and '100积分' agree while
    '100 credits' and '1000 credits' stay distinct benefits."""
    source = " ".join(x for x in (quota, entitlement) if x)
    if not source:
        return ""
    parts: list[str] = []
    for amount, unit in _QUOTA.findall(normalize_text(source)):
        amount = amount.replace(",", "").replace(" ", "")
        unit_key = (unit or "").strip().lower()
        parts.append(f"{amount}{_UNITS.get(unit_key, unit_key)}")
    return "|".join(sorted(set(parts)))


def claim_identity(perk: PerkFields) -> str:
    """Host-level identity of where the benefit is claimed, preferring the claim
    page and falling back to the rules page."""
    for url in (perk.claim_url, perk.rules_url):
        reg = registrable_domain(url)
        if reg and reg not in {"qq.com", "weixin.qq.com", "t.co", "bit.ly"}:
            return reg
    return ""


def canonical_key(perk: PerkFields) -> str:
    # 额度数字是两条福利之间最硬的区别，但它经常解析不出来（"免费试用 Pro 版"没有
    # 任何数字）。只靠额度时，同产品、同类型、又落在同一官方域名上的两条不同权益会
    # 撞成同一个 key，后一条就把前一条整条顶掉。把权益文本本身也纳入身份，撞 key
    # 就不再可能：文本不同 → key 不同 → 各占一行。
    raw = "::".join([
        normalize_product(perk.product),
        perk.benefit_type or "",
        quota_signature(perk.quota, perk.entitlement),
        content_hash(None, perk.entitlement)[:16],
        claim_identity(perk),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def similarity(left: str | None, right: str | None) -> float:
    a, b = normalize_text(left or ""), normalize_text(right or "")
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class PerkWrite:
    perk_id: int
    action: str  # created | updated | unchanged
    changed_fields: list[str]


def _perk_payload(perk: PerkFields) -> dict[str, Any]:
    return {
        "product": perk.product,
        "product_norm": normalize_product(perk.product),
        "benefit_type": perk.benefit_type,
        "entitlement": perk.entitlement,
        "quota": perk.quota,
        "starts_at": perk.starts_at,
        "ends_at": perk.ends_at,
        "claim_url": perk.claim_url,
        "rules_url": perk.rules_url,
        "region": perk.region,
        "eligibility": perk.eligibility,
        "new_users_only": _tri(perk.new_users_only),
        "student_verification_required": _tri(perk.student_verification_required),
        "payment_method_required": _tri(perk.payment_method_required),
        "invite_required": _tri(perk.invite_required),
        "confidence": perk.confidence,
    }


def _tri(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


TRACKED_FIELDS = [
    "product", "benefit_type", "entitlement", "quota", "starts_at", "ends_at",
    "claim_url", "rules_url", "region", "eligibility", "new_users_only",
    "student_verification_required", "payment_method_required", "invite_required",
]


def same_benefit(left: str | None, right: str | None) -> bool:
    """Do the two entitlement texts describe the same offer?

    A merge overwrites ``entitlement`` on the row that survives, so merging two
    different benefits of one product destroys one of them. Missing text on either
    side cannot be compared and is left to the quota / domain signals; two texts
    that are both present and clearly dissimilar must not merge.
    """
    if not left or not right:
        return True
    return similarity(left, right) >= 0.72


def find_merge_target(conn: sqlite3.Connection, perk: PerkFields) -> sqlite3.Row | None:
    """Locate the existing perk, if any, that this claim describes.

    Ordering matters: an exact canonical-key hit wins, then the same product and
    benefit type landing on the same official host, then a textually near-identical
    benefit. That ladder is what merges reposts and repeat promotion articles of one
    activity across different accounts and URLs.
    """
    key = canonical_key(perk)
    exact = conn.execute("SELECT * FROM perks WHERE canonical_key = ?", (key,)).fetchone()
    if exact:
        return exact

    pname = normalize_product(perk.product)
    if not pname:
        return None
    candidates = conn.execute(
        "SELECT * FROM perks WHERE product_norm = ? AND benefit_type = ? "
        "ORDER BY updated_at DESC LIMIT 40",
        (pname, perk.benefit_type),
    ).fetchall()

    ident = claim_identity(perk)
    qsig = quota_signature(perk.quota, perk.entitlement)
    for row in candidates:
        row_ident = claim_identity(
            PerkFields(claim_url=row["claim_url"], rules_url=row["rules_url"])
        )
        row_q = quota_signature(row["quota"], row["entitlement"])
        # A different stated amount is a different benefit, whatever else matches.
        if qsig and row_q and qsig != row_q:
            continue
        # 同产品、同类型、同主机、又都解析不出额度数字时，此前只凭域名就合并，于是
        # 「免费试用 Pro 版」被后来的一条「免费领取会员资格」整条顶掉。权益文本
        # 讲的就是两件事时坚决不合 —— 合错了会丢福利，漏合了最多是重复一行。
        if not same_benefit(perk.entitlement, row["entitlement"]):
            continue
        if ident and row_ident and ident == row_ident:
            return row
        if qsig and row_q and qsig == row_q:
            return row

    for row in candidates:
        if qsig and quota_signature(row["quota"], row["entitlement"]) not in ("", qsig):
            continue
        if similarity(perk.entitlement, row["entitlement"]) >= 0.72:
            return row
    return None


def upsert_perk(
    conn: sqlite3.Connection,
    perk: PerkFields,
    article_id: int,
    evidence: list[dict[str, Any]] | None = None,
) -> PerkWrite:
    """Insert or merge a benefit claim, always preserving every contributing
    source and recording a version whenever the stated rules change."""
    now = timeutil.iso(timeutil.now_utc())
    payload = _perk_payload(perk)
    payload["canonical_key"] = canonical_key(perk)
    if not perk.benefit_type:
        stated = " ".join(x for x in (perk.entitlement, perk.quota) if x)
        payload["canonical_key"] = hashlib.sha1(
            f"{payload['product_norm']}::novel::{content_hash(None, stated)}".encode()
        ).hexdigest()[:24]

    existing = find_merge_target(conn, perk)
    if existing is None:
        columns = ", ".join(payload)
        marks = ", ".join("?" for _ in payload)
        cursor = conn.execute(
            f"INSERT INTO perks ({columns}, status, first_seen_at, updated_at, needs_review, review_reason) "
            f"VALUES ({marks}, 'pending', ?, ?, ?, ?)",
            [*payload.values(), now, now, 0, None],
        )
        perk_id = int(cursor.lastrowid)
        _snapshot(conn, perk_id, payload, "created", article_id, [])
        _link(conn, perk_id, article_id, "primary", now)
        _add_evidence(conn, perk_id, article_id, evidence or [], now)
        return PerkWrite(perk_id, "created", [])

    perk_id = int(existing["id"])
    changed = [
        field for field in TRACKED_FIELDS
        if _differs(existing[field], payload.get(field))
        # A differently spelled name for the same product is not new information.
        and not (field == "product"
                 and normalize_product(existing["product"]) == payload["product_norm"])
    ]
    _link(conn, perk_id, article_id, "repost", now)
    _add_evidence(conn, perk_id, article_id, evidence or [], now)

    if not changed:
        # Re-seeing the same rules in a repost is not new information. Moving
        # ``updated_at`` here would re-open the report window on every duplicate
        # (the nightly run sees hundreds) and put the same perk in every day's mail,
        # so only a genuinely higher confidence is recorded.
        if (perk.confidence or 0) > (existing["confidence"] or 0):
            conn.execute("UPDATE perks SET confidence = ?, updated_at = ? WHERE id = ?",
                         (perk.confidence, now, perk_id))
        return PerkWrite(perk_id, "unchanged", [])

    # Only genuinely new information overwrites a field; a null never erases a
    # value an earlier article established.
    sets, values = [], []
    for field in changed:
        sets.append(f"{field} = ?")
        values.append(payload.get(field))
    sets.append("canonical_key = ?")
    values.append(payload["canonical_key"])
    sets.append("updated_at = ?")
    values.append(now)
    conn.execute(f"UPDATE perks SET {', '.join(sets)} WHERE id = ?", [*values, perk_id])
    _snapshot(conn, perk_id, payload, "rule_update", article_id, changed)
    return PerkWrite(perk_id, "updated", changed)


def _differs(old: Any, new: Any) -> bool:
    if new in (None, "", []):
        return False
    if old in (None, "", []):
        return True
    if isinstance(old, str) and isinstance(new, str):
        return normalize_text(old) != normalize_text(new)
    if isinstance(old, int) and isinstance(new, bool):
        return (1 if new else 0) != old
    return old != new


def _link(conn: sqlite3.Connection, perk_id: int, article_id: int, role: str, now: str) -> None:
    conn.execute(
        "INSERT INTO perk_sources (perk_id, article_id, role, added_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(perk_id, article_id) DO UPDATE SET role = excluded.role",
        (perk_id, article_id, role, now),
    )


def _add_evidence(
    conn: sqlite3.Connection, perk_id: int, article_id: int,
    evidence: list[dict[str, Any]], now: str,
) -> None:
    for item in evidence:
        quote = (item.get("quote") or "").strip()
        if not quote:
            continue
        conn.execute(
            "INSERT INTO perk_evidence (perk_id, article_id, field, quote, verified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (perk_id, article_id, item.get("field"), quote, 1 if item.get("verified", True) else 0, now),
        )


def _snapshot(
    conn: sqlite3.Connection, perk_id: int, payload: dict[str, Any],
    reason: str, article_id: int | None, changed: list[str],
) -> None:
    conn.execute(
        "INSERT INTO perk_versions (perk_id, snapshot, reason, article_id, changed_fields, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (perk_id, json.dumps(payload, ensure_ascii=False, sort_keys=True), reason,
         article_id, json.dumps(changed, ensure_ascii=False),
         timeutil.iso(timeutil.now_utc())),
    )


def record_status(
    conn: sqlite3.Connection, perk_id: int, from_status: str | None,
    to_status: str, reason: str, changed_by: str,
) -> None:
    conn.execute(
        "INSERT INTO perk_status_history (perk_id, from_status, to_status, reason, changed_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (perk_id, from_status, to_status, reason, changed_by, timeutil.iso(timeutil.now_utc())),
    )


def set_status(conn: sqlite3.Connection, perk_id: int, to_status: str, reason: str, changed_by: str) -> bool:
    """Guarded status transition. The database trigger additionally refuses any
    jump to 'confirmed' that is not backed by a fresh official-domain check."""
    row = conn.execute("SELECT status, verification_id FROM perks WHERE id = ?", (perk_id,)).fetchone()
    if row is None or row["status"] == to_status:
        return False
    try:
        conn.execute("UPDATE perks SET status = ?, updated_at = ? WHERE id = ?",
                     (to_status, timeutil.iso(timeutil.now_utc()), perk_id))
    except sqlite3.IntegrityError as exc:
        if "CONFIRM_GUARD" in str(exc):
            conn.execute("UPDATE perks SET needs_review = 1, review_reason = ? WHERE id = ?",
                         (f"拒绝直接确认：{reason}", perk_id))
            return False
        raise
    record_status(conn, perk_id, row["status"], to_status, reason, changed_by)
    return True


def note_article(conn: sqlite3.Connection, url: str | None, title: str | None) -> str:
    """url_norm participates in a UNIQUE index, so it must never be NULL for a
    real article; fall back to the raw URL and then to a title hash."""
    return normalize_url(url) or (url or "").strip() or f"anon:{content_hash(title, None)}"


@dataclass
class ArticleWrite:
    article_id: int | None
    action: str  # new | revision | duplicate | alias
    reason: str = ""


def record_article(
    conn: sqlite3.Connection,
    *,
    url: str | None,
    title: str | None,
    content: str | None,
    summary: str | None = None,
    published_at: str | None = None,
    source_id: int | None = None,
    author: str | None = None,
    extract_error: str | None = None,
) -> ArticleWrite:
    """Store a fetched item while applying the article-level dedupe rules:

    * same location, same content   -> duplicate, nothing stored
    * same content, other location   -> alias recorded against the stored article
    * same location, changed content -> new revision row (history preserved)
    """
    now = timeutil.iso(timeutil.now_utc())
    url_norm = note_article(conn, url, title)
    body = content or summary or ""
    # 正文为空时所有文章的 content_hash 都是空串的哈希，于是两篇毫不相干的文章
    # 会被判成 alias 并互相挂上对方的 URL —— 追溯的时候一个来源会莫名其妙出现在
    # 另一条福利下。没有正文可比对时退化为按 URL 区分，各自占一行。
    digest = (content_hash(title, body) if body.strip()
              else content_hash(None, f"empty:{url_norm}"))

    same_loc = conn.execute(
        "SELECT id, content_hash FROM articles WHERE url_norm = ? AND content_hash = ?",
        (url_norm, digest),
    ).fetchone()
    if same_loc:
        conn.execute("UPDATE articles SET fetched_at = ? WHERE id = ?", (now, same_loc["id"]))
        return ArticleWrite(int(same_loc["id"]), "duplicate", "identical location and content")

    other_url = conn.execute(
        "SELECT id, url_norm FROM articles WHERE content_hash = ? AND url_norm IS NOT ? ORDER BY id LIMIT 1",
        (digest, url_norm),
    ).fetchone()
    if other_url:
        conn.execute(
            "INSERT INTO article_aliases (url_norm, article_id, first_seen_at) VALUES (?, ?, ?) "
            "ON CONFLICT(url_norm, article_id) DO NOTHING",
            (url_norm, other_url["id"], now),
        )
        conn.execute(
            "INSERT INTO article_aliases (url_norm, article_id, first_seen_at) VALUES (?, ?, ?) "
            "ON CONFLICT(url_norm, article_id) DO NOTHING",
            (other_url["url_norm"], other_url["id"], now),
        )
        return ArticleWrite(int(other_url["id"]), "alias", "same content reached from another URL")

    previous = conn.execute(
        "SELECT id FROM articles WHERE url_norm = ? ORDER BY id DESC LIMIT 1", (url_norm,)
    ).fetchone()

    cursor = conn.execute(
        "INSERT INTO articles (source_id, url, url_norm, title, author, published_at, fetched_at, "
        "content, summary, content_hash, prev_article_id, domain, extract_error, analyze_state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
        (source_id, url, url_norm, title, author, published_at, now, content, summary,
         digest, previous["id"] if previous else None, domain_of(url), extract_error),
    )
    article_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO article_aliases (url_norm, article_id, first_seen_at) VALUES (?, ?, ?) "
        "ON CONFLICT(url_norm, article_id) DO NOTHING",
        (url_norm, article_id, now),
    )
    if previous:
        return ArticleWrite(article_id, "revision", "same URL, changed content")
    return ArticleWrite(article_id, "new")
