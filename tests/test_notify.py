"""通知去重与邮件封装。

用户要的是一份能收到附件的 Gmail 日报，而不是每天重复收到同一条福利。
"""
from __future__ import annotations

from datetime import date

import pytest
from helpers import add_perk, article, authorise, perk

from bp import config, report, store
from bp.models import PerkFields
from bp.notify import email as mail


def row(pid=1, product="Cursor", benefit="free_credits", entitlement="100 次免费补全",
        quota="100 次", ends="2026-12-31", claim="https://cursor.com/x", status="pending"):
    return {
        "id": pid, "product": product, "benefit_type": benefit, "entitlement": entitlement,
        "quota": quota, "ends_at": ends, "claim_url": claim, "status": status,
    }


class TestNotificationDedupe:
    @pytest.fixture
    def pids(self, conn):
        """notifications.perk_id 是外键，去重必须在真实福利行上验证。"""
        return [add_perk(conn, perk(product=f"Demo{i}", claim=f"https://demo{i}.com/x"))
                for i in (1, 2)]

    def test_first_delivery_is_announced(self, conn, pids):
        assert report.mark_notified(conn, pids[0], "new_found", row(pids[0]), "2026-10-15") is True

    def test_identical_state_is_not_sent_twice(self, conn, pids):
        report.mark_notified(conn, pids[0], "new_found", row(pids[0]), "2026-10-15")
        conn.commit()
        assert report.mark_notified(conn, pids[0], "new_found", row(pids[0]), "2026-10-16") is False
        n = conn.execute("SELECT COUNT(*) n FROM notifications").fetchone()["n"]
        assert n == 1

    def test_only_a_real_change_re_notifications(self, conn, pids):
        report.mark_notified(conn, pids[0], "new_found", row(pids[0]), "2026-10-15")
        conn.commit()
        assert report.mark_notified(conn, pids[0], "new_found",
                                    row(pids[0], status="confirmed"), "2026-10-16") is True, "状态变化必须再提醒一次"
        assert report.mark_notified(conn, pids[0], "new_found",
                                    row(pids[0], ends="2026-11-30"), "2026-10-17") is True, "截止日期变化要提醒"
        assert report.mark_notified(conn, pids[0], "new_found",
                                    row(pids[0], claim="https://cursor.com/y"), "2026-10-18") is True, "入口变化要提醒"
        assert report.mark_notified(conn, pids[0], "new_found",
                                    row(pids[0], quota="200 次"), "2026-10-19") is True, "额度变化要提醒"
        assert conn.execute("SELECT COUNT(*) n FROM notifications").fetchone()["n"] == 5

    def test_cosmetic_changes_do_not_re_notify(self, conn, pids):
        report.mark_notified(conn, pids[0], "new_found", row(pids[0]), "2026-10-15")
        conn.commit()
        noisy = {**row(pids[0]), "updated_at": "2026-10-20T00:00:00Z", "confidence": 0.95,
                 "product": "Cursor 编辑器", "first_seen_at": "2026-10-16T00:00:00Z"}
        assert report.mark_notified(conn, pids[0], "new_found", noisy, "2026-10-20") is False

    def test_surrounding_whitespace_is_not_a_new_offer(self, conn, pids):
        report.mark_notified(conn, pids[0], "new_found", row(pids[0]), "2026-10-15")
        conn.commit()
        assert report.mark_notified(conn, pids[0], "new_found",
                                    row(pids[0], entitlement="  100 次免费补全\n"), "2026-10-16") is False

    def test_a_really_different_amount_does_re_notify(self, conn, pids):
        report.mark_notified(conn, pids[0], "new_found", row(pids[0]), "2026-10-15")
        conn.commit()
        assert report.mark_notified(conn, pids[0], "new_found",
                                    row(pids[0], entitlement="100次免费补全改成1000次"), "2026-10-16") is True

    def test_different_perks_are_tracked_separately(self, conn, pids):
        report.mark_notified(conn, pids[0], "new_found", row(pids[0]), "2026-10-15")
        report.mark_notified(conn, pids[1], "new_found", row(pids[1]), "2026-10-15")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) n FROM notifications").fetchone()["n"] == 2

    def test_same_perk_in_two_sections_is_two_records_but_only_one_notice_each(self, conn, pids):
        assert report.mark_notified(conn, pids[0], "new_found", row(pids[0]), "2026-10-15") is True
        assert report.mark_notified(conn, pids[0], "expiring_soon", row(pids[0]), "2026-10-15") is True
        conn.commit()
        assert report.mark_notified(conn, pids[0], "expiring_soon", row(pids[0]), "2026-10-16") is False

    def test_channel_is_recorded(self, conn, pids):
        report.mark_notified(conn, pids[0], "new_found", row(pids[0]), "2026-10-15", channel="email")
        conn.commit()
        got = conn.execute("SELECT channel, kind, report_day, perk_id FROM notifications").fetchone()
        assert (got["channel"], got["kind"], got["report_day"], got["perk_id"]) == \
               ("email", "new_found", "2026-10-15", pids[0])

    def test_already_notified_surfaces_previously_delivered_perks(self, conn, pids):
        sections = {"new_found": [row(pids[0])]}
        assert report.already_notified(conn, sections) == []
        report.mark_notified(conn, pids[0], "new_found", row(pids[0]), "2026-10-15")
        conn.commit()
        assert report.already_notified(conn, sections) == [pids[0]]

    def test_dedupe_key_is_unique_at_the_database_level(self, conn, pids):
        """绕过 Python 直接插入也造不出重复通知。"""
        key = report.fingerprint(row(pids[0]))
        conn.execute("INSERT INTO notifications (perk_id, channel, kind, dedupe_key, sent_at) "
                     "VALUES (?, 'email', 'new_found', ?, '2026-10-15T00:00:00Z')", (pids[0], key))
        with pytest.raises(store.sqlite3.IntegrityError):
            conn.execute("INSERT INTO notifications (perk_id, channel, kind, dedupe_key, sent_at) "
                         "VALUES (?, 'email', 'new_found', ?, '2026-10-16T00:00:00Z')", (pids[0], key))

    def test_fingerprint_ignores_fields_that_do_not_change_the_offer(self, conn):
        assert report.fingerprint(row()) == report.fingerprint({**row(), "region": "全球"})


class TestReportGeneration:
    def test_report_is_written_as_markdown_file_and_row(self, conn, test_env):
        add_perk(conn, perk(ends="2026-12-31", claim="https://cursor.com/x"))
        generated = report.generate(conn, on_day=date(2026, 10, 15))
        assert generated.path is not None and generated.path.exists()
        assert generated.path.suffix == ".md"
        assert "Cursor" in generated.markdown
        stored = conn.execute("SELECT day, markdown_path FROM reports").fetchone()
        assert stored["day"] == "2026-10-15"
        assert stored["markdown_path"].endswith(".md")

    def test_generating_twice_for_one_day_replaces_the_row_not_duplicates_it(self, conn):
        add_perk(conn, perk(claim="https://cursor.com/x"))
        report.generate(conn, on_day=date(2026, 10, 15))
        report.generate(conn, on_day=date(2026, 10, 15))
        assert conn.execute("SELECT COUNT(*) n FROM reports").fetchone()["n"] == 1

    def test_only_a_delivered_report_moves_the_watermark(self, conn):
        """生成 ≠ 送达。读 generated_at 的话，dry-run、没加 --send、同一天重复生成
        都会把基线往前推，而它之前发现的福利永远不会出现在任何一封邮件里。"""
        assert report.last_report_watermark(conn) is None
        generated = report.generate(conn, on_day=date(2026, 10, 14))
        assert report.last_report_watermark(conn) is None, "没发出去就不能算已读"
        conn.execute("UPDATE reports SET sent_at = ? WHERE day = ?",
                     (generated.generated_at, generated.day))
        conn.commit()
        assert report.last_report_watermark(conn) == generated.generated_at

    def test_regenerating_without_sending_does_not_lose_new_perks(self, conn):
        add_perk(conn, perk(claim="https://cursor.com/a", entitlement="第一批"))
        preview = report.generate(conn, on_day=date(2026, 10, 14), write_file=False)
        assert len(preview.sections["new_found"]) == 1
        # 又一次生成（没发）之后，第一批福利必须仍然算"新"
        add_perk(conn, perk(claim="https://cursor.com/b", entitlement="第二批"))
        again = report.generate(conn, on_day=date(2026, 10, 14), write_file=False)
        assert len(again.sections["new_found"]) == 2, "空转的预览不得吞掉已发现的福利"

    def test_sections_cover_the_six_views_the_user_asked_for(self, conn):
        add_perk(conn, perk(claim="https://cursor.com/a", ends="2026-10-16"))
        generated = report.generate(conn)
        assert set(generated.sections) == {
            "new_found", "rule_updates", "confirmed_live",
            "expiring_soon", "recently_expired", "needs_review",
        }
        assert generated.total_items >= 1

    def test_html_rendition_escapes_untrusted_article_text(self, conn):
        """产品名与权益描述都来自外部文章，进 HTML 前必须转义。"""
        article_id = article(conn, "https://a.example/1")
        store.upsert_perk(conn, PerkFields(
            product="<img src=x onerror=alert(1)>", benefit_type="other",
            entitlement="含 <script> 标签的描述",
        ), article_id)
        conn.commit()
        generated = report.generate(conn)
        assert "<script>" not in generated.html
        assert "<img src=x" not in generated.html
        assert "&lt;script&gt;" in generated.html
        assert "onerror" in generated.html, "文本内容要保留，只是不再可执行"


def _status_table(markdown: str) -> dict[str, int]:
    """Parse the ``| 状态 | 数量 |`` block the report renders for the email header."""
    lines = markdown.splitlines()
    table = lines[lines.index("| 状态 | 数量 |") + 2:]
    out: dict[str, int] = {}
    for line in table:
        if not line.startswith("|"):
            break
        label, count = (cell.strip() for cell in line.strip("|").split("|"))
        out[label] = int(count)
    return out


class TestStatusBreakdown:
    """报告表格把 statuses 的键直接当状态名渲染，所以一个状态只能占一行。"""

    def test_every_label_appears_once_with_zeros_when_unused(self, conn):
        breakdown = report.status_breakdown(conn)
        assert list(breakdown) == list(report.STATUS_LABELS.values())
        assert all(value == 0 for value in breakdown.values())

    def test_counts_land_under_the_label_not_the_raw_status(self, conn):
        pid = add_perk(conn, perk(claim="https://cursor.com/a"))
        authorise(conn, pid, "https://cursor.com/a")
        store.set_status(conn, pid, "confirmed", "verified", "human")
        breakdown = report.status_breakdown(conn)
        assert breakdown["待核实"] == 0
        assert breakdown["已确认"] == 1
        assert set(breakdown) & set(report.STATUS_LABELS) == set()

    def test_report_markdown_has_no_english_status_rows(self, conn):
        add_perk(conn, perk(claim="https://cursor.com/b"))
        table = _status_table(report.generate(conn).markdown)
        assert set(table) == set(report.STATUS_LABELS.values())
        assert table["待核实"] == 1


class TestStandingLiveList:
    """REPORT_REPEAT_LIVE_LIST 之前只被用来决定"要不要写通知日志"，对报告内容零
    影响——开关是坏的。现在它真的决定常驻清单是否每天重复出现。"""

    def _confirmed(self, conn):
        pid = add_perk(conn, perk(claim="https://cursor.com/live", ends="2026-12-31"))
        authorise(conn, pid, "https://cursor.com/live")
        store.set_status(conn, pid, "confirmed", "官方页面仍可领取", "verification")
        conn.commit()
        return pid

    @staticmethod
    def _mark_sent(conn, generated):
        conn.execute("UPDATE reports SET sent_at = ? WHERE day = ?",
                     (generated.generated_at, generated.day))
        report.record_dispatched(conn, generated)
        conn.commit()

    def test_with_the_flag_on_the_standing_list_repeats_every_day(self, conn):
        pid = self._confirmed(conn)
        first = report.generate(conn, on_day=date(2026, 10, 15), write_file=False)
        assert [p["id"] for p in first.sections["confirmed_live"]] == [pid]
        self._mark_sent(conn, first)
        second = report.generate(conn, on_day=date(2026, 10, 16), write_file=False)
        assert [p["id"] for p in second.sections["confirmed_live"]] == [pid], "默认每天重复"

    def test_with_the_flag_off_it_is_delivered_once_per_state(self, conn, monkeypatch):
        monkeypatch.setenv("REPORT_REPEAT_LIVE_LIST", "false")
        config.get_settings.cache_clear()
        pid = self._confirmed(conn)
        first = report.generate(conn, on_day=date(2026, 10, 15), write_file=False)
        assert [p["id"] for p in first.sections["confirmed_live"]] == [pid]
        self._mark_sent(conn, first)
        second = report.generate(conn, on_day=date(2026, 10, 16), write_file=False)
        assert second.sections["confirmed_live"] == [], "同一状态已投递过，不再占版面"

    def test_a_state_change_brings_it_back_even_with_the_flag_off(self, conn, monkeypatch):
        monkeypatch.setenv("REPORT_REPEAT_LIVE_LIST", "false")
        config.get_settings.cache_clear()
        pid = self._confirmed(conn)
        first = report.generate(conn, on_day=date(2026, 10, 15), write_file=False)
        self._mark_sent(conn, first)
        conn.execute("UPDATE perks SET ends_at = '2026-11-20' WHERE id = ?", (pid,))
        conn.commit()
        second = report.generate(conn, on_day=date(2026, 10, 16), write_file=False)
        assert [p["id"] for p in second.sections["confirmed_live"]] == [pid], "规则变了就值得再报"


class TestEmailMessage:
    def make_report(self, conn):
        add_perk(conn, perk(claim="https://cursor.com/x", ends="2026-12-31"))
        return report.generate(conn)

    def test_message_is_multipart_mixed_with_an_md_attachment(self, conn):
        generated = self.make_report(conn)
        message = mail.build_message(generated, to=["me@gmail.com"], sender="bot@gmail.com")
        assert message.get_content_type() == "multipart/mixed"
        parts = list(message.iter_attachments())
        assert len(parts) == 1
        attachment = parts[0]
        assert attachment.get_content_type() == "text/markdown"
        assert attachment.get_filename() == f"perk-report-{generated.day}.md"
        assert attachment["Content-Disposition"].startswith("attachment")

    def test_attachment_content_round_trips(self, conn):
        generated = self.make_report(conn)
        message = mail.build_message(generated, to=["me@gmail.com"], sender="bot@gmail.com")
        attachment = list(message.iter_attachments())[0]
        payload = attachment.get_payload(decode=True).decode("utf-8")
        assert payload == generated.markdown

    def test_body_offers_both_plain_markdown_and_html(self, conn):
        generated = self.make_report(conn)
        message = mail.build_message(generated, to=["me@gmail.com"], sender="bot@gmail.com")
        kinds = [p.get_content_type() for p in message.walk() if not p.is_multipart()]
        assert "text/plain" in kinds and "text/html" in kinds and "text/markdown" in kinds

    def test_headers(self, conn):
        generated = self.make_report(conn)
        message = mail.build_message(generated, to=["a@gmail.com", "b@gmail.com"],
                                     sender="bot@gmail.com", subject="测试日报")
        assert message["Subject"] == "测试日报"
        assert message["From"] == "bot@gmail.com"
        assert set(message["To"].split(", ")) == {"a@gmail.com", "b@gmail.com"}
        assert message["Message-ID"] and message["Date"]

    def test_default_subject_carries_the_day(self, conn):
        generated = self.make_report(conn)
        message = mail.build_message(generated, to=["me@gmail.com"], sender="me@gmail.com")
        assert generated.day in message["Subject"]

    def test_as_bytes_is_ascii_safe_for_smtp(self, conn):
        generated = self.make_report(conn)
        message = mail.build_message(generated, to=["me@gmail.com"], sender="me@gmail.com")
        raw = message.as_bytes()
        assert b"Content-Type: multipart/mixed" in raw
        assert raw.count(b"perk-report-") >= 1


class TestSendReportWiring:
    @pytest.fixture(autouse=True)
    def _mail_settings(self, test_env, monkeypatch):
        """Depends on test_env so these values are applied after the fixture's blanks."""
        monkeypatch.setenv("REPORT_TO", "me@gmail.com")
        monkeypatch.setenv("SMTP_USERNAME", "bot@gmail.com")
        monkeypatch.setenv("SMTP_PASSWORD", "app-password")
        monkeypatch.setenv("SMTP_FROM", "bot@gmail.com")
        config.get_settings.cache_clear()
        yield
        config.get_settings.cache_clear()

    def test_recipients_come_from_report_to(self):
        assert config.get_settings().recipients() == ["me@gmail.com"]
        assert config.get_settings().mail_enabled is True

    def test_multiple_recipients_are_split_from_one_env_value(self, monkeypatch):
        monkeypatch.setenv("REPORT_TO", "a@gmail.com, b@gmail.com")
        config.get_settings.cache_clear()
        assert config.get_settings().recipients() == ["a@gmail.com", "b@gmail.com"]

    def test_missing_recipients_fails_before_any_connection(self, monkeypatch):
        monkeypatch.setenv("REPORT_TO", "")
        config.get_settings.cache_clear()
        generated = report.Report(day="2026-10-15", generated_at="x", markdown="a", html="b")
        outcome = mail.send_report(generated)
        assert outcome.ok is False and "收件人" in outcome.error

    def test_missing_credentials_are_reported_not_raised(self, monkeypatch, conn):
        monkeypatch.setenv("SMTP_PASSWORD", "")
        config.get_settings.cache_clear()
        generated = self._make(conn)
        outcome = mail.send_report(generated)
        assert outcome.ok is False
        assert "SMTP" in outcome.error

    def _make(self, conn):
        add_perk(conn, perk(claim="https://cursor.com/x"))
        return report.generate(conn)

    def test_ssl_path_logs_in_and_sends_exactly_one_message(self, monkeypatch, conn):
        calls = []

        class FakeServer:
            def __init__(self, host, port, context=None, timeout=None):
                calls.append(("connect", host, port))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, user, password):
                calls.append(("login", user, password))

            def send_message(self, message):
                calls.append(("send", message.get_content_type(),
                              message["To"], message["Subject"]))

        monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeServer)
        generated = self._make(conn)
        outcome = mail.send_report(generated)
        assert outcome.ok is True and outcome.dry_run is False
        assert ("login", "bot@gmail.com", "app-password") in calls
        send = [c for c in calls if c[0] == "send"]
        assert len(send) == 1
        assert send[0][1] == "multipart/mixed"
        assert "me@gmail.com" in send[0][2]

    def test_starttls_path_is_used_when_ssl_disabled(self, monkeypatch, conn):
        calls = []

        class FakeServer:
            def __init__(self, host, port, timeout=None):
                calls.append(("connect", host, port))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def ehlo(self):
                calls.append(("ehlo",))

            def starttls(self, context=None):
                calls.append(("starttls",))

            def login(self, user, password):
                calls.append(("login", user, password))

            def send_message(self, message):
                calls.append(("send",))

        monkeypatch.setenv("SMTP_USE_SSL", "false")
        monkeypatch.setenv("SMTP_PORT", "587")
        config.get_settings.cache_clear()
        monkeypatch.setattr(mail.smtplib, "SMTP", FakeServer)
        outcome = mail.send_report(self._make(conn))
        assert outcome.ok is True
        assert [c[0] for c in calls] == ["connect", "ehlo", "starttls", "ehlo", "login", "send"]

    def test_server_failure_becomes_a_result_not_an_exception(self, monkeypatch, conn):
        class Boom:
            def __init__(self, *a, **k):
                raise OSError("connection refused")

        monkeypatch.setattr(mail.smtplib, "SMTP_SSL", Boom)
        outcome = mail.send_report(self._make(conn))
        assert outcome.ok is False
        assert "OSError" in outcome.error

    def test_dry_run_never_touches_the_network(self, monkeypatch, conn):
        def forbid(*a, **k):
            raise AssertionError("dry run must not connect")

        monkeypatch.setattr(mail.smtplib, "SMTP_SSL", forbid)
        monkeypatch.setattr(mail.smtplib, "SMTP", forbid)
        outcome = mail.send_report(self._make(conn), dry_run=True)
        assert outcome.ok is True and outcome.dry_run is True
        assert outcome.bytes > 0 and outcome.attachment.endswith(".md")


class TestProductGrouping:
    """一个产品的免费档 / 学生价 / 兑换码是分别入库的，但它们回答的是同一个问题：
    「这家给我什么」。平铺会让读者在一封邮件里自己把它们拼回去。"""

    def _items(self, *specs):
        return [
            {"id": i, "product": p, "product_norm": n, "benefit_label": b,
             "days_left": d, "countdown": None, "urgent": False}
            for i, (p, n, b, d) in enumerate(specs, start=1)
        ]

    def test_same_product_collapses_into_one_group(self):
        groups = report.group_by_product(self._items(
            ("GitHub Copilot", "githubcopilot", "免费会员", None),
            ("GitHub Copilot", "githubcopilot", "学生优惠", 1),
            ("Cursor", "cursor", "免费额度", 5),
        ))
        assert [g["product"] for g in groups] == ["GitHub Copilot", "Cursor"]
        assert [g["count"] for g in groups] == [2, 1]
        assert [p["benefit_label"] for p in groups[0]["perks"]] == ["免费会员", "学生优惠"]

    def test_group_order_follows_first_appearance(self):
        """板块内部自带排序（即将截止按剩余天数），分组不能把它打乱。"""
        groups = report.group_by_product(self._items(
            ("A 家", "a", "免费额度", 1),
            ("B 家", "b", "免费会员", 2),
            ("A 家", "a", "兑换码", 3),
        ))
        assert [g["product"] for g in groups] == ["A 家", "B 家"]

    def test_markdown_renders_one_product_heading_per_group(self, conn):
        add_perk(conn, perk(product="GitHub Copilot", benefit="free_membership",
                            entitlement="教师免费用 Pro", claim="https://github.com/features/copilot"))
        add_perk(conn, perk(product="GitHub Copilot", benefit="student_discount",
                            entitlement="学生免费", claim="https://github.com/students"))
        generated = report.generate(conn, on_day=date(2026, 10, 15), write_file=False)
        section = generated.markdown.split("## 新发现的候选福利")[1]
        assert section.count("### GitHub Copilot（2 项）") == 1
        assert "#### 免费会员 1" in section
        assert "#### 学生优惠 2" in section
        assert "个产品）" in generated.markdown

    def test_single_perk_product_has_no_index_suffix(self, conn):
        add_perk(conn, perk(product="Cursor", entitlement="100 次免费补全",
                            claim="https://cursor.com/x"))
        generated = report.generate(conn, on_day=date(2026, 10, 15), write_file=False)
        assert "### Cursor\n" in generated.markdown
        assert "#### 免费额度 1" not in generated.markdown

    def test_html_groups_items_under_the_product_heading(self, conn):
        add_perk(conn, perk(product="GitHub Copilot", benefit="free_membership",
                            entitlement="教师免费用 Pro", claim="https://github.com/features/copilot"))
        add_perk(conn, perk(product="GitHub Copilot", benefit="student_discount",
                            entitlement="学生免费", claim="https://github.com/students"))
        generated = report.generate(conn, on_day=date(2026, 10, 15), write_file=False)
        html = generated.html
        assert html.count("GitHub Copilot") == 1, "产品标题在板块里只出现一次"
        assert ">GitHub Copilot <span" in html
        assert "· 2 项" in html
        section = html[html.index("GitHub Copilot"):]
        assert "免费会员 1" in section and "学生优惠 2" in section

    def test_deadline_urgent_wording_reaches_both_renditions(self, conn):
        today = date(2026, 10, 15)
        add_perk(conn, perk(product="Kimi", ends="2026-10-15", entitlement="今天截止的额度",
                            claim="https://kimi.com/x"))
        generated = report.generate(conn, on_day=today, write_file=False)
        assert "今天截止" in generated.markdown
        assert "**今天截止**" in generated.markdown, "Markdown 里紧迫项要加粗"
        assert "今天截止" in generated.html


class TestOnePerkPerReport:
    """六个板块是同一批福利的六个视角，不是六批福利。

    一条缺链接的新福利同时满足「新发现」与「需人工判断」，实测 28 条福利被渲染了
    59 次 —— 用户收到的第一封邮件因此满是重复。每条福利在一封日报里只能出现一次。
    """

    def _item(self, pid, product="Cursor"):
        return {"id": pid, "product": product, "product_norm": product.lower(),
                "benefit_label": "免费额度", "days_left": None, "countdown": "未注明截止日",
                "urgent": False, "entitlement": "100 次", "sources": [], "confidence_pct": 70}

    def _six(self, **by_key):
        return {key: by_key.get(key, []) for key, _title in report.SECTIONS}

    def test_a_perk_shows_up_once_even_when_it_qualifies_everywhere(self):
        item = self._item(1)
        sections, dropped = report.assign_sections(self._six(
            new_found=[item], needs_review=[item], rule_updates=[item]))
        assert sum(len(v) for v in sections.values()) == 1
        assert sections["new_found"] == [item]
        assert sorted(dropped[1]) == ["needs_review", "rule_updates"]

    def test_time_critical_wins_over_new(self):
        item = self._item(1)
        sections, _dropped = report.assign_sections(self._six(
            new_found=[item], expiring_soon=[item]))
        assert sections["expiring_soon"] == [item]
        assert sections["new_found"] == []

    def test_carryover_review_items_stay_visible_after_they_stop_being_new(self):
        """去重要移走条目，但不能让「一直没解决」的事从此消失。"""
        old = self._item(2)
        sections, dropped = report.assign_sections(self._six(needs_review=[old]))
        assert sections["needs_review"] == [old]
        assert dropped == {}

    def test_an_unresolved_perk_keeps_coming_back_in_later_reports(self, conn):
        """此前 needs_review 板块也按 updated_at > since 过滤：一条 pending、没有截止
        日期、之后又没人动过的福利只在它第一次被看见的那封日报里出现过一次，之后
        flag_incomplete 不会再推它的 updated_at，它就永久消失了。"""
        from bp import lifecycle

        pid = add_perk(conn, perk(entitlement="缺链接的一条", claim=None))
        lifecycle.flag_incomplete(conn)
        conn.commit()

        first = report.generate(conn, on_day=date(2026, 10, 15), write_file=False)
        assert pid in [p["id"] for p in first.sections["new_found"]], "第一次应当作为新发现出现"
        conn.execute("UPDATE reports SET sent_at = ? WHERE day = ?",
                     (first.generated_at, first.day))
        conn.commit()

        second = report.generate(conn, on_day=date(2026, 10, 16), write_file=False)
        ids = [p["id"] for p in second.sections["needs_review"]]
        assert pid in ids, "还没解决的事不能从日报里消失"

        third = report.generate(conn, on_day=date(2026, 10, 17), write_file=False)
        assert pid in [p["id"] for p in third.sections["needs_review"]]

    def test_once_the_user_marks_it_claimed_it_stops_coming_back(self, conn):
        """标记已领取是用户让它从日报里退场的唯一出口。"""
        from bp import lifecycle

        pid = add_perk(conn, perk(entitlement="缺链接的一条", claim=None))
        lifecycle.flag_incomplete(conn)
        conn.commit()
        first = report.generate(conn, on_day=date(2026, 10, 15), write_file=False)
        conn.execute("UPDATE reports SET sent_at = ? WHERE day = ?",
                     (first.generated_at, first.day))
        lifecycle.mark_claimed(conn, pid, True)
        conn.commit()
        second = report.generate(conn, on_day=date(2026, 10, 16), write_file=False)
        assert pid not in [p["id"] for p in second.sections["needs_review"]]

    def test_counts_and_total_reflect_the_deduplicated_lists(self, conn):
        pid = add_perk(conn, perk(product="Cursor", entitlement="100 次免费补全",
                                  claim="https://cursor.com/x"))
        generated = report.generate(conn, on_day=date(2026, 10, 15), write_file=False)
        assert sum(generated.stats["counts"].values()) == generated.total_items
        assert generated.stats["counts"]["needs_review"] == 0
        assert generated.stats["counts"]["new_found"] == 1
        assert [p["id"] for p in generated.sections["new_found"]] == [pid]

    def test_the_mail_explains_the_fold_so_counts_are_not_misread(self, conn):
        both = perk(product="Cursor", entitlement="100 次免费补全", claim="https://cursor.com/x")
        add_perk(conn, both)
        add_perk(conn, perk(product="Kimi", entitlement="明天就截止的额度",
                            claim="https://kimi.com/x", ends="2026-10-16"))
        # Kimi 同时属于新发现与即将截止，Cursor 只属于新发现
        generated = report.generate(conn, on_day=date(2026, 10, 15), write_file=False)
        assert "另有 1 条同时符合多个板块" in generated.markdown
        assert "另有 <b>1</b> 条同时符合多个板块" in generated.html

    def test_no_note_when_nothing_was_folded(self, conn):
        add_perk(conn, perk(product="Cursor", claim="https://cursor.com/x"))
        generated = report.generate(conn, on_day=date(2026, 10, 15), write_file=False)
        assert "同时符合多个板块" not in generated.markdown
