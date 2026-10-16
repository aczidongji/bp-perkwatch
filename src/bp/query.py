from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import date
from typing import Any, Iterable

from . import lifecycle, timeutil
from .report import BENEFIT_LABELS, STATUS_LABELS
from .normalize import normalize_text

EXPORT_COLUMNS = [
    "id", "product", "benefit_type", "entitlement", "quota", "starts_at", "ends_at",
    "days_left", "status", "confidence", "region", "eligibility",
    "student_verification_required", "payment_method_required", "invite_required",
    "needs_review", "review_reason", "user_claimed", "claim_url", "rules_url",
    "first_seen_at", "last_verified_at", "source_count", "source_urls",
]

SORTS = {
    "ends": "ends_at IS NULL, ends_at ASC",
    "ends_desc": "ends_at DESC",
    "recent": "updated_at DESC",
    "confidence": "confidence IS NULL, confidence ASC",
    "product": "product ASC",
    "created": "first_seen_at DESC",
}


def hydrate(conn: sqlite3.Connection, rows: Iterable[sqlite3.Row],
            *, today: date | None = None) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        item = dict(row)
        item["days_left"] = lifecycle.days_remaining(item.get("ends_at"), on_day=today)
        item["status_label"] = STATUS_LABELS.get(item.get("status"), item.get("status"))
        item["benefit_label"] = BENEFIT_LABELS.get(item.get("benefit_type"))
        out.append(item)
    return out


def search_perks(
    conn: sqlite3.Connection,
    *,
    text: str | None = None,
    product: str | None = None,
    benefit_type: str | None = None,
    statuses: list[str] | None = None,
    only_unclaimed: bool = False,
    only_expiring: bool = False,
    sort: str = "ends",
    limit: int = 200,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Filter in SQL where possible, then filter derived values (days remaining) in
    Python, since those depend on today's date rather than stored columns."""
    where, params = [], []
    if statuses:
        where.append(f"status IN ({', '.join('?' for _ in statuses)})")
        params.extend(statuses)
    if benefit_type:
        where.append("benefit_type = ?")
        params.append(benefit_type)
    if product:
        where.append("product_norm LIKE ?")
        params.append(f"%{normalize_text(product)}%")
    if text:
        where.append("(product LIKE ? OR entitlement LIKE ? OR quota LIKE ? OR claim_url LIKE ?)")
        needle = f"%{text.strip()}%"
        params.extend([needle, needle, needle, needle])
    if only_unclaimed:
        where.append("user_claimed = 0")

    sql = "SELECT * FROM perks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {SORTS.get(sort, SORTS['recent'])} LIMIT ?"
    params.append(int(limit))

    rows = conn.execute(sql, params).fetchall()
    items = hydrate(conn, rows, today=today)

    if only_expiring:
        items = [i for i in items
                 if i["status"] in ("pending", "confirmed")
                 and i["days_left"] is not None
                 and 0 <= i["days_left"] <= get_settings_expiring()]
    return items


def get_settings_expiring() -> int:
    from .config import get_settings
    return get_settings().expiring_soon_days


def sources_for(conn: sqlite3.Connection, perk_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT a.id, a.url, a.title, a.published_at, a.fetched_at, ps.role, s.name AS source_name "
        "FROM perk_sources ps JOIN articles a ON a.id = ps.article_id "
        "LEFT JOIN sources s ON s.id = a.source_id "
        "WHERE ps.perk_id = ? ORDER BY ps.added_at DESC",
        (perk_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def all_urls_for(conn: sqlite3.Connection, perk_id: int) -> list[str]:
    """Every location this benefit has been observed at, including alias URLs that
    point to the same article and older revisions of a changed page."""
    rows = conn.execute(
        "SELECT DISTINCT a.url FROM perk_sources ps "
        "JOIN articles a ON a.id = ps.article_id WHERE ps.perk_id = ?",
        (perk_id,),
    ).fetchall()
    urls = [r["url"] for r in rows]
    aliases = conn.execute(
        "SELECT DISTINCT al.url_norm FROM perk_sources ps "
        "JOIN article_aliases al ON al.article_id = ps.article_id WHERE ps.perk_id = ?",
        (perk_id,),
    ).fetchall()
    return urls + [r["url_norm"] for r in aliases if r["url_norm"] not in urls]


def history(conn: sqlite3.Connection, perk_id: int) -> dict[str, list[dict[str, Any]]]:
    status_rows = conn.execute(
        "SELECT * FROM perk_status_history WHERE perk_id = ? ORDER BY created_at", (perk_id,)
    ).fetchall()
    version_rows = conn.execute(
        "SELECT * FROM perk_versions WHERE perk_id = ? ORDER BY created_at", (perk_id,)
    ).fetchall()
    verify_rows = conn.execute(
        "SELECT * FROM verification_runs WHERE perk_id = ? ORDER BY checked_at", (perk_id,)
    ).fetchall()
    evidence_rows = conn.execute(
        "SELECT * FROM perk_evidence WHERE perk_id = ? ORDER BY created_at", (perk_id,)
    ).fetchall()
    return {
        "status": [dict(r) for r in status_rows],
        "versions": [
            {**dict(r), "snapshot": json.loads(r["snapshot"]),
             "changed_fields": json.loads(r["changed_fields"] or "[]")}
            for r in version_rows
        ],
        "verifications": [dict(r) for r in verify_rows],
        "evidence": [dict(r) for r in evidence_rows],
    }


def to_csv(conn: sqlite3.Connection, items: list[dict[str, Any]], *,
           english: bool = False) -> str:
    """One row per benefit. Chinese headers by default; ``english=True`` for the raw
    column names. All source URLs are flattened in, so an export never loses the
    provenance the dedupe step preserved."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
    writer.writerow({name: (name if english else _header_cn(name)) for name in EXPORT_COLUMNS})
    for item in items:
        row = {name: item.get(name) for name in EXPORT_COLUMNS}
        row["status"] = item.get("status_label")
        row["benefit_type"] = item.get("benefit_label")
        for flag in ("student_verification_required", "payment_method_required",
                     "invite_required", "user_claimed", "needs_review"):
            value = item.get(flag)
            row[flag] = {1: "是", 0: "否", None: "未说明"}.get(value, value)
        urls = all_urls_for(conn, item["id"]) if item.get("id") else []
        row["source_count"] = len(urls)
        row["source_urls"] = " | ".join(urls)
        writer.writerow(row)
    return buffer.getvalue()


def to_json(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> str:
    payload = []
    for item in items:
        record = {**item, "urls": all_urls_for(conn, item["id"]) if item.get("id") else []}
        if item.get("id"):
            record["history"] = history(conn, item["id"])
        payload.append(record)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


HEADER_CN = {
    "id": "ID", "product": "产品", "benefit_type": "福利类型", "entitlement": "权益描述",
    "quota": "额度", "starts_at": "开始日期", "ends_at": "截止日期", "days_left": "剩余天数",
    "status": "状态", "confidence": "可信度", "region": "适用地区", "eligibility": "用户限制",
    "student_verification_required": "需学生认证", "payment_method_required": "需绑卡",
    "invite_required": "需邀请", "needs_review": "待核实", "review_reason": "待核实原因",
    "user_claimed": "已领取", "claim_url": "领取入口", "rules_url": "官方规则",
    "first_seen_at": "首次发现", "last_verified_at": "最后核验",
    "source_count": "来源数", "source_urls": "全部来源",
}


def _header_cn(name: str) -> str:
    return HEADER_CN.get(name, name)


def counts(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {"perks_by_status": {}, "articles_by_state": {}, "sources": {}}
    for row in conn.execute("SELECT status, COUNT(*) n FROM perks GROUP BY status"):
        out["perks_by_status"][STATUS_LABELS.get(row["status"], row["status"])] = row["n"]
    for row in conn.execute("SELECT analyze_state, COUNT(*) n FROM articles GROUP BY analyze_state"):
        out["articles_by_state"][row["analyze_state"]] = row["n"]
    out["sources"]["enabled"] = conn.execute("SELECT COUNT(*) n FROM sources WHERE enabled=1").fetchone()["n"]
    out["sources"]["errored"] = conn.execute(
        "SELECT COUNT(*) n FROM sources WHERE last_error IS NOT NULL AND enabled=1").fetchone()["n"]
    out["articles"] = conn.execute("SELECT COUNT(*) n FROM articles").fetchone()["n"]
    out["perks"] = conn.execute("SELECT COUNT(*) n FROM perks").fetchone()["n"]
    return out
