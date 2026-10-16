from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from . import db, lifecycle, store, timeutil, verify
from .collect import RssItem, discover, fetch_article, fetch_rss
from .config import get_settings
from .fetch import Fetcher
from .llm import Extractor, PerkOutcome, off_peak_window_closed
from .models import PerkFields
from .prefilter import looks_like_promotion, score as prefilter_score
from .sources import list_sources, mark_failure, mark_success

COUNT_FIELDS = (
    "sources_ok", "sources_failed", "items_seen", "articles_new", "articles_revision",
    "articles_duplicate", "articles_alias", "prefiltered", "analysed",
    "analysis_failed", "perks_created", "perks_updated", "perks_rejected",
    "perks_unactionable", "verified", "expired",
)


@dataclass
class RunCounts:
    sources_ok: int = 0
    sources_failed: int = 0
    items_seen: int = 0
    articles_new: int = 0
    articles_revision: int = 0
    articles_duplicate: int = 0
    articles_alias: int = 0
    prefiltered: int = 0
    analysed: int = 0
    analysis_failed: int = 0
    perks_created: int = 0
    perks_updated: int = 0
    perks_rejected: int = 0
    perks_unactionable: int = 0
    verified: int = 0
    expired: int = 0
    stopped_early: bool = False
    notes: list[str] = field(default_factory=list)
    model_tokens: dict[str, int] = field(default_factory=lambda: {"prompt": 0, "completion": 0, "cached": 0})

    def merge(self, other: "RunCounts") -> None:
        for name in COUNT_FIELDS:
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for note in other.notes:
            if note not in self.notes:
                self.notes.append(note)
        for key, value in other.model_tokens.items():
            self.model_tokens[key] = self.model_tokens.get(key, 0) + value

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {name: getattr(self, name) for name in COUNT_FIELDS}
        out["stopped_early"] = self.stopped_early
        out["notes"] = self.notes
        out["model_tokens"] = self.model_tokens
        return out


def _record_rss_item(conn: sqlite3.Connection, item: RssItem, source_id: int,
                     fetcher: Fetcher, counts: RunCounts) -> None:
    """Prefer the feed body; crawl the article URL when the feed only carried a
    summary. A failed body extraction still stores the summary together with an
    explicit error, so nothing is lost silently."""
    content = item.content or item.summary or ""
    extract_error = None

    if len(content) < 400:
        page = fetch_article(item.url, fetcher)
        if page.content and len(page.content) > len(content):
            content = page.content
            extract_error = page.error
        elif page.error:
            extract_error = f"{page.error}; kept feed summary instead"

    write = store.record_article(
        conn,
        url=item.url,
        title=item.title,
        content=content or None,
        summary=item.summary,
        published_at=item.published_at,
        source_id=source_id,
        author=item.author,
        extract_error=extract_error,
    )
    setattr(counts, f"articles_{write.action}", getattr(counts, f"articles_{write.action}") + 1)


def collect(conn: sqlite3.Connection, *, limit_sources: int | None = None,
            include_disabled: bool = False, crawl_web: bool = True) -> RunCounts:
    """Poll every enabled source.

    Each source is isolated: any exception is recorded against that source and the
    loop moves on, so one dead feed can never end the run.
    """
    settings = get_settings()
    counts = RunCounts()
    sources = list_sources(conn, enabled_only=not include_disabled)
    if limit_sources:
        sources = sources[:limit_sources]

    with Fetcher() as fetcher:
        for source in sources:
            try:
                if source["kind"] == "rss":
                    items, error, headers = fetch_rss(
                        source["url"], fetcher, max_items=settings.max_items_per_source)
                    if error:
                        raise RuntimeError(error)
                    for item in items:
                        counts.items_seen += 1
                        _record_rss_item(conn, item, source["id"], fetcher, counts)
                    mark_success(conn, source["id"],
                                 headers.get("etag") or headers.get("ETag"),
                                 headers.get("last-modified") or headers.get("Last-Modified"))
                elif source["kind"] == "web":
                    counts.items_seen += 1
                    page = fetch_article(source["url"], fetcher)
                    write = store.record_article(
                        conn, url=page.url, title=page.title, content=page.content,
                        summary=page.summary, published_at=page.published_at,
                        source_id=source["id"], extract_error=page.error,
                    )
                    setattr(counts, f"articles_{write.action}",
                            getattr(counts, f"articles_{write.action}") + 1)
                    if not page.usable:
                        raise RuntimeError(page.error or "no content extracted")
                    mark_success(conn, source["id"])
                elif source["kind"] == "search":
                    hits, error = discover(source["url"], fetcher,
                                            max_results=settings.max_items_per_source,
                                            deny_domains=set(settings.search_deny_domains))
                    if error:
                        raise RuntimeError(error)
                    for hit in hits:
                        counts.items_seen += 1
                        if not crawl_web:
                            continue
                        page = fetch_article(hit.url, fetcher)
                        write = store.record_article(
                            conn, url=page.url, title=page.title or hit.title,
                            content=page.content, summary=hit.snippet or page.summary,
                            published_at=page.published_at, source_id=source["id"],
                            extract_error=page.error,
                        )
                        setattr(counts, f"articles_{write.action}",
                                getattr(counts, f"articles_{write.action}") + 1)
                        time.sleep(settings.fetch_delay_seconds)
                    mark_success(conn, source["id"])
                else:
                    raise RuntimeError(f"unknown source kind {source['kind']}")
                counts.sources_ok += 1
            except Exception as exc:  # noqa: BLE001 - per-source isolation is the point
                mark_failure(conn, source["id"], str(exc))
                counts.sources_failed += 1
            finally:
                conn.commit()
                time.sleep(settings.fetch_delay_seconds)

    return counts


def _review_reason(outcome: PerkOutcome) -> str | None:
    parts: list[str] = []
    if outcome.perk.benefit_type is None:
        parts.append("福利类型无可核验引文")
    if outcome.dropped_fields:
        parts.append("模型主张但原文无证据，已置空: " + ",".join(outcome.dropped_fields))
    if outcome.fabricated:
        parts.append("模型给出了原文中不存在的链接: " + ",".join(outcome.fabricated))
    return "；".join(parts)[:400] or None


def analyse_pending(conn: sqlite3.Connection, *, limit: int | None = None,
                    force: bool = False, extractor: Extractor | None = None) -> RunCounts:
    """Spend model tokens only on rows still in ``analyze_state='pending'``.

    A completed article is never re-submitted, so re-running the pipeline costs
    nothing on content already seen. The free keyword prefilter runs first and marks
    irrelevant articles ``skipped`` before any call is made.
    """
    settings = get_settings()
    counts = RunCounts()

    if extractor is None and not settings.llm_enabled:
        counts.notes.append("未配置 DEEPSEEK_API_KEY，本次跳过模型分析（文章已入库，配置后重跑即可）")
        return counts

    extractor = extractor or Extractor()
    # 计费窗口按"这一批是什么时候开始的"判定：18:05 起来的夜间批次必须在早高峰前
    # 收尾；白天补跑的批次没有跨夜，只受高峰判定约束（见 off_peak_window_closed）。
    batch_start = timeutil.now_utc()
    sql = ("SELECT id, url, title, content, summary FROM articles "
           "WHERE analyze_state = 'pending' ORDER BY id")
    if limit:
        sql += f" LIMIT {int(limit)}"

    for row in conn.execute(sql).fetchall():
        if not force and off_peak_window_closed(batch_start=batch_start):
            counts.stopped_early = True
            break

        text = row["content"] or row["summary"] or ""
        verdict = prefilter_score(f"{row['title'] or ''}\n{text}")
        now = timeutil.iso(timeutil.now_utc())

        if not verdict.worth_analysing:
            conn.execute(
                "UPDATE articles SET analyze_state = 'skipped', prefilter_hit = ?, analyzed_at = ? WHERE id = ?",
                (",".join(verdict.matched)[:200], now, row["id"]),
            )
            counts.prefiltered += 1
            conn.commit()
            continue

        report = extractor.analyse(int(row["id"]), text, row["title"])
        if report.error:
            conn.execute(
                "UPDATE articles SET analyze_state = 'failed', analyzed_at = ?, analyze_error = ? WHERE id = ?",
                (now, report.error, row["id"]),
            )
            counts.analysis_failed += 1
            conn.commit()
            continue

        conn.execute(
            "UPDATE articles SET analyze_state = 'done', analyzed_at = ?, prefilter_hit = ? WHERE id = ?",
            (now, ",".join(verdict.matched)[:200], row["id"]),
        )

        for outcome in report.outcomes:
            if not outcome.perk.product or not outcome.perk.entitlement:
                # 反幻觉回溯把产品名或权益描述清空了。没有产品名就没有可核验的对象，
                # 与其写进库里编一个产品，不如计数丢弃并在报告里说明。
                counts.perks_rejected += 1
                conn.commit()
                continue
            if not looks_like_promotion(outcome.perk):
                # 模型自报 is_promotion=true 拦不住两类东西：产品发布稿，以及
                # "Free plan includes 0.5 GB" 这类常驻套餐说明。后者有额度、看起来
                # 很具体，但它是产品的固有属性，没有获取门槛也没有时间边界，读者
                # 无从下手。判定规则见 prefilter.looks_like_promotion。
                counts.perks_unactionable += 1
                conn.commit()
                continue
            try:
                write = store.upsert_perk(
                    conn, outcome.perk, int(row["id"]),
                    evidence=[{"field": e.field, "quote": e.quote, "verified": True}
                              for e in outcome.perk.evidence] or None,
                )
            except store.sqlite3.IntegrityError as exc:
                # A constraint violation aborts only this statement in SQLite, so the
                # article row above stays committed and one bad perk cannot kill a batch.
                counts.perks_rejected += 1
                counts.notes.append(f"福利写入被拒: {str(exc)[:120]}")
                conn.commit()
                continue
            reason = _review_reason(outcome)
            if reason:
                conn.execute("UPDATE perks SET needs_review = 1, review_reason = ? WHERE id = ?",
                             (reason, write.perk_id))
            if write.action == "created":
                counts.perks_created += 1
            elif write.action == "updated":
                counts.perks_updated += 1

        for key, value in report.usage.items():
            counts.model_tokens[key] = counts.model_tokens.get(key, 0) + int(value)
        counts.analysed += 1
        conn.commit()

    return counts


def verify_new(conn: sqlite3.Connection, *, limit: int = 20) -> int:
    """Re-check perks that were never verified, or whose check has gone stale."""
    settings = get_settings()
    verify.sync_verification_window(conn)
    stale_before = timeutil.iso(timeutil.now_utc() - timedelta(days=settings.verification_max_age_days))
    rows = conn.execute(
        "SELECT id FROM perks WHERE status IN ('pending', 'dubious') "
        "AND (last_verified_at IS NULL OR last_verified_at < ?) "
        "ORDER BY updated_at DESC LIMIT ?",
        (stale_before, limit),
    ).fetchall()
    done = 0
    with Fetcher() as fetcher:
        for row in rows:
            try:
                verify.verify_perk(conn, int(row["id"]), fetcher)
                done += 1
            except Exception:  # noqa: BLE001 - one unreachable vendor site must not stop the sweep
                pass
            finally:
                conn.commit()
                time.sleep(settings.fetch_delay_seconds)
    return done


GATE_REVIEW_REASON = "系统规则复核：这只是产品/套餐的固有属性，不是促销活动"


def apply_promotion_gate(conn: sqlite3.Connection, *, limit: int | None = None,
                          dry_run: bool = False) -> dict[str, Any]:
    """Re-decide perks that are already in the database.

    ``looks_like_promotion`` is a heuristic whose word lists live in
    ``config/keywords.toml``, and it was only ever applied at extraction time.
    Anything stored before it existed — or before a list was tightened — keeps being
    mailed every single day, because the ``needs_review`` section now persists until
    something changes the row. This is the way to re-decide what is already stored.

    Rejected perks are marked ``invalid``, not deleted: the status history, versions,
    evidence and sources all survive, and the perk leaves the mail through the normal
    "本周期内过期或失效" channel — once, with its reason on show — instead of silently.
    """
    sql = "SELECT * FROM perks WHERE status IN ('pending', 'dubious') ORDER BY updated_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    gated: list[dict[str, Any]] = []
    checked = 0
    for row in conn.execute(sql).fetchall():
        perk = PerkFields(
            product=row["product"], benefit_type=row["benefit_type"],
            entitlement=row["entitlement"], quota=row["quota"],
            eligibility=row["eligibility"], starts_at=row["starts_at"],
            ends_at=row["ends_at"], claim_url=row["claim_url"], rules_url=row["rules_url"],
        )
        checked += 1
        if looks_like_promotion(perk):
            continue
        gated.append({"id": int(row["id"]), "product": row["product"],
                      "entitlement": row["entitlement"]})
        if dry_run:
            continue
        store.set_status(conn, int(row["id"]), "invalid", GATE_REVIEW_REASON, "system")
        # set_status 只把理由写进状态历史，而日报里读的是 review_reason 列。
        # 不写的话这条福利会以"本周期内过期或失效"出现一次却不说为什么。
        conn.execute("UPDATE perks SET needs_review = 1, review_reason = ? WHERE id = ?",
                     (GATE_REVIEW_REASON, int(row["id"])))
    if not dry_run:
        conn.commit()
    return {"checked": checked, "gated": len(gated), "items": gated}


def run_once(conn: sqlite3.Connection, *, do_analysis: bool = True, do_verify: bool = True,
             limit: int | None = None, force_analysis: bool = False,
             crawl_web: bool = True) -> dict[str, Any]:
    """One complete pipeline pass, recorded in the runs table.

    A ``None`` limit means "use ``ANALYSE_BATCH_LIMIT``", not "everything": a queue
    that has grown to hundreds of articles would otherwise turn one unattended
    evening into the whole month's model budget. Set that setting to 0 to drain.
    """
    settings = get_settings()
    limit = limit or (settings.analyse_batch_limit or None)
    started = timeutil.iso(timeutil.now_utc())
    cursor = conn.execute("INSERT INTO runs (kind, started_at) VALUES ('full', ?)", (started,))
    run_id = int(cursor.lastrowid)
    conn.commit()

    summary: dict[str, Any] = {}
    try:
        counts = collect(conn, crawl_web=crawl_web)
        counts.merge(RunCounts(expired=len(lifecycle.sweep_expired(conn))))
        lifecycle.flag_incomplete(conn)
        if do_analysis:
            counts.merge(analyse_pending(conn, limit=limit, force=force_analysis))
            if do_verify:
                counts.verified = verify_new(conn)
            # 本轮新建的福利也要立刻判"缺不缺关键证据"，否则它们要等到明天
            # 下一趟才被标上待核实原因。
            lifecycle.flag_incomplete(conn)
            # 已经入库的旧条目也要按当前词表复核一遍，否则"套餐说明"会天天回来。
            summary["gate"] = apply_promotion_gate(conn)
        summary = counts.as_dict()
        summary["run_id"] = run_id
        conn.execute(
            "UPDATE runs SET finished_at = ?, counts = ? WHERE id = ?",
            (timeutil.iso(timeutil.now_utc()), db.dumps(summary), run_id),
        )
    except Exception as exc:  # noqa: BLE001
        conn.execute(
            "UPDATE runs SET finished_at = ?, error = ? WHERE id = ?",
            (timeutil.iso(timeutil.now_utc()), f"{type(exc).__name__}: {exc}"[:500], run_id),
        )
        conn.commit()
        raise
    finally:
        _close_unfinished_run(conn, run_id)
        conn.commit()
    return summary


def _close_unfinished_run(conn: sqlite3.Connection, run_id: int) -> None:
    """Close a run that never got to write its own result.

    Ctrl-C / SIGTERM escape ``except Exception``, so an interrupted run used to stay
    in ``runs`` forever with ``finished_at IS NULL``, no counts and no error — a
    phantom run that ``bp stats`` reports as the latest one.
    """
    row = conn.execute("SELECT finished_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None or row["finished_at"] is not None:
        return
    conn.execute("UPDATE runs SET finished_at = ?, error = ? WHERE id = ?",
                 (timeutil.iso(timeutil.now_utc()), "中断：进程在写回结果前结束", run_id))
