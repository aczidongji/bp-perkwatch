from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

from . import db, lifecycle, pipeline, query, store, timeutil
from .config import PROJECT_ROOT, Settings, get_settings
from .models import extraction_json_schema
from .sources import SourceSpec, add_source, load_seed_file, remove_source, seed, set_enabled
from .sources import list_sources, mark_failure, mark_success

STATUS_WORDS = {"pending": "待核实", "confirmed": "已确认", "expired": "已过期",
                "invalid": "无效", "dubious": "存疑"}


def _out(*lines: str) -> None:
    print("\n".join(lines))


def cmd_init(args: argparse.Namespace) -> int:
    env_file = PROJECT_ROOT / ".env"
    example = PROJECT_ROOT / ".env.example"
    if not env_file.exists() and example.exists() and not args.no_env:
        shutil.copyfile(example, env_file)
        _out(f"已创建 {env_file}（不含任何真实密钥）")
        _out("请至少填写 DEEPSEEK_API_KEY、SMTP_PASSWORD 后再运行采集。")

    applied = db.init_db()
    _out("数据库迁移:", ", ".join(applied) if applied else "已是最新")

    with db.session() as conn:
        result = seed(conn)
        _out(f"来源清单: 共 {result['total']} 条，新增 {result['added']} 条")
    _out("初始化完成。下一步: uv run bp run   # 单次采集+分析+核验")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    with db.session() as conn:
        summary = pipeline.run_once(
            conn,
            do_analysis=not args.collect_only,
            do_verify=not args.skip_verify,
            limit=args.limit,
            force_analysis=args.force_analysis,
            crawl_web=not args.no_crawl,
        )
    _print_counts(summary)
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    with db.session() as conn:
        counts = pipeline.collect(conn, limit_sources=args.limit, crawl_web=not args.no_crawl)
        expired = lifecycle.sweep_expired(conn)
        lifecycle.flag_incomplete(conn)
    _print_counts(counts.as_dict())
    _out(f"过期关闭: {len(expired)}")
    return 0


def cmd_analyse(args: argparse.Namespace) -> int:
    with db.session() as conn:
        counts = pipeline.analyse_pending(conn, limit=args.limit, force=args.force)
        # bp run does this after analysis; doing it here too means a manual
        # "analyse then look at the web page" session shows the same flags as the timer.
        lifecycle.flag_incomplete(conn)
    _out(f"送模型分析 {counts.analysed}，免费预筛跳过 {counts.prefiltered}，"
         f"新福利 {counts.perks_created}，规则更新 {counts.perks_updated}，"
         f"失败 {counts.analysis_failed}")
    if counts.perks_rejected:
        _out(f"证据不足被拒 {counts.perks_rejected} 条（产品名或权益在原文里找不到逐字引文）")
    if counts.perks_unactionable:
        _out(f"无可执行条款被拒 {counts.perks_unactionable} 条"
             f"（既没有领取入口/时间窗/额度，也没写怎么拿到，多半是产品发布稿）")
    _out(f"token: {counts.model_tokens}")
    for note in counts.notes:
        _out(f"注意: {note}")
    if counts.stopped_early:
        _out("注意: 已进入 DeepSeek 高峰窗口，分析提前停止（--force 可忽略）")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    with db.session() as conn:
        done = pipeline.verify_new(conn, limit=args.limit)
        lifecycle.flag_incomplete(conn)
        silent = conn.execute(
            "SELECT COUNT(*) AS n FROM perks WHERE status IN ('pending', 'dubious') "
            "AND claim_url IS NULL AND rules_url IS NULL"
        ).fetchone()["n"]
    _out(f"核验 {done} 条福利")
    if silent:
        _out(f"其中 {silent} 条连一个链接都没有，自动核验对它们无事可做——"
             f"这类只会在网页上标着「缺少领取入口链接」，需要你人工确认")
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    """按当前 config/keywords.toml 重新判断库里已有的福利。

    闸门词表是可以改的，改完之后之前入库的旧条目不会自动重新评估——而
    needs_review 板块现在会持续列出，所以一条被误判成福利的套餐说明会天天回来。
    """
    with db.session() as conn:
        result = pipeline.apply_promotion_gate(conn, limit=args.limit, dry_run=args.dry_run)
    _out(f"复核 {result['checked']} 条，判为非促销 {result['gated']} 条"
         + ("（dry-run，未改动）" if args.dry_run else ""))
    for item in result["items"][: args.show]:
        _out(f"  #{item['id']} {str(item['product'])[:26]:<28} {str(item['entitlement'] or '')[:44]}")
    rest = result["gated"] - len(result["items"][: args.show])
    if rest > 0:
        _out(f"  …另有 {rest} 条（--show 调整显示条数）")
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    from .scheduler import tick
    with db.session() as conn:
        result = tick(conn, dry_run=args.dry_run, force_analysis=True if args.force else None)
    _out(str(result))
    for key, value in result.detail.items():
        _out(f"{key}: {value}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .report import generate, record_dispatched, record_send_failure
    from .notify.email import send_report
    with db.session() as conn:
        generated = generate(conn, on_day=date.fromisoformat(args.day) if args.day else None)
        _out(f"日报已生成: {generated.path}")
        _out(f"条目统计: " + ", ".join(f"{k}={len(v)}" for k, v in generated.sections.items()))
        if args.stdout:
            print(generated.markdown)
        if args.send:
            outcome = send_report(generated, dry_run=args.dry_run_send)
            if outcome.ok:
                conn.execute("UPDATE reports SET sent_at = ? WHERE day = ?",
                             (timeutil.iso(timeutil.now_utc()), generated.day))
                # 与定时器走同一条落库路径，否则命令行发出的日报不会被去重记住。
                record_dispatched(conn, generated)
                _out(f"已发送 -> {', '.join(outcome.recipients)}  "
                     f"附件 {outcome.attachment}  {outcome.bytes // 1024} KB"
                     + ("  [dry-run，未真正投递]" if outcome.dry_run else ""))
            else:
                record_send_failure(conn, generated.day, outcome.error)
                _out(f"发送失败: {outcome.error}")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    with db.session() as conn:
        if args.sources_command == "list":
            rows = list_sources(conn, enabled_only=False)
            _out(f"{'ID':>4} {'状态':<4} {'类型':<7} {'最近成功':<20} 名称 / URL")
            for row in rows:
                flag = "启用" if row["enabled"] else "停用"
                error = f"  ! {row['last_error'][:60]}" if row["last_error"] else ""
                _out(f"{row['id']:>4} {flag:<4} {row['kind']:<7} "
                     f"{(row['last_success_at'] or '-'):<20} {row['name']}{error}")
                _out(f"{'':>33}{row['url']}")
        elif args.sources_command == "add":
            spec = SourceSpec(name=args.name or args.url, kind=args.kind, url=args.url,
                              product_hint=args.product_hint)
            sid = add_source(conn, spec, enabled=not args.disabled)
            _out(f"已添加来源 #{sid}: {spec.kind} {spec.url}")
        elif args.sources_command == "enable":
            _out("已启用" if set_enabled(conn, args.source_id, True) else "未找到该来源")
        elif args.sources_command == "disable":
            _out("已停用" if set_enabled(conn, args.source_id, False) else "未找到该来源")
        elif args.sources_command == "remove":
            _out("已删除" if remove_source(conn, args.source_id) else "未找到该来源")
        elif args.sources_command == "check":
            return _check_sources(conn, source_id=args.source_id,
                                  include_disabled=args.include_disabled)
    return 0


def _check_sources(conn, *, source_id: int | None = None,
                   include_disabled: bool = False) -> int:
    """Actually fetch every enabled source and report what came back. This is how the
    seed list is validated rather than assumed."""
    from .fetch import Fetcher
    from .collect import fetch_rss, fetch_article, discover

    rows = list_sources(conn, enabled_only=not include_disabled)
    if source_id is not None:
        rows = [row for row in rows if row["id"] == source_id]
        if not rows:
            _out(f"未找到来源 #{source_id}（可能已停用，加 --include-disabled）")
            return 1
    bad = 0
    with Fetcher() as fetcher:
        for row in rows:
            try:
                if row["kind"] == "rss":
                    items, error, _ = fetch_rss(row["url"], fetcher, max_items=5)
                    detail = f"{len(items)} 条条目" + (f"; 首条: {(items[0].title or '')[:40]}" if items else "")
                    if error:
                        raise RuntimeError(error)
                elif row["kind"] == "web":
                    page = fetch_article(row["url"], fetcher)
                    if not page.usable:
                        raise RuntimeError(page.error or "no content")
                    detail = f"{len(page.content or '')} 字符正文"
                else:
                    hits, error = discover(row["url"], fetcher, max_results=5)
                    if error:
                        raise RuntimeError(error)
                    detail = f"{len(hits)} 条搜索结果"
                mark_success(conn, row["id"])
                _out(f"OK   #{row['id']:<4} {row['name'][:30]:<30} {detail}")
            except Exception as exc:  # noqa: BLE001
                bad += 1
                mark_failure(conn, row["id"], str(exc))
                _out(f"FAIL #{row['id']:<4} {row['name'][:30]:<30} {str(exc)[:90]}")
            conn.commit()
    _out(f"\n可用 {len(rows) - bad} / {len(rows)}")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    with db.session() as conn:
        items = query.search_perks(
            conn, text=args.text, product=args.product, benefit_type=args.type,
            statuses=args.status or None, only_unclaimed=args.unclaimed,
            only_expiring=args.expiring, sort=args.sort, limit=args.limit,
        )
        if not items:
            _out("没有匹配的福利")
            return 0
        _out(f"{'ID':>4} {'状态':<4} {'剩余':>5} {'可信':>5}  {'产品':<22} {'类型':<7} 截止日期")
        for item in items:
            left = f"{item['days_left']}d" if item["days_left"] is not None else "-"
            _out(f"{item['id']:>4} {STATUS_WORDS.get(item['status'], item['status']):<4} "
                 f"{left:>5} {int((item['confidence'] or 0) * 100):>4}%  "
                 f"{(item['product'] or '')[:22]:<22} {(item['benefit_label'] or ''):<7} "
                 f"{item['ends_at'] or '未注明'}")
            _out(f"{'':>30}{item['entitlement'] or ''}"
                 + ("  [待核实]" if item["needs_review"] else ""))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM perks WHERE id = ?", (args.perk_id,)).fetchone()
        if row is None:
            _out("未找到该福利")
            return 1
        item = query.hydrate(conn, [row])[0]
        _out(json.dumps(item, ensure_ascii=False, indent=2, default=str))
        _out("\n== 来源 ==")
        for src in query.sources_for(conn, args.perk_id):
            _out(f"- [{src['role']}] {src['source_name'] or '未知来源'}: {src['title'] or ''}\n  {src['url']}")
        if args.history:
            _out("\n== 历史 ==")
            _out(json.dumps(query.history(conn, args.perk_id), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    with db.session() as conn:
        ok = lifecycle.mark_claimed(conn, args.perk_id, not args.unclaim, note=args.note)
        _out("已标记为已领取" if ok and not args.unclaim else ("已取消标记" if ok else "未找到该福利"))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Manual status change. Going to 已确认 still has to pass the same guard as
    automation, because the database trigger does not care who asked."""
    with db.session() as conn:
        row = conn.execute("SELECT status, verification_id FROM perks WHERE id = ?", (args.perk_id,)).fetchone()
        if row is None:
            _out("未找到该福利")
            return 1
        if args.status == "confirmed":
            best = conn.execute(
                "SELECT id FROM verification_runs WHERE perk_id = ? AND official_domain_match = 1 "
                "AND verdict = 'available' AND checked_at >= datetime('now', ?) "
                "ORDER BY checked_at DESC LIMIT 1",
                (args.perk_id, f"-{get_settings().verification_max_age_days} days"),
            ).fetchone()
            if best is None:
                _out("拒绝：没有新鲜的官方域名核验记录。请先执行 bp verify，或人工核验后用 --status dubious/invalid。")
                return 2
            conn.execute("UPDATE perks SET verification_id = ? WHERE id = ?", (best["id"], args.perk_id))
        changed = store.set_status(conn, args.perk_id, args.status, args.reason or "人工修改", "human")
        _out(f"状态 {STATUS_WORDS.get(row['status'])} -> {STATUS_WORDS.get(args.status)}"
             if changed else "状态未变化")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    with db.session() as conn:
        items = query.search_perks(
            conn, text=args.text, product=args.product, benefit_type=args.type,
            statuses=args.status or None, sort=args.sort, limit=args.limit,
        )
        if args.format == "csv":
            payload = query.to_csv(conn, items, english=args.english_headers)
        else:
            payload = query.to_json(conn, items)

    destination = Path(args.output) if args.output else None
    if destination:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")
        _out(f"已导出 {len(items)} 条到 {destination}")
    else:
        print(payload)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with db.session() as conn:
        stats = query.counts(conn)
        last = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        reports = conn.execute("SELECT * FROM reports ORDER BY day DESC LIMIT 5").fetchall()
    _out(json.dumps({"stats": stats,
                     "last_run": dict(last) if last else None,
                     "recent_reports": [dict(r) for r in reports]},
                    ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Self-check everything that does not need a live model or mail server."""
    settings = get_settings()
    _out(f"Python {sys.version.split()[0]}  项目根 {PROJECT_ROOT}")
    _out(f"数据库 {settings.db_path} 存在={settings.db_path.exists()}")
    _out(f"DeepSeek key={'已配置' if settings.deepseek_api_key else '缺失'} "
         f"model={settings.deepseek_model} base={settings.deepseek_base_url}")
    _out(f"SMTP {settings.smtp_host}:{settings.smtp_port} user={settings.smtp_username or '缺失'} "
         f"password={'已配置' if settings.smtp_password else '缺失'} "
         f"收件人={settings.recipients() or '缺失'}")
    schema = extraction_json_schema()
    _out(f"JSON Schema 字段数={len(schema['properties']['perks']['items']['required'])}")
    _out(f"低谷判定现在={'高峰' if timeutil.is_deepseek_peak(timeutil.now_utc()) else '低谷'} "
         f"(北京时间 {timeutil.bj_dt(timeutil.now_utc()):%H:%M})")
    with db.session() as conn:
        _out("表: " + ", ".join(r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")))
    return 0


def cmd_mail_test(args: argparse.Namespace) -> int:
    from .notify.email import build_message, send_report
    from .report import Report
    settings = get_settings()
    fake = Report(day=date.today().isoformat(), generated_at=timeutil.iso(timeutil.now_utc()),
                  markdown="# 测试\n\n这是一封来自 bp-perkwatch 的测试邮件，用于确认 SMTP 与 .md 附件通道。\n",
                  html="<h1>测试</h1><p>这是一封来自 bp-perkwatch 的测试邮件。</p>",
                  sections={}, stats={})
    message = build_message(fake, to=settings.recipients(),
                           sender=settings.smtp_from or settings.smtp_username)
    _out(f"主题: {message['Subject']}")
    for part in message.walk():
        # Inline parts have no Content-Disposition at all, and None cannot be formatted.
        _out(f"  {part.get_content_type():<24} "
             f"disposition={str(part.get_content_disposition() or '-'):<9} "
             f"filename={part.get_filename() or '-'}")
    outcome = send_report(fake)
    _out("发送结果:", "成功" if outcome.ok else f"失败 -> {outcome.error}")
    return 0 if outcome.ok else 1


def _print_counts(summary: dict) -> int | None:
    _out(json.dumps(summary, ensure_ascii=False, indent=2))
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bp", description="本地 AI 福利监控系统")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="首次初始化：建库、迁移、导入来源清单")
    p.add_argument("--no-env", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("run", help="单次完整流程：采集 + 分析 + 核验")
    p.add_argument("--limit", type=int)
    p.add_argument("--collect-only", action="store_true")
    p.add_argument("--skip-verify", action="store_true")
    p.add_argument("--no-crawl", action="store_true", help="搜索命中只入库不抓正文")
    p.add_argument("--force-analysis", action="store_true", help="忽略低谷限制")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("collect", help="只采集，不调用模型")
    p.add_argument("--limit", type=int)
    p.add_argument("--no-crawl", action="store_true")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("analyse", help="只分析待处理文章")
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_analyse)

    p = sub.add_parser("verify", help="对未核验的福利做官方核验")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("gate", help="按当前关键词表重新判断库里已有的福利")
    p.add_argument("--limit", type=int)
    p.add_argument("--dry-run", action="store_true", help="只看会判掉哪些，不改数据")
    p.add_argument("--show", type=int, default=20, help="打印前 N 条被判掉的")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("tick", help="定时器入口：按状态机决定该做什么")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_tick)

    p = sub.add_parser("report", help="生成/发送日报")
    p.add_argument("--day")
    p.add_argument("--stdout", action="store_true")
    p.add_argument("--send", action="store_true")
    p.add_argument("--dry-run-send", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("sources", help="管理信息源")
    p.add_argument("sources_command", choices=["list", "add", "enable", "disable", "remove", "check"])
    p.add_argument("source_id", nargs="?", type=int)
    p.add_argument("--url")
    p.add_argument("--name")
    p.add_argument("--kind", default="rss", choices=["rss", "web", "search"])
    p.add_argument("--product-hint")
    p.add_argument("--disabled", action="store_true")
    p.add_argument("--include-disabled", action="store_true")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("ls", help="查询福利")
    p.add_argument("--text")
    p.add_argument("--product")
    p.add_argument("--type", choices=list(query.BENEFIT_LABELS))
    p.add_argument("--status", nargs="*", choices=list(STATUS_WORDS))
    p.add_argument("--sort", default="ends", choices=list(query.SORTS))
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--unclaimed", action="store_true")
    p.add_argument("--expiring", action="store_true")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("show", help="查看一条福利的完整信息")
    p.add_argument("perk_id", type=int)
    p.add_argument("--history", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("mark", help="标记已领取")
    p.add_argument("perk_id", type=int)
    p.add_argument("--unclaim", action="store_true")
    p.add_argument("--note")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("status", help="人工修改状态")
    p.add_argument("perk_id", type=int)
    p.add_argument("status", choices=list(STATUS_WORDS))
    p.add_argument("--reason")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("export", help="导出 CSV / JSON")
    p.add_argument("--format", default="csv", choices=["csv", "json"])
    p.add_argument("--output")
    p.add_argument("--text")
    p.add_argument("--product")
    p.add_argument("--type", choices=list(query.BENEFIT_LABELS))
    p.add_argument("--status", nargs="*", choices=list(STATUS_WORDS))
    p.add_argument("--sort", default="ends", choices=list(query.SORTS))
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--english-headers", action="store_false", default=False)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("stats", help="统计与最近运行状态")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("doctor", help="自检：配置、schema、低谷窗口")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("mail-test", help="发一封带 .md 附件的测试邮件")
    p.set_defaults(func=cmd_mail_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        _out("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
