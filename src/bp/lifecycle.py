from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from . import timeutil
from .config import get_settings
from .store import record_status

LIVE_STATUSES = ("pending", "confirmed")
ACTIVE_STATUSES = ("pending", "confirmed")


def _placeholders(values: tuple[str, ...]) -> str:
    return ", ".join("?" for _ in values)


def sweep_expired(conn: sqlite3.Connection, *, on_day: date | None = None) -> list[int]:
    """Close out every perk whose stated deadline has passed.

    A perk with no stated ``ends_at`` is deliberately left alone and stays
    待核实/已确认: absence of a deadline is not evidence of expiry.
    """
    today = on_day or timeutil.bj_today()
    cutoff = today.isoformat()
    rows = conn.execute(
        f"SELECT id, status, ends_at FROM perks "
        f"WHERE status IN ({_placeholders(LIVE_STATUSES)}) "
        "AND ends_at IS NOT NULL AND ends_at != '' AND ends_at < ?",
        (*LIVE_STATUSES, cutoff),
    ).fetchall()

    expired: list[int] = []
    now = timeutil.iso(timeutil.now_utc())
    for row in rows:
        conn.execute("UPDATE perks SET status = 'expired', updated_at = ?, verification_id = NULL WHERE id = ?",
                     (now, row["id"]))
        record_status(conn, row["id"], row["status"], "expired",
                      f"截止日期 {row['ends_at']} 已过", "scheduler")
        expired.append(int(row["id"]))
    return expired


def flag_incomplete(conn: sqlite3.Connection) -> list[int]:
    """Anything we cannot act on is flagged rather than silently dropped: a perk
    with no entry URL is useless to the user even if the model was confident.

    Only rows whose flag actually changes are written. Rewriting an already-flagged
    row would move ``updated_at``, and the report windows ``needs_review`` on
    ``updated_at`` — so a no-op tick would push the same perk into every later
    report and the daily mail would repeat itself forever.
    """
    reason = "缺少领取入口链接"
    rows = conn.execute(
        "SELECT id FROM perks WHERE claim_url IS NULL AND status IN ('pending', 'dubious') "
        "AND (needs_review = 0 OR review_reason IS NULL OR review_reason != ?)",
        (reason,),
    ).fetchall()
    now = timeutil.iso(timeutil.now_utc())
    ids = []
    for row in rows:
        conn.execute("UPDATE perks SET needs_review = 1, review_reason = ?, updated_at = ? WHERE id = ?",
                     (reason, now, row["id"]))
        ids.append(int(row["id"]))
    return ids


@dataclass
class DaysLeft:
    ends_at: str
    days: int


def days_remaining(ends_at: str | None, *, on_day: date | None = None) -> int | None:
    if not ends_at:
        return None
    try:
        deadline = date.fromisoformat(ends_at[:10])
    except ValueError:
        return None
    return (deadline - (on_day or timeutil.bj_today())).days


def countdown(days_left: int | None) -> str:
    """Deadline in the words a person scanning an email actually wants.

    ``ends_at: 2026-10-16`` makes you do arithmetic at 07:00; "明天截止" does not.
    A missing date is stated as missing rather than rendered as a blank.
    """
    if days_left is None:
        return "未注明截止日"
    if days_left < 0:
        return f"已过期 {-days_left} 天"
    if days_left == 0:
        return "今天截止"
    if days_left == 1:
        return "明天截止"
    return f"剩 {days_left} 天"


def expiring_soon(conn: sqlite3.Connection, *, on_day: date | None = None,
                  window_days: int | None = None) -> list[sqlite3.Row]:
    settings = get_settings()
    window = window_days if window_days is not None else settings.expiring_soon_days
    today = on_day or timeutil.bj_today()
    rows = conn.execute(
        f"SELECT * FROM perks WHERE status IN ({_placeholders(ACTIVE_STATUSES)}) "
        "AND ends_at IS NOT NULL AND ends_at != '' ORDER BY ends_at",
        ACTIVE_STATUSES,
    ).fetchall()
    out = []
    for row in rows:
        remaining = days_remaining(row["ends_at"], on_day=today)
        if remaining is not None and 0 <= remaining <= window:
            out.append(row)
    return out


def mark_claimed(conn: sqlite3.Connection, perk_id: int, claimed: bool, note: str | None = None) -> bool:
    """User-facing flag. Orthogonal to the system status machine, so marking a perk
    claimed never rewrites what verification concluded."""
    cursor = conn.execute(
        "UPDATE perks SET user_claimed = ?, user_note = COALESCE(?, user_note), updated_at = ? WHERE id = ?",
        (1 if claimed else 0, note, timeutil.iso(timeutil.now_utc()), perk_id),
    )
    return cursor.rowcount > 0
