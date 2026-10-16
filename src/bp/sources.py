from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import timeutil
from .config import PROJECT_ROOT

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

KINDS = {"rss", "web", "search"}


@dataclass
class SourceSpec:
    name: str
    kind: str
    url: str
    product_hint: str | None = None
    notes: str | None = None


def load_seed_file(path: Path | None = None) -> list[SourceSpec]:
    path = path or PROJECT_ROOT / "config" / "sources.toml"
    if not path.exists():
        return []
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    specs: list[SourceSpec] = []
    for entry in raw.get("source", []):
        kind = str(entry.get("kind", "rss")).strip().lower()
        if kind not in KINDS:
            continue
        url = str(entry.get("url", "")).strip()
        if not url:
            continue
        specs.append(SourceSpec(
            name=str(entry.get("name", url)).strip(),
            kind=kind,
            url=url,
            product_hint=entry.get("product_hint"),
            notes=entry.get("notes"),
        ))
    return specs


def add_source(conn: sqlite3.Connection, spec: SourceSpec, enabled: bool = True) -> int:
    conn.execute(
        "INSERT INTO sources (name, kind, url, enabled, product_hint, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET name = excluded.name, kind = excluded.kind, "
        "enabled = excluded.enabled, product_hint = excluded.product_hint, notes = excluded.notes",
        (spec.name, spec.kind, spec.url, 1 if enabled else 0, spec.product_hint, spec.notes,
         timeutil.iso(timeutil.now_utc())),
    )
    row = conn.execute("SELECT id FROM sources WHERE url = ?", (spec.url,)).fetchone()
    return int(row["id"])


def seed(conn: sqlite3.Connection, path: Path | None = None) -> dict[str, int]:
    specs = load_seed_file(path)
    added = 0
    for spec in specs:
        before = conn.execute("SELECT 1 FROM sources WHERE url = ?", (spec.url,)).fetchone()
        add_source(conn, spec)
        if before is None:
            added += 1
    return {"total": len(specs), "added": added}


def list_sources(conn: sqlite3.Connection, enabled_only: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM sources"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY name"
    return [dict(r) for r in conn.execute(sql)]


def set_enabled(conn: sqlite3.Connection, source_id: int, enabled: bool) -> bool:
    cursor = conn.execute("UPDATE sources SET enabled = ? WHERE id = ?", (1 if enabled else 0, source_id))
    return cursor.rowcount > 0


def remove_source(conn: sqlite3.Connection, source_id: int) -> bool:
    cursor = conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    return cursor.rowcount > 0


def mark_success(conn: sqlite3.Connection, source_id: int, etag: str | None = None,
                 last_modified: str | None = None) -> None:
    conn.execute(
        "UPDATE sources SET last_success_at = ?, last_error = NULL, last_error_at = NULL, "
        "etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified) WHERE id = ?",
        (timeutil.iso(timeutil.now_utc()), etag, last_modified, source_id),
    )


def mark_failure(conn: sqlite3.Connection, source_id: int, message: str) -> None:
    """Recorded per source so one broken feed can never abort the collection run."""
    conn.execute(
        "UPDATE sources SET last_error = ?, last_error_at = ? WHERE id = ?",
        (message[:500], timeutil.iso(timeutil.now_utc()), source_id),
    )
