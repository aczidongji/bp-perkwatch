"""过期检测：截止日期一到就自动关闭，缺日期的一律不动。"""
from __future__ import annotations

from datetime import date

import pytest
from helpers import add_perk, article, authorise, perk

from bp import lifecycle, report, store

TODAY = date(2026, 10, 15)


class TestSweepExpired:
    def test_deadline_that_passed_closes_the_perk(self, conn):
        pid = add_perk(conn, perk(ends="2026-10-01", claim="https://cursor.com/a"))
        closed = lifecycle.sweep_expired(conn, on_day=TODAY)
        conn.commit()
        assert pid in closed
        row = conn.execute("SELECT status, verification_id FROM perks WHERE id = ?", (pid,)).fetchone()
        assert row["status"] == "expired"
        assert row["verification_id"] is None, "过期后不得残留旧的核验凭据"

    def test_deadline_today_is_still_live(self, conn):
        """最后一天仍然有效，不能提前关掉。"""
        pid = add_perk(conn, perk(ends="2026-10-15", claim="https://cursor.com/b"))
        assert lifecycle.sweep_expired(conn, on_day=TODAY) == []
        assert conn.execute("SELECT status FROM perks WHERE id = ?", (pid,)).fetchone()["status"] == "pending"

    def test_perk_without_a_deadline_is_never_expired_by_assumption(self, conn):
        """缺 ends_at 只说明文章没写，不构成过期证据。"""
        pid = add_perk(conn, perk(ends=None, claim="https://cursor.com/c"))
        assert lifecycle.sweep_expired(conn, on_day=TODAY) == []
        assert conn.execute("SELECT status FROM perks WHERE id = ?", (pid,)).fetchone()["status"] == "pending"

    def test_future_deadline_untouched(self, conn):
        pid = add_perk(conn, perk(ends="2026-12-31", claim="https://cursor.com/d"))
        assert lifecycle.sweep_expired(conn, on_day=TODAY) == []
        assert conn.execute("SELECT status FROM perks WHERE id = ?", (pid,)).fetchone()["status"] == "pending"

    def test_sweep_records_why_in_status_history(self, conn):
        pid = add_perk(conn, perk(ends="2026-09-30", claim="https://cursor.com/e"))
        lifecycle.sweep_expired(conn, on_day=TODAY)
        conn.commit()
        history = conn.execute(
            "SELECT from_status, to_status, reason, changed_by FROM perk_status_history "
            "WHERE perk_id = ? ORDER BY id", (pid,)).fetchall()
        assert [(h["from_status"], h["to_status"]) for h in history] == [("pending", "expired")]
        assert "2026-09-30" in history[0]["reason"]
        assert history[0]["changed_by"] == "scheduler"

    def test_second_sweep_does_not_repeat_the_transition(self, conn):
        pid = add_perk(conn, perk(ends="2026-09-30", claim="https://cursor.com/f"))
        lifecycle.sweep_expired(conn, on_day=TODAY)
        conn.commit()
        assert lifecycle.sweep_expired(conn, on_day=TODAY) == []
        n = conn.execute("SELECT COUNT(*) n FROM perk_status_history WHERE perk_id = ?",
                         (pid,)).fetchone()["n"]
        assert n == 1

    def test_confirmed_perk_also_closes_when_its_offer_ends(self, conn):
        """已确认的福利到期同样要关掉，否则日报会一直挂着过期活动。"""
        claim = "https://cursor.com/g"
        pid = add_perk(conn, perk(ends="2026-10-10", claim=claim))
        authorise(conn, pid, claim)
        assert store.set_status(conn, pid, "confirmed", "官方页面仍可领取", "verification") is True
        conn.commit()
        assert lifecycle.sweep_expired(conn, on_day=TODAY) == [pid]
        assert conn.execute("SELECT status FROM perks WHERE id = ?", (pid,)).fetchone()["status"] == "expired"

    def test_invalid_perks_are_not_reswept(self, conn):
        pid = add_perk(conn, perk(ends="2026-01-01", claim="https://cursor.com/h"))
        conn.execute("UPDATE perks SET status = 'invalid' WHERE id = ?", (pid,))
        conn.commit()
        assert lifecycle.sweep_expired(conn, on_day=TODAY) == []


class TestDaysRemaining:
    @pytest.mark.parametrize("value,expected", [
        ("2026-10-20", 5),
        ("2026-10-15", 0),
        ("2026-10-14", -1),
        ("2026-10-20T18:00:00", 5),
        (None, None),
        ("", None),
        ("活动结束于本月底", None),
        ("0000-13-45", None),
    ])
    def test_remaining_days(self, value, expected):
        assert lifecycle.days_remaining(value, on_day=TODAY) == expected


class TestExpiringSoon:
    def test_within_window_is_flagged(self, conn):
        pid = add_perk(conn, perk(ends="2026-10-20", claim="https://cursor.com/i"))
        rows = lifecycle.expiring_soon(conn, on_day=TODAY, window_days=7)
        assert [r["id"] for r in rows] == [pid]

    def test_outside_window_is_not_flagged(self, conn):
        add_perk(conn, perk(ends="2026-12-20", claim="https://cursor.com/j"))
        assert lifecycle.expiring_soon(conn, on_day=TODAY, window_days=7) == []

    def test_already_past_is_not_expiring_soon(self, conn):
        """过去的时间属于 expired 分支，不重复出现在“即将截止”。"""
        add_perk(conn, perk(ends="2026-10-01", claim="https://cursor.com/k"))
        assert lifecycle.expiring_soon(conn, on_day=TODAY, window_days=7) == []

    def test_last_day_counts(self, conn):
        pid = add_perk(conn, perk(ends="2026-10-15", claim="https://cursor.com/l"))
        assert [r["id"] for r in lifecycle.expiring_soon(conn, on_day=TODAY, window_days=1)] == [pid]


class TestIncompleteAndClaimed:
    def test_perk_without_entry_link_is_flagged_for_review(self, conn):
        """没有领取入口的福利对用户没有行动价值，必须进“需要人工判断”。"""
        p = perk(entitlement="扫码进群领取", claim=None)
        aid = article(conn, "https://mp.weixin.qq.com/s/abc")
        pid = store.upsert_perk(conn, p, aid).perk_id
        conn.commit()
        flagged = lifecycle.flag_incomplete(conn)
        conn.commit()
        assert pid in flagged
        row = conn.execute("SELECT needs_review, review_reason FROM perks WHERE id = ?", (pid,)).fetchone()
        assert row["needs_review"] == 1
        assert "领取入口" in row["review_reason"]

    def test_flagging_an_already_flagged_perk_is_a_no_op(self, conn):
        """每 15 分钟的 tick 都会跑一遍这个函数。重复打标若写入，就会推高 updated_at，
        而日报按 updated_at 开窗 → 待核实的项目会在之后每封邮件里不断重现。"""
        pid = add_perk(conn, perk(entitlement="扫码进群领取", claim=None))
        assert pid in lifecycle.flag_incomplete(conn)
        conn.commit()
        before = conn.execute("SELECT updated_at FROM perks WHERE id = ?", (pid,)).fetchone()["updated_at"]

        assert lifecycle.flag_incomplete(conn) == []
        conn.commit()
        row = conn.execute("SELECT updated_at, needs_review FROM perks WHERE id = ?", (pid,)).fetchone()
        assert row["needs_review"] == 1, "不打标不等于取消标记"
        assert row["updated_at"] == before

    def test_marking_claimed_does_not_rewrite_the_system_status(self, conn):
        pid = add_perk(conn, perk(ends="2026-11-01", claim="https://cursor.com/m"))
        assert lifecycle.mark_claimed(conn, pid, True, "已领到") is True
        row = conn.execute("SELECT status, user_claimed, user_note FROM perks WHERE id = ?",
                           (pid,)).fetchone()
        assert row["status"] == "pending"
        assert row["user_claimed"] == 1
        assert row["user_note"] == "已领到"

    def test_unclaiming_restores_the_flag(self, conn):
        pid = add_perk(conn, perk(ends="2026-11-01", claim="https://cursor.com/n"))
        lifecycle.mark_claimed(conn, pid, True)
        lifecycle.mark_claimed(conn, pid, False)
        assert conn.execute("SELECT user_claimed FROM perks WHERE id = ?",
                            (pid,)).fetchone()["user_claimed"] == 0

    def test_claiming_an_unknown_perk_reports_false(self, conn):
        assert lifecycle.mark_claimed(conn, 9999, True) is False


class TestCountdownWording:
    """倒计时是给扫邮件的人看的，不是给人做日期算术的。

    ``ends_at: 2026-10-16`` 需要在早上 7 点心算「还剩几天」，而「明天截止」不用。
    """

    def test_missing_date_is_stated_as_missing(self):
        assert lifecycle.countdown(None) == "未注明截止日"

    def test_today_and_tomorrow_are_words_not_numbers(self):
        assert lifecycle.countdown(0) == "今天截止"
        assert lifecycle.countdown(1) == "明天截止"

    def test_other_days_are_counted(self):
        assert lifecycle.countdown(2) == "剩 2 天"
        assert lifecycle.countdown(45) == "剩 45 天"

    def test_past_deadline_says_so(self):
        assert lifecycle.countdown(-3) == "已过期 3 天"

    def test_countdown_comes_from_the_same_number_the_sweep_uses(self, conn):
        """文案必须与 sweep/过期判定用的是同一个天数，否则日报说「明天截止」而当晚被关成过期。"""
        pid = add_perk(conn, perk(ends="2026-10-16", claim="https://cursor.com/x"))
        generated = report.build_sections(conn, since=None, today=date(2026, 10, 15))
        item = next(p for p in generated["new_found"] if p["id"] == pid)
        assert item["days_left"] == 1
        assert item["countdown"] == "明天截止"
        assert item["urgent"] is True
