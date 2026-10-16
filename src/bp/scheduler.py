"""The tick state machine.

Every scheduled behaviour in this project is one idempotent function that a timer
calls every 15 minutes. That is deliberate: the machine is a personal Ubuntu box
that is often powered off, so "fire at exactly 18:05" is useless. What matters is
"has today's run happened yet, and if not, do it now" — which is what survives a
missed window, a reboot, or several days off.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db, lifecycle, pipeline, report as report_mod, timeutil
from .config import get_settings
from .notify.email import send_report


@dataclass
class TickResult:
    actions: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    detail: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        parts = []
        if self.actions:
            parts.append("执行: " + ", ".join(self.actions))
        if self.skipped:
            parts.append("跳过: " + "; ".join(self.skipped))
        return " | ".join(parts) or "无事可做"


def _watermark(conn: sqlite3.Connection, key: str) -> str | None:
    return db.get_meta(conn, key)


def _set_watermark(conn: sqlite3.Connection, key: str, value: str) -> None:
    db.set_meta(conn, key, value)


def past_hour(now_bj, hour: int, minute: int = 0) -> bool:
    return (now_bj.hour, now_bj.minute) >= (hour, minute)


def tick(conn: sqlite3.Connection, *, now=None, dry_run: bool = False,
         force_analysis: bool | None = None, include_disabled: bool = False) -> TickResult:
    settings = get_settings()
    if force_analysis is None:
        force_analysis = settings.force_analysis_anytime
    moment = now or timeutil.now_utc()
    bj = timeutil.bj_dt(moment)
    today = bj.date()
    result = TickResult()

    # 1. Expiry sweep: cheap, always right to run, keeps the database honest.
    expired = lifecycle.sweep_expired(conn, on_day=today)
    if expired:
        result.actions.append(f"过期关闭 {len(expired)} 条")
    lifecycle.flag_incomplete(conn)
    conn.commit()

    # 2. Nightly collection + model analysis. Starts after DeepSeek's peak ends.
    night_key = f"night_run_{today.isoformat()}"
    if _watermark(conn, night_key) is None:
        # A run belongs to the evening that just began; before today's 18:05 the
        # backlog still belongs to yesterday's window.
        window_open = past_hour(bj, settings.pipeline_start_hour_bj, settings.pipeline_start_minute_bj)
        yesterday_key = f"night_run_{(today - timedelta(days=1)).isoformat()}"
        carryover = _watermark(conn, yesterday_key) is None

        if window_open or carryover:
            if not force_analysis and timeutil.is_deepseek_peak(moment) and not window_open:
                result.skipped.append("非低谷时段，模型分析推迟（设 FORCE_ANALYSIS_ANYTIME=1 可强制）")
            else:
                if dry_run:
                    result.actions.append("DRY-RUN 采集+分析")
                else:
                    summary = pipeline.run_once(conn, force_analysis=force_analysis)
                    result.detail["collect"] = summary
                    result.actions.append(
                        f"采集+分析（新文章 {summary['articles_new']}，"
                        f"重复 {summary['articles_duplicate']}，"
                        f"送模型 {summary['analysed']}，"
                        f"免费预筛 {summary['prefiltered']}，"
                        f"新福利 {summary['perks_created']}）"
                    )
                    if summary.get("stopped_early"):
                        result.actions.append("低谷窗口结束，分析中途停止")
                        # 分析被计费窗口截断，今晚的欠账还没还完。此时写水位会把这一
                        # 整天记成"已完成"，积压只能等到明晚才有人管，所以留着不写，
                        # 让下一趟继续（高峰期 tick 本身就不会进来，不会空转采集）。
                        result.skipped.append("分析被窗口截断，本夜稍后继续（未标记今晚已完成）")
                    else:
                        _set_watermark(conn, night_key, timeutil.iso(moment))
        else:
            result.skipped.append("今晚的采集窗口尚未开始")
    else:
        result.skipped.append("今日采集+分析已完成")

    # 3. Morning report. Never calls the model, so its 10:00 slot costs nothing even
    #    though 10:00 sits inside peak hours.
    report_key = f"report_sent_{today.isoformat()}"
    if past_hour(bj, settings.report_send_hour_bj) and _watermark(conn, report_key) is None:
        if not (settings.recipients() and settings.smtp_username and settings.smtp_password):
            # Nothing to retry into: the user has not configured mail yet.
            result.skipped.append("日报未发送：SMTP 凭据或 REPORT_TO 未配置（bp mail-test 可自检）")
        elif dry_run:
            note = _deliver(conn, today, dry_run=True)
            result.actions.append(note)
        else:
            note = _deliver(conn, today, dry_run=False)
            if note.startswith("日报发送失败"):
                # A transient SMTP/network failure must not burn today's slot: the
                # next tick tries again, which is what gets mail out after a reboot.
                result.skipped.append(note)
            else:
                _set_watermark(conn, report_key, timeutil.iso(moment))
                result.actions.append(note)
    else:
        result.skipped.append("未到日报发送时间或今日已发送")

    conn.commit()
    return result


def _deliver(conn: sqlite3.Connection, today: date, *, dry_run: bool) -> str:
    """Send exactly one report — the newest. Anything left unsent from earlier days
    is marked superseded rather than queued, which is what produces the required
    behaviour after the box has been off for several days."""
    stale = conn.execute(
        "SELECT id, day FROM reports WHERE sent_at IS NULL AND superseded_at IS NULL AND day != ?",
        (today.isoformat(),),
    ).fetchall()
    notes = []
    if stale:
        for row in stale:
            conn.execute("UPDATE reports SET superseded_at = ? WHERE id = ?",
                         (timeutil.iso(timeutil.now_utc()), row["id"]))
        notes.append(f"{len(stale)} 份历史未发日报被合并取代")

    generated = report_mod.generate(conn, on_day=today)
    if dry_run:
        return "DRY-RUN 日报已生成: " + str(generated.path)

    outcome = send_report(generated)
    if outcome.ok and not outcome.dry_run:
        conn.execute("UPDATE reports SET sent_at = ? WHERE day = ?",
                     (timeutil.iso(timeutil.now_utc()), today.isoformat()))
        report_mod.record_dispatched(conn, generated)
        return f"日报已发送至 {', '.join(outcome.recipients)}（{outcome.bytes // 1024} KB，附件 {outcome.attachment}）"
    report_mod.record_send_failure(conn, today.isoformat(), outcome.error)
    return f"日报发送失败: {outcome.error}"
