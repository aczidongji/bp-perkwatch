from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import timeutil
from .config import PROJECT_ROOT, Settings, get_settings

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def connect(db_path: Path | None = None, *, check_same_thread: bool = True) -> sqlite3.Connection:
    settings = get_settings()
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # WAL already protects against corruption; NORMAL skips the per-transaction fsync
    # that made a single nightly run take minutes on a spinning disk.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def session(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    return {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every pending ``NNNN_name.sql`` in lexical order, each in its own
    transaction so a broken migration cannot half-apply."""
    done = applied_versions(conn)
    applied: list[str] = []
    for script in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = script.stem
        if version in done:
            continue
        sql = script.read_text(encoding="utf-8")
        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, timeutil.iso(timeutil.now_utc())),
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise RuntimeError(f"migration {version} failed: {exc}") from exc
        applied.append(version)
    return applied


def init_db(db_path: Path | None = None, settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    with session(db_path) as conn:
        applied = migrate(conn)
        set_meta(conn, "verification_max_age_days", str(settings.verification_max_age_days))
    return applied


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
