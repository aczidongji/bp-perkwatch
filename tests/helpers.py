from __future__ import annotations

import itertools
import sqlite3

from bp import store
from bp.models import Evidence, PerkFields

_counter = itertools.count()


def article(conn: sqlite3.Connection, url: str, body: str | None = None,
            title: str | None = "某公众号文章", source_id: int | None = None) -> int:
    """Record one article and return its id. Bodies default to unique text so two
    articles never collapse into one alias row unless the test asks for that."""
    digest = f"{next(_counter)}"
    return store.record_article(
        conn, url=url, title=title, content=body or f"{url} 正文 {digest}",
        summary=(body or url)[:200], source_id=source_id,
    ).article_id


def perk(
    product: str = "Cursor",
    benefit: str | None = "free_credits",
    entitlement: str = "100 次免费补全",
    quota: str | None = "100 次",
    ends: str | None = None,
    starts: str | None = None,
    claim: str | None = "https://cursor.com/promo",
    rules: str | None = None,
    conf: float | None = 0.7,
    **extra: object,
) -> PerkFields:
    evidence = [Evidence(field="entitlement", quote=entitlement)]
    if ends:
        evidence.append(Evidence(field="ends_at", quote=ends))
    if claim:
        evidence.append(Evidence(field="claim_url", quote=claim))
    return PerkFields(
        product=product, benefit_type=benefit, entitlement=entitlement, quota=quota,
        starts_at=starts, ends_at=ends, claim_url=claim, rules_url=rules,
        confidence=conf, evidence=evidence, **extra,  # type: ignore[arg-type]
    )


def add_perk(conn: sqlite3.Connection, p: PerkFields, url: str | None = None) -> int:
    """Store one claim as its own article and return the perk id."""
    key = store.canonical_key(p)
    article_id = article(conn, url or f"https://src.example/{key}")
    write = store.upsert_perk(
        conn, p, article_id,
        evidence=[{"field": e.field, "quote": e.quote} for e in p.evidence],
    )
    conn.commit()
    return write.perk_id


def extraction_payload(*perks: dict, is_promotion: bool = True, notes: str | None = None) -> dict:
    return {"is_promotion": is_promotion, "perks": [dict(p) for p in perks], "notes": notes}


def authorise(
    conn: sqlite3.Connection, perk_id: int, url: str, *,
    verdict: str = "available", official: bool = True, age_days: int = 0,
) -> int:
    """Write a verification_runs row and attach it to the perk, which is the only
    legitimate route to 已确认. ``age_days`` lets a test produce a stale record."""
    from datetime import timedelta

    from bp import timeutil

    checked_at = timeutil.iso(timeutil.now_utc() - timedelta(days=age_days))
    cursor = conn.execute(
        "INSERT INTO verification_runs (perk_id, url, registrable_domain, official_domain_match, "
        "http_status, page_accessible, verdict, detail, checked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (perk_id, url, url.split("//", 1)[-1].split("/")[0], 1 if official else 0,
         200, 1, verdict, "test fixture", checked_at),
    )
    verification_id = int(cursor.lastrowid)
    conn.execute("UPDATE perks SET verification_id = ?, last_verified_at = ? WHERE id = ?",
                 (verification_id, checked_at, perk_id))
    conn.commit()
    return verification_id
