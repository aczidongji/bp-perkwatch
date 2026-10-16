from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

from bp import config, db


@pytest.fixture
def test_env(tmp_path: Path, monkeypatch) -> Path:
    """Point every settings-derived path at a private temp dir and make sure no
    real credentials from the developer's .env can reach a test."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bp.db"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("SMTP_USERNAME", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    monkeypatch.setenv("SMTP_FROM", "")
    monkeypatch.setenv("REPORT_TO", "")
    monkeypatch.setenv("SEARXNG_BASE_URL", "")
    # The settings object is lru_cached; drop it so the env above takes effect.
    config.get_settings.cache_clear()
    yield tmp_path
    config.get_settings.cache_clear()


@pytest.fixture
def conn(test_env: Path) -> sqlite3.Connection:
    """A real migrated SQLite database, but with the durability writes switched off.

    ``db.init_db`` opens its own session (WAL + fsync), which costs seconds per test
    on a busy laptop while proving nothing the fixture's own ``migrate`` call does not
    already prove.
    """
    path = test_env / "bp.db"
    connection = db.connect(path)
    connection.execute("PRAGMA journal_mode=MEMORY")
    connection.execute("PRAGMA synchronous=OFF")
    db.migrate(connection)
    db.set_meta(connection, "verification_max_age_days",
                str(config.get_settings().verification_max_age_days))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def frozen_today():
    from datetime import date
    return date(2026, 10, 15)
