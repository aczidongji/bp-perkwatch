"""tick 状态机：机器经常关机，所以"到点触发"不成立，只成立"今天这件事还没做过"。

覆盖用户定的时刻表：18:00 后开始采集分析（避开高峰计费），次日 10:00 发日报；
10:00 之后才开机则开机即发；连续几天没开机只发最新的一份。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from helpers import add_perk, authorise, perk

from bp import config, db, report as report_mod, scheduler, store
from bp.notify.email import SendResult
from bp.timeutil import BEIJING, UTC


def bj(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=BEIJING).astimezone(UTC)


MON = date(2026, 10, 12)      # 工作日
SAT = date(2026, 10, 17)      # 周末，永远低谷


@pytest.fixture
def recorder(monkeypatch):
    """把外呼（采集/模型/SMTP）全部换成计数桩。"""
    calls: dict[str, list] = {"run_once": [], "send": []}

    def fake_run_once(conn, *, force_analysis=False, **kw):
        calls["run_once"].append(force_analysis)
        return {"articles_new": 3, "articles_duplicate": 1, "analysed": 2,
                "prefiltered": 1, "perks_created": 2, "verified": 0,
                "stopped_early": False, "errors": []}

    def fake_send(report, *, dry_run=False, recipients=None):
        calls["send"].append(report.day)
        return SendResult(ok=True, recipients=list(recipients or ["me@gmail.com"]),
                          attachment=f"perk-report-{report.day}.md", bytes=4096,
                          dry_run=dry_run)

    monkeypatch.setattr(scheduler.pipeline, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "send_report", fake_send)
    return calls


@pytest.fixture
def mail_on(test_env, monkeypatch):
    monkeypatch.setenv("REPORT_TO", "me@gmail.com")
    monkeypatch.setenv("SMTP_USERNAME", "bot@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


class TestNightlyWindow:
    def test_before_six_pm_no_collection_is_scheduled_when_yesterday_ran(self, conn, recorder, mail_on):
        db.set_meta(conn, f"night_run_{(MON - timedelta(days=1)).isoformat()}", "2026-10-11T10:00:00Z")
        conn.commit()
        result = scheduler.tick(conn, now=bj(2026, 10, 12, 15, 0))
        assert recorder["run_once"] == []
        assert any("尚未开始" in s for s in result.skipped)

    def test_at_six_ten_the_run_happens_once_and_is_watermarked(self, conn, recorder, mail_on):
        db.set_meta(conn, f"night_run_{(MON - timedelta(days=1)).isoformat()}", "x")
        conn.commit()
        scheduler.tick(conn, now=bj(2026, 10, 12, 18, 10))
        assert recorder["run_once"] == [False]
        assert db.get_meta(conn, f"night_run_{MON.isoformat()}")

    def test_second_tick_the_same_evening_does_nothing(self, conn, recorder, mail_on):
        db.set_meta(conn, f"night_run_{(MON - timedelta(days=1)).isoformat()}", "x")
        conn.commit()
        scheduler.tick(conn, now=bj(2026, 10, 12, 18, 10))
        result = scheduler.tick(conn, now=bj(2026, 10, 12, 18, 25))
        assert len(recorder["run_once"]) == 1
        assert any("已完成" in s for s in result.skipped)

    def test_missed_evening_is_made_up_the_next_morning(self, conn, recorder, mail_on):
        """前一天没开机 → 第二天开机即在低谷时段补跑，不用等到 18:05。"""
        scheduler.tick(conn, now=bj(2026, 10, 12, 12, 30))
        assert len(recorder["run_once"]) == 1, "存在隔夜欠账，应当立即补跑"

    def test_carryover_still_refuses_to_spend_during_peak_billing(self, conn, recorder, mail_on):
        """周日上午开机是低谷，可以补跑；工作日 10:30 是高峰，没有 FORCE 就推迟。"""
        result = scheduler.tick(conn, now=bj(2026, 10, 12, 10, 30))
        assert recorder["run_once"] == []
        assert any("低谷" in s for s in result.skipped)
        assert not db.get_meta(conn, f"night_run_{MON.isoformat()}")

    def test_carryover_on_a_weekend_is_run_immediately(self, conn, recorder, mail_on):
        """周五晚没开机、周六上午开机：周末全天半价，立即补跑。"""
        scheduler.tick(conn, now=bj(2026, 10, 17, 10, 30))
        assert len(recorder["run_once"]) == 1, "周末全天半价，开机即可补跑"
        assert db.get_meta(conn, f"night_run_{SAT.isoformat()}")

    def test_force_analysis_overrides_the_peak_refusal(self, conn, recorder, mail_on):
        scheduler.tick(conn, now=bj(2026, 10, 12, 10, 30), force_analysis=True)
        assert recorder["run_once"] == [True]

    def test_dry_run_previews_without_consuming_the_night(self, conn, recorder, mail_on):
        db.set_meta(conn, f"night_run_{(MON - timedelta(days=1)).isoformat()}", "x")
        conn.commit()
        scheduler.tick(conn, now=bj(2026, 10, 12, 18, 10), dry_run=True)
        assert recorder["run_once"] == []
        assert db.get_meta(conn, f"night_run_{MON.isoformat()}") is None, "试运行不得占用今晚的名额"


class TestWatermarkIsNotConsumedByAnEmptyRun:
    """水位写着"今晚已完成"，所以只有在真的干完了时才该写。"""

    @staticmethod
    def _stopped(monkeypatch):
        """模拟"分析被计费窗口截断"：文章采到了，模型一条都没分析完。"""
        calls: list = []

        def fake_run_once(conn, *, force_analysis=False, **kw):
            calls.append(force_analysis)
            return {"articles_new": 3, "articles_duplicate": 1, "analysed": 0,
                    "prefiltered": 0, "perks_created": 0, "verified": 0,
                    "stopped_early": True, "errors": []}

        monkeypatch.setattr(scheduler.pipeline, "run_once", fake_run_once)
        return calls

    def test_a_run_cut_short_by_the_billing_window_does_not_burn_the_night(
            self, conn, mail_on, monkeypatch):
        """分析被窗口截断 -> 当晚的欠账还在。写水位会让积压等到明晚才有人管。"""
        calls = self._stopped(monkeypatch)
        result = scheduler.tick(conn, now=bj(2026, 10, 12, 12, 30))
        assert len(calls) == 1, "补跑确实发生了"
        assert db.get_meta(conn, f"night_run_{MON.isoformat()}") is None
        assert any("截断" in s for s in result.skipped)

    def test_the_next_tick_tries_again_after_a_cut_short_run(
            self, conn, mail_on, monkeypatch):
        calls = self._stopped(monkeypatch)
        scheduler.tick(conn, now=bj(2026, 10, 12, 12, 30))
        scheduler.tick(conn, now=bj(2026, 10, 12, 12, 45))
        assert len(calls) == 2, "没干完就必须再试"

    def test_a_completed_run_still_watermarks_the_night(self, conn, recorder, mail_on):
        db.set_meta(conn, f"night_run_{(MON - timedelta(days=1)).isoformat()}", "x")
        conn.commit()
        scheduler.tick(conn, now=bj(2026, 10, 12, 18, 10))
        assert db.get_meta(conn, f"night_run_{MON.isoformat()}")


class TestMorningReport:
    def test_report_is_not_sent_before_ten(self, conn, recorder, mail_on):
        db.set_meta(conn, f"night_run_{MON.isoformat()}", "x")
        conn.commit()
        result = scheduler.tick(conn, now=bj(2026, 10, 13, 9, 30))
        assert recorder["send"] == []
        assert any("未到日报发送时间" in s for s in result.skipped)

    def test_report_goes_out_at_ten(self, conn, recorder, mail_on):
        add_perk(conn, perk(claim="https://cursor.com/x", ends="2026-12-31"))
        next_day = (MON + timedelta(days=1)).isoformat()
        db.set_meta(conn, f"night_run_{next_day}", "x")
        conn.commit()
        scheduler.tick(conn, now=bj(2026, 10, 13, 10, 5))
        assert recorder["send"] == [next_day]
        assert db.get_meta(conn, f"report_sent_{next_day}")
        row = conn.execute("SELECT sent_at FROM reports WHERE day = ?", (next_day,)).fetchone()
        assert row["sent_at"], "已发送必须记录，否则分不清「没发」和「发过了」"

    def test_only_one_mail_per_day(self, conn, recorder, mail_on):
        next_day = (MON + timedelta(days=1)).isoformat()
        db.set_meta(conn, f"night_run_{next_day}", "x")
        conn.commit()
        scheduler.tick(conn, now=bj(2026, 10, 13, 10, 5))
        scheduler.tick(conn, now=bj(2026, 10, 13, 10, 20))
        assert len(recorder["send"]) == 1

    def test_booting_at_noon_sends_the_report_immediately(self, conn, recorder, mail_on):
        """10:00 之后才开机 → 开机即发。"""
        next_day = (MON + timedelta(days=1)).isoformat()
        db.set_meta(conn, f"night_run_{next_day}", "x")
        conn.commit()
        scheduler.tick(conn, now=bj(2026, 10, 13, 13, 40))
        assert recorder["send"] == [next_day]

    def test_several_missed_days_send_only_the_newest(self, conn, recorder, mail_on):
        today = (MON + timedelta(days=3)).isoformat()
        for offset in (1, 2):
            day = (MON + timedelta(days=offset)).isoformat()
            conn.execute("INSERT INTO reports (day, generated_at) VALUES (?, ?)",
                         (day, f"{day}T10:00:00Z"))
        conn.execute("INSERT INTO reports (day, generated_at) VALUES (?, ?)",
                     (today, f"{today}T10:00:00Z"))
        db.set_meta(conn, f"night_run_{today}", "x")
        conn.commit()
        scheduler.tick(conn, now=bj(2026, 10, 15, 11, 0))
        assert recorder["send"] == [today], "连续几天没开机只发前一天/最新的一份"
        old = conn.execute("SELECT day, sent_at, superseded_at FROM reports WHERE day != ?",
                           (today,)).fetchall()
        assert len(old) == 2
        assert all(r["superseded_at"] for r in old), "落后的未发日报要标记为被合并取代"
        assert all(r["sent_at"] is None for r in old)

    def test_already_sent_reports_are_not_superseded_again(self, conn, recorder, mail_on):
        today = (MON + timedelta(days=3)).isoformat()
        conn.execute("INSERT INTO reports (day, generated_at, sent_at) VALUES (?, ?, ?)",
                     ("2026-10-12", "2026-10-12T10:00:00Z", "2026-10-12T10:00:01Z"))
        conn.execute("INSERT INTO reports (day, generated_at) VALUES (?, ?)",
                     (today, f"{today}T10:00:00Z"))
        db.set_meta(conn, f"night_run_{today}", "x")
        conn.commit()
        scheduler.tick(conn, now=bj(2026, 10, 15, 11, 0))
        row = conn.execute("SELECT superseded_at FROM reports WHERE day = '2026-10-12'").fetchone()
        assert row["superseded_at"] is None

    def test_transient_smtp_failure_is_retried_on_the_next_tick(self, conn, monkeypatch, mail_on):
        def fail(report, *, dry_run=False, recipients=None):
            return SendResult(ok=False, error="OSError: connection reset")

        monkeypatch.setattr(scheduler, "send_report", fail)
        next_day = (MON + timedelta(days=1)).isoformat()
        db.set_meta(conn, f"night_run_{next_day}", "x")
        conn.commit()
        first = scheduler.tick(conn, now=bj(2026, 10, 13, 10, 5))
        assert any("发送失败" in s for s in first.skipped)
        assert db.get_meta(conn, f"report_sent_{next_day}") is None, "发失败不能把今天划掉"
        row = conn.execute("SELECT send_error FROM reports WHERE day = ?", (next_day,)).fetchone()
        assert "connection reset" in row["send_error"], "失败原因要留在库里，不能只在 stdout"
        second = scheduler.tick(conn, now=bj(2026, 10, 13, 10, 20))
        assert any("发送失败" in s for s in second.skipped)

    def test_unconfigured_mail_is_reported_without_a_loop(self, conn, recorder, monkeypatch):
        monkeypatch.setenv("REPORT_TO", "")
        config.get_settings.cache_clear()
        db.set_meta(conn, f"night_run_{MON.isoformat()}", "x")
        conn.commit()
        result = scheduler.tick(conn, now=bj(2026, 10, 12, 10, 30))
        assert recorder["send"] == []
        assert any("未配置" in s for s in result.skipped)

    def test_dispatch_records_notification_keys(self, conn, recorder, mail_on):
        pid = add_perk(conn, perk(claim="https://cursor.com/x", ends="2026-12-31"))
        next_day = (MON + timedelta(days=1)).isoformat()
        db.set_meta(conn, f"night_run_{next_day}", "x")
        conn.commit()
        scheduler.tick(conn, now=bj(2026, 10, 13, 10, 5))
        keys = conn.execute("SELECT perk_id, kind FROM notifications").fetchall()
        assert keys and {k["perk_id"] for k in keys} >= {pid}

    def test_the_standing_list_is_logged_so_it_can_be_suppressed_later(
            self, conn, recorder, mail_on, monkeypatch):
        """通知记录是去重的依据，所以每个板块都要落库——包括常驻清单。以前它只在
        开关打开时才记，于是关掉开关后没有任何依据可查，开关等于没接。"""
        next_day = (MON + timedelta(days=1)).isoformat()
        claim = "https://cursor.com/live"
        pid = add_perk(conn, perk(claim=claim, ends="2026-12-31"))
        authorise(conn, pid, claim)
        assert store.set_status(conn, pid, "confirmed", "官方页面仍可领取", "verification") is True
        db.set_meta(conn, f"night_run_{next_day}", "x")
        conn.commit()

        for flag in ("true", "false"):
            monkeypatch.setenv("REPORT_REPEAT_LIVE_LIST", flag)
            config.get_settings.cache_clear()
            conn.execute("DELETE FROM meta WHERE key = ?", (f"report_sent_{next_day}",))
            conn.execute("DELETE FROM reports WHERE day = ?", (next_day,))
            conn.commit()
            scheduler.tick(conn, now=bj(2026, 10, 13, 10, 5))
            kinds = {r["kind"] for r in conn.execute("SELECT kind FROM notifications")}
            assert "confirmed_live" in kinds, f"REPORT_REPEAT_LIVE_LIST={flag} 时也必须落库"

    def test_a_dry_run_morning_does_not_move_the_report_baseline(
            self, conn, recorder, mail_on):
        """试运行会把 reports 行写进去，但没发出去就不能算用户看过。"""
        next_day = (MON + timedelta(days=1)).isoformat()
        add_perk(conn, perk(claim="https://cursor.com/x", entitlement="试运行前发现"))
        db.set_meta(conn, f"night_run_{next_day}", "x")
        conn.commit()

        scheduler.tick(conn, now=bj(2026, 10, 13, 10, 5), dry_run=True)
        record = conn.execute("SELECT sent_at FROM reports WHERE day = ?", (next_day,)).fetchone()
        assert record is not None, "试运行也会生成日报行"
        assert record["sent_at"] is None
        assert report_mod.last_report_watermark(conn) is None, "dry-run 不得推进送达水位"


class TestTickMaintenance:
    def test_expiry_sweep_runs_on_every_tick(self, conn, recorder, mail_on):
        add_perk(conn, perk(claim="https://cursor.com/x", ends="2026-10-01"))
        result = scheduler.tick(conn, now=bj(2026, 10, 12, 3, 0))
        assert any("过期关闭" in a for a in result.actions)
        assert conn.execute("SELECT status FROM perks").fetchone()["status"] == "expired"

    def test_incomplete_perks_get_flagged(self, conn, recorder, mail_on):
        p = perk(entitlement="扫码进群领取", claim=None)
        pid = add_perk(conn, p)
        scheduler.tick(conn, now=bj(2026, 10, 12, 3, 0))
        assert conn.execute("SELECT needs_review FROM perks WHERE id = ?", (pid,)).fetchone()["needs_review"] == 1

    def test_tick_reports_a_readable_summary(self, conn, recorder, mail_on):
        db.set_meta(conn, f"night_run_{(MON - timedelta(days=1)).isoformat()}", "x")
        conn.commit()
        result = scheduler.tick(conn, now=bj(2026, 10, 12, 18, 10))
        assert "采集+分析" in str(result)
        assert "新文章 3" in str(result)

    def test_ten_ticks_in_a_row_do_one_night_run(self, conn, recorder, mail_on):
        db.set_meta(conn, f"night_run_{(MON - timedelta(days=1)).isoformat()}", "x")
        conn.commit()
        for minute in range(0, 100, 15):
            scheduler.tick(conn, now=bj(2026, 10, 12, 18, 5) + timedelta(minutes=minute))
        assert len(recorder["run_once"]) == 1


class TestNightlyBatchCap:
    """One unattended evening must not be allowed to spend the whole month's budget."""

    def _record_limits(self, monkeypatch, *, limit=80):
        from bp import pipeline

        seen: list = []

        def fake_collect(conn, **kw):
            return pipeline.RunCounts()

        def fake_analyse(conn, *, limit=None, force=False, extractor=None):
            seen.append(limit)
            return pipeline.RunCounts()

        monkeypatch.setattr(pipeline, "collect", fake_collect)
        monkeypatch.setattr(pipeline, "analyse_pending", fake_analyse)
        monkeypatch.setattr(pipeline.lifecycle, "sweep_expired", lambda c, **kw: [])
        monkeypatch.setattr(pipeline, "verify_new", lambda c, **kw: 0)
        return seen

    def test_default_limit_comes_from_settings(self, conn, monkeypatch, test_env):
        from bp import pipeline

        monkeypatch.setenv("ANALYSE_BATCH_LIMIT", "80")
        config.get_settings.cache_clear()
        seen = self._record_limits(monkeypatch)
        pipeline.run_once(conn)
        assert seen == [80]

    def test_zero_means_drain_the_queue(self, conn, monkeypatch, test_env):
        from bp import pipeline

        monkeypatch.setenv("ANALYSE_BATCH_LIMIT", "0")
        config.get_settings.cache_clear()
        seen = self._record_limits(monkeypatch)
        pipeline.run_once(conn)
        assert seen == [None]

    def test_explicit_limit_wins(self, conn, monkeypatch, test_env):
        from bp import pipeline

        monkeypatch.setenv("ANALYSE_BATCH_LIMIT", "80")
        config.get_settings.cache_clear()
        seen = self._record_limits(monkeypatch)
        pipeline.run_once(conn, limit=5)
        assert seen == [5]
