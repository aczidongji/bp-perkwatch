from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db, timeutil
from .collect.search import discover
from .config import PROJECT_ROOT, get_settings
from .fetch import Fetcher
from .normalize import normalize_text, registrable_domain
from .store import record_status, set_status

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

EXPIRY_MARKERS = (
    "活动已结束", "活动过期", "已过期", "兑换码已失效", "此优惠已停止",
    "no longer available", "offer has ended", "this offer is no longer",
    "campaign ended", "活动已下架", "已下架",
)

BENEFIT_MARKERS = (
    "免费", "试用", "额度", "领取", "兑换", "优惠", "学生", "注册",
    "free", "trial", "credits", "redeem", "discount", "student", "sign up", "get started",
)

# Vendor domains a single user can extend without touching code. Matching is on the
# registrable domain, so docs.cursor.com satisfies cursor.com.
def official_allowlist(path: Path | None = None) -> dict[str, list[str]]:
    path = path or PROJECT_ROOT / "config" / "official_domains.toml"
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    return {str(k).strip().lower(): [str(d).strip().lower() for d in (v or [])] for k, v in raw.items()}


def domain_is_official_for(host: str | None, product_norm: str) -> str | None:
    """Return the product the allowlist says owns ``host``, if any."""
    if not host:
        return None
    for product, domains in official_allowlist().items():
        if host in domains:
            return product
    return None


@dataclass
class Verification:
    perk_id: int
    url: str
    verdict: str
    official_domain_match: bool
    page_accessible: bool
    http_status: int | None
    detail: str
    verification_id: int | None = None

    @property
    def authorises_confirmation(self) -> bool:
        return (
            self.official_domain_match
            and self.page_accessible
            and self.verdict == "available"
        )


def _candidate_official_domain(conn: sqlite3.Connection, perk: sqlite3.Row) -> tuple[str | None, str]:
    claim_host = registrable_domain(perk["claim_url"])
    rules_host = registrable_domain(perk["rules_url"])

    listed = domain_is_official_for(claim_host, perk["product_norm"])
    if listed:
        return claim_host, f"allowlisted official domain for {listed}"

    if claim_host and rules_host and claim_host == rules_host:
        # Article links the claim page and the rules page on one domain: the rules
        # page is the vendor's own terms, so that domain is the strongest candidate.
        return claim_host, "claim_url and rules_url share a registrable domain"

    for host in (rules_host, claim_host):
        if host and host not in {"qq.com", "weixin.qq.com"}:
            return host, "best available candidate domain from the article links"
    return None, "no candidate official domain recoverable from the article"


def _corroborate_via_search(domain: str, product: str, fetcher: Fetcher) -> tuple[bool, str]:
    """Ask a search engine whether ``product``'s own site really is ``domain``.

    This is the check that stops a repost on a coupon aggregator from being read as
    an official page: the aggregator may well host a working copy of the offer, but
    it is not the vendor.
    """
    hits, error = discover(f"{product} 官网 official site", fetcher, max_results=10)
    if error and not hits:
        return False, f"search unavailable: {error}"
    found = [registrable_domain(hit.url) for hit in hits if hit.url]
    if domain in [f for f in found if f]:
        return True, f"search top results include {domain}"
    return False, f"search did not rank {domain} for {product}"


def _assess_page(url: str, product: str, fetcher: Fetcher) -> tuple[str, bool, int | None, str]:
    """(verdict, page_accessible, http_status, detail) for one claimed perk page."""
    reply = fetcher.get(url)
    if reply.error:
        return "error", False, reply.status, reply.error
    if reply.gate:
        # We record the gate and stop. No login, no captcha solving, no payment.
        # A 403 is the same door from the other side, so it is reported as a gate
        # rather than as a broken link.
        return (reply.gate, reply.status < 400, reply.status,
                f"page is gated ({reply.gate}); not bypassed")
    if reply.status >= 400:
        verdict = "not_found" if reply.status in (404, 410) else "error"
        return verdict, False, reply.status, f"HTTP {reply.status}"

    text = normalize_text(reply.body[:120_000])
    if any(marker.lower() in text for marker in EXPIRY_MARKERS):
        return "expired", True, reply.status, "page states the offer has ended"

    haystack = text
    product_present = bool(product) and normalize_text(product) in haystack
    benefit_present = any(marker.lower() in haystack for marker in BENEFIT_MARKERS)
    if benefit_present and (product_present or not product):
        return "available", True, reply.status, "official page still advertises the benefit"
    if not benefit_present:
        return "not_found", True, reply.status, "no benefit language present on the page"
    return "unknown", True, reply.status, "page reachable but product name not found"


def sync_verification_window(conn: sqlite3.Connection) -> None:
    """Keep ``meta['verification_max_age_days']`` equal to the current setting.

    The ``CONFIRM_GUARD`` trigger reads that row, and it used to be written only once
    at ``init_db`` — editing ``VERIFICATION_MAX_AGE_DAYS`` in .env afterwards left the
    database guard on the old window while the application used the new one, so the
    two could silently disagree about whether a check was fresh enough.
    """
    db.set_meta(conn, "verification_max_age_days", str(get_settings().verification_max_age_days))


def verify_perk(conn: sqlite3.Connection, perk_id: int, fetcher: Fetcher) -> list[Verification]:
    """Re-check one perk against its own vendor rather than against the article
    that mentioned it. Writes a verification_runs row per URL examined.

    ``official_domain_match`` is only ever set from allowlist or search
    corroboration, never from the model, so the confirmation trigger cannot be
    satisfied by LLM output alone.
    """
    perk = conn.execute("SELECT * FROM perks WHERE id = ?", (perk_id,)).fetchone()
    if perk is None:
        return []

    settings = get_settings()
    product = perk["product"] or perk["product_norm"]
    official_domain, domain_reason = _candidate_official_domain(conn, perk)

    corroborated: bool | None = None
    results: list[Verification] = []
    now = timeutil.iso(timeutil.now_utc())

    # dict.fromkeys keeps order; many perks quote the same page as both the claim
    # entry and the rules page, and fetching it twice buys nothing.
    for url in dict.fromkeys(x for x in (perk["claim_url"], perk["rules_url"]) if x):
        host = registrable_domain(url)
        if official_domain and host == official_domain:
            if domain_is_official_for(host, perk["product_norm"]):
                match, why = True, f"{domain_reason}; {host} is allowlisted"
            else:
                if corroborated is None:
                    corroborated, corroboration_note = _corroborate_via_search(host, product, fetcher)
                    why = corroboration_note
                else:
                    why = domain_reason
                match = bool(corroborated)
        else:
            match = False
            why = (f"{host or '该链接'} is not the candidate official domain "
                   f"({official_domain or '无法确定'})")

        verdict, accessible, status, detail = _assess_page(url, product, fetcher)
        cursor = conn.execute(
            "INSERT INTO verification_runs (perk_id, url, registrable_domain, official_domain_match, "
            "http_status, page_accessible, verdict, detail, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (perk_id, url, host, 1 if match else 0, status, 1 if accessible else 0,
             verdict, f"{why} | {detail}"[:500], now),
        )
        results.append(Verification(
            perk_id=perk_id, url=url, verdict=verdict, official_domain_match=match,
            page_accessible=accessible, http_status=status, detail=detail,
            verification_id=int(cursor.lastrowid),
        ))

    _apply_verdict(conn, perk, results, now)
    return results


# 一条福利被误判成 存疑 / 无效 之后，官方页面恢复正常时必须能被重新确认——
# 核验本来就是"现在这一刻官方页面说了什么"，没有理由只认第一次的结论。
# 不含 'expired'：它是按 ends_at 由 sweep_expired 判的，当晚就会被再判一次，
# 在这里恢复只会造成 已确认 ↔ 已过期 每天来回翻。
RECOVERABLE_STATUSES = ("pending", "dubious", "invalid")


def _flag_review(conn: sqlite3.Connection, perk_id: int, reason: str) -> bool:
    """True only when the flag or its reason actually changed.

    Rewriting an unchanged flag would move ``updated_at`` for no reason, which is
    exactly what the rest of the system avoids so the daily mail stops repeating
    itself.
    """
    row = conn.execute("SELECT needs_review, review_reason FROM perks WHERE id = ?",
                       (perk_id,)).fetchone()
    if row is None or (row["needs_review"] == 1 and row["review_reason"] == reason):
        return False
    conn.execute("UPDATE perks SET needs_review = 1, review_reason = ? WHERE id = ?",
                 (reason, perk_id))
    return True


def _apply_verdict(conn: sqlite3.Connection, perk: sqlite3.Row, results: list[Verification], now: str) -> None:
    """Move the perk according to what the *official* pages said.

    Confirmation requires a verification row that passes the allowlist/search test
    and reads 'available'; the database trigger enforces the same rule independently.

    ``last_verified_at`` moves on every sweep — that is the point of sweeping — but
    ``updated_at`` means "something the reader needs to see changed", so it must stay
    put when the check confirmed what was already known. Bumping it unconditionally
    pushed an untouched 待核实 item back into the report window on every sweep.
    """
    authorising = next((r for r in results if r.authorises_confirmation), None)
    conn.execute("UPDATE perks SET last_verified_at = ? WHERE id = ?", (now, perk["id"]))
    changed = False

    if authorising:
        conn.execute("UPDATE perks SET verification_id = ? WHERE id = ?",
                     (authorising.verification_id, perk["id"]))
        if perk["status"] in RECOVERABLE_STATUSES:
            changed = set_status(
                conn, perk["id"], "confirmed",
                f"官方域名 {registrable_domain(authorising.url)} 页面仍可领取", "verification")
    else:
        conn.execute("UPDATE perks SET verification_id = NULL WHERE id = ?", (perk["id"],))
        if any(r.verdict == "expired" and r.official_domain_match for r in results):
            changed = set_status(conn, perk["id"], "expired",
                                 "官方页面显示活动已结束", "verification")
        elif any(r.verdict == "not_found" and r.official_domain_match for r in results):
            changed = set_status(conn, perk["id"], "invalid",
                                 "官方页面已无此活动", "verification")
        elif any(r.verdict in ("login_required", "paywall", "captcha") for r in results):
            changed = set_status(conn, perk["id"], "dubious",
                                 "页面存在登录/付费/验证码门槛，未尝试绕过，无法确认", "verification")
            changed = _flag_review(conn, perk["id"], "需人工登录确认") or changed
        elif results and all(r.verdict in ("error", "unknown") for r in results):
            changed = set_status(conn, perk["id"], "dubious",
                                 "官方页面无法判定", "verification")
            changed = _flag_review(conn, perk["id"], "核验结果不明确") or changed
        elif results and not any(r.official_domain_match for r in results):
            changed = _flag_review(conn, perk["id"],
                                   "领取页不在官方域名上，仅第三方转载，不能确认")

    if changed:
        conn.execute("UPDATE perks SET updated_at = ? WHERE id = ?", (now, perk["id"]))
