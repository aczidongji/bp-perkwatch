from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import db, lifecycle, timeutil
from .config import PROJECT_ROOT, get_settings
from .normalize import normalize_text

STATUS_LABELS = {
    "pending": "待核实",
    "confirmed": "已确认",
    "expired": "已过期",
    "invalid": "无效",
    "dubious": "存疑",
}

BENEFIT_LABELS = {
    "free_membership": "免费会员",
    "free_credits": "免费额度",
    "redemption_code": "兑换码",
    "student_discount": "学生优惠",
    "free_trial": "限时试用",
    "discount": "促销折扣",
    "giveaway": "赠送/抽奖",
    "other": "其他",
    None: "未定性",
}

SECTIONS = (
    ("new_found", "新发现的候选福利"),
    ("rule_updates", "规则有更新的福利"),
    ("confirmed_live", "已确认且仍在有效期内"),
    ("expiring_soon", "即将截止"),
    ("recently_expired", "本周期内过期或失效"),
    ("needs_review", "需要你人工判断"),
)

# 六个板块是同一批福利的六个视角，彼此天然重叠：一条缺链接的新福利同时是
# 「新发现」和「需人工判断」，一封邮件里就把同一条渲染三遍（实测 28 条 → 59 次）。
# 所以一条福利在一封日报里只出现一次，归到它最该被看到的那个板块。
SECTION_PRIORITY = (
    "expiring_soon",       # 有时间窗口，错过就没了
    "recently_expired",    # 已经关掉，只报一次
    "confirmed_live",      # 已经核实过，比"候选"更值得先说；它每天都会被重新列出，不会消失
    "new_found",           # 第一次见到它 —— 就算同轮里有来源合并触发 rule_update，那也是"新发现"
    "rule_updates",        # 之前报过、这轮规则真的变了
    "needs_review",        # 剩下的都是「上次就提过、还卡着」
)


def assign_sections(
        sections: dict[str, list[dict[str, Any]]]) -> tuple[dict[str, list[dict[str, Any]]], dict[int, list[str]]]:
    """Fold each perk into one section. Returns the per-section lists to render and,
    for reporting, which sections each folded item was dropped from."""
    owner: dict[int, str] = {}
    kept: dict[str, list[dict[str, Any]]] = {}
    dropped: dict[int, list[str]] = {}
    for key in SECTION_PRIORITY:
        rows: list[dict[str, Any]] = []
        for item in sections[key]:
            first = owner.get(item["id"])
            if first is None:
                owner[item["id"]] = key
                rows.append(item)
            else:
                dropped.setdefault(item["id"], []).append(key)
        kept[key] = rows
    return {key: kept[key] for key in sections}, dropped


@dataclass
class Report:
    day: str
    generated_at: str
    markdown: str
    html: str
    sections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None
    already_notified: list[int] = field(default_factory=list)

    @property
    def total_items(self) -> int:
        return sum(len(v) for v in self.sections.values())


def _source_urls(conn: sqlite3.Connection, perk_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT a.url, a.title FROM perk_sources ps "
        "JOIN articles a ON a.id = ps.article_id WHERE ps.perk_id = ? "
        "ORDER BY ps.added_at DESC LIMIT 5",
        (perk_id,),
    ).fetchall()
    return [r["url"] for r in rows]


def _hydrate(conn: sqlite3.Connection, rows: list[sqlite3.Row],
             today: date) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        remaining = lifecycle.days_remaining(row["ends_at"], on_day=today)
        verified = timeutil.parse_iso(row["last_verified_at"])
        items.append({
            **dict(row),
            "status_label": STATUS_LABELS.get(row["status"], row["status"]),
            "benefit_label": BENEFIT_LABELS.get(row["benefit_type"]),
            "days_left": remaining,
            "countdown": lifecycle.countdown(remaining),
            "urgent": remaining is not None and remaining <= 2,
            # Stored as UTC; a mail stamped 北京时间 must not show 11:32 for 19:32.
            "verified_at_local": timeutil.bj_dt(verified).strftime("%m-%d %H:%M") if verified else None,
            "sources": _source_urls(conn, row["id"]),
            "confidence_pct": int((row["confidence"] or 0) * 100),
        })
    return items


def group_by_product(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold one section's items under their product, in order of first appearance.

    The same vendor's offers arrive as separate perks (a free tier, a student deal,
    a code), and listing them flat means the reader has to reassemble "what does
    Cursor actually give me" across the whole mail. Item order inside a group is the
    section's own ordering, so "即将截止" stays deadline-sorted.
    """
    groups: dict[str, dict[str, Any]] = {}
    for item in items:
        key = item["product_norm"] or normalize_text(item["product"] or "")
        label = item["product"] or "(未识别产品)"
        group = groups.setdefault(key or label, {"product": label, "perks": []})
        group["perks"].append(item)
    out = list(groups.values())
    for group in out:
        group["count"] = len(group["perks"])
    return out


def build_sections(conn: sqlite3.Connection, *, since: str | None, today: date) -> dict[str, list[dict[str, Any]]]:
    """Everything for one report, from the database alone. No model call: the 10:00
    delivery lands inside DeepSeek's peak window, so report rendering must be free.

    ``since`` is the previous report's timestamp. On the very first report it is
    None, and then the whole history counts as new — anything else would make the
    first mail arrive empty.
    """
    window = "first_seen_at > ?" if since else "1=1"
    updated_window = "updated_at > ?" if since else "1=1"
    version_window = "v.created_at > ?" if since else "1=1"
    params: tuple[str, ...] = (since,) if since else ()

    new_found = conn.execute(
        f"SELECT * FROM perks WHERE {window} ORDER BY first_seen_at DESC LIMIT 60", params
    ).fetchall()

    updates = conn.execute(
        f"SELECT p.* FROM perks p JOIN perk_versions v ON v.perk_id = p.id "
        f"WHERE v.reason = 'rule_update' AND {version_window} GROUP BY p.id "
        "ORDER BY p.updated_at DESC LIMIT 40", params
    ).fetchall()

    confirmed_live = conn.execute(
        "SELECT * FROM perks WHERE status = 'confirmed' AND user_claimed = 0 "
        "AND (ends_at IS NULL OR ends_at >= ?) ORDER BY ends_at IS NULL, ends_at LIMIT 80",
        (today.isoformat(),),
    ).fetchall()

    settings = get_settings()
    expiring = []
    for row in lifecycle.expiring_soon(conn, on_day=today):
        if row["status"] in ("pending", "confirmed"):
            expiring.append(row)
    expiring = expiring[:40]

    recently_expired = conn.execute(
        f"SELECT * FROM perks WHERE status IN ('expired', 'invalid') AND {updated_window} "
        "ORDER BY updated_at DESC LIMIT 40", params
    ).fetchall()

    # 「需要你人工判断」是一个**仍未解决的状态**，不是"这段时间发生过的事"。加了
    # updated_at > since 之后，一条 pending、没有截止日期、之后又没人动过的福利
    # 只在它第一次被看见的那封日报里出现过一次：flag_incomplete 不会重复推它的
    # updated_at，于是它从此在所有日报里消失——恰好与模板里"不会消失"的承诺相反。
    # 所以这个板块不看时间窗，一直列到状态变化或用户标记已领取为止。
    needs_review = conn.execute(
        "SELECT * FROM perks WHERE needs_review = 1 AND status IN ('pending', 'dubious') "
        "AND user_claimed = 0 ORDER BY updated_at DESC LIMIT 60"
    ).fetchall()

    return {
        "new_found": _hydrate(conn, new_found, today),
        "rule_updates": _hydrate(conn, updates, today),
        "confirmed_live": _hydrate(conn, confirmed_live, today),
        "expiring_soon": _hydrate(conn, expiring, today),
        "recently_expired": _hydrate(conn, recently_expired, today),
        "needs_review": _hydrate(conn, needs_review, today),
    }


def render(markdown_ctx: dict[str, Any]) -> tuple[str, str]:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    env = Environment(
        loader=FileSystemLoader(str(PROJECT_ROOT / "templates")),
        autoescape=select_autoescape(["html.j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template("report.md.j2").render(**markdown_ctx), \
        env.get_template("report.html.j2").render(**markdown_ctx)


def last_report_watermark(conn: sqlite3.Connection) -> str | None:
    """'New' is relative to the last report that actually reached the user.

    Reading ``generated_at`` instead would let a preview move the baseline forward:
    ``bp tick --dry-run``, ``bp report`` without ``--send``, or simply generating
    twice in one day all write a reports row, and everything discovered before that
    row would then never be delivered to anyone. Only a sent mail counts as seen.
    """
    row = conn.execute(
        "SELECT sent_at FROM reports WHERE sent_at IS NOT NULL ORDER BY sent_at DESC LIMIT 1"
    ).fetchone()
    return row["sent_at"] if row else None


def record_send_failure(conn: sqlite3.Connection, day: str, error: str | None) -> None:
    """Write why a report did not go out.

    The column existed but was never written, so a failed delivery left no trace in
    the database — the mail was gone and only stdout knew why.
    """
    conn.execute("UPDATE reports SET send_error = ? WHERE day = ?", ((error or "")[:500], day))


def record_dispatched(conn: sqlite3.Connection, generated: "Report") -> int:
    """Log every perk that went out in this report, keyed on a fingerprint of its
    current rules, so a later run only re-notifies when something meaningful changed.

    Called on every successful delivery — the timer's and ``bp report --send``'s
    alike — because the suppression in :func:`generate` reads these rows.
    """
    written = 0
    for kind, rows in generated.sections.items():
        for item in rows:
            if mark_notified(conn, item["id"], kind, item, generated.day):
                written += 1
    return written


def generate(conn: sqlite3.Connection, *, on_day: date | None = None,
             write_file: bool = True) -> Report:
    settings = get_settings()
    today = on_day or timeutil.bj_today()
    now = timeutil.iso(timeutil.now_utc())
    since = last_report_watermark(conn)
    sections, dropped = assign_sections(build_sections(conn, since=since, today=today))
    already = already_notified(conn, sections)
    if not settings.report_repeat_live_list:
        # 关掉"每天重复常驻清单"后，同一状态的福利只上一次日报。fingerprint 含
        # status，所以状态一变（比如待核实 -> 已确认）它又会回来。开关开着时不做
        # 任何过滤——常驻清单本来就是要天天出现。
        sections["confirmed_live"] = [
            item for item in sections["confirmed_live"] if item["id"] not in already
        ]

    ctx = {
        "day": today.isoformat(),
        "generated_at_local": datetime.now(timeutil.BEIJING).strftime("%Y-%m-%d %H:%M 北京时间"),
        "sections": [(key, title, sections[key], group_by_product(sections[key]))
                     for key, title in SECTIONS],
        "total_items": sum(len(sections[key]) for key, _title in SECTIONS),
        "folded_items": len(dropped),
        "stats": {
            "counts": {key: len(sections[key]) for key, _title in SECTIONS},
            "folded": len(dropped),
            "since": since,
            "statuses": status_breakdown(conn),
        },
        "status_labels": STATUS_LABELS,
        "expiring_window_days": settings.expiring_soon_days,
    }
    markdown, html = render(ctx)

    path = None
    if write_file:
        directory = settings.reports_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"perk-report-{today.isoformat()}.md"
        path.write_text(markdown, encoding="utf-8")

    stats = db.dumps(ctx["stats"])
    conn.execute(
        "INSERT INTO reports (day, generated_at, markdown_path, stats) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(day) DO UPDATE SET generated_at = excluded.generated_at, "
        "markdown_path = excluded.markdown_path, stats = excluded.stats",
        (today.isoformat(), now, str(path) if path else None, stats),
    )
    conn.commit()

    return Report(day=today.isoformat(), generated_at=now, markdown=markdown, html=html,
                  sections=sections, stats=ctx["stats"], path=path,
                  already_notified=already)


def status_breakdown(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts keyed by display label. The templates iterate this mapping directly, so
    a raw key and its label must never appear as two separate rows for one status."""
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM perks GROUP BY status").fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        label = STATUS_LABELS.get(row["status"], row["status"])
        counts[label] = counts.get(label, 0) + row["n"]
    ordered = {label: counts.pop(label, 0) for label in STATUS_LABELS.values()}
    ordered.update(counts)
    return ordered


def fingerprint(row: dict[str, Any]) -> str:
    """Changes worth telling the user about. A repost of an unchanged offer must not
    re-notify, but a new deadline, a new status or new entitlement text will."""
    parts = [
        str(row.get("status")),
        str(row.get("ends_at")),
        str(row.get("claim_url")),
        normalize_text(str(row.get("entitlement") or ""))[:80],
        normalize_text(str(row.get("quota") or ""))[:40],
    ]
    return db.dumps(parts)


def mark_notified(conn: sqlite3.Connection, perk_id: int, kind: str,
                  row: dict[str, Any], day: str, channel: str = "email") -> bool:
    """True when this is worth sending; False when this exact state was already
    delivered. UNIQUE on dedupe_key is the mechanism."""
    key = f"{perk_id}:{kind}:{fingerprint(row)}"
    cursor = conn.execute(
        "INSERT INTO notifications (perk_id, channel, kind, dedupe_key, report_day, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(dedupe_key) DO NOTHING",
        (perk_id, channel, kind, key, day, timeutil.iso(timeutil.now_utc())),
    )
    return cursor.rowcount > 0


def already_notified(conn: sqlite3.Connection, sections: dict[str, list[dict[str, Any]]]) -> list[int]:
    """Perk ids whose current state has already been delivered, used by the sender to
    avoid repeating the same offer every day."""
    keys = []
    for kind, rows in sections.items():
        for row in rows:
            keys.append(f"{row['id']}:{kind}:{fingerprint(row)}")
    if not keys:
        return []
    marks = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT perk_id FROM notifications WHERE dedupe_key IN ({marks})", keys
    ).fetchall()
    return [int(r["perk_id"]) for r in rows]
