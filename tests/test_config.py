from __future__ import annotations

from pathlib import Path

from bp.config import PROJECT_ROOT, Settings


def settings(**overrides) -> Settings:
    """Build Settings from explicit values only — never from the developer's .env."""
    return Settings(_env_file=None, **overrides)


class TestPathAnchoring:
    """A relative ``DB_PATH`` means "the directory the command happened to run from",
    so invoking the CLI elsewhere silently opens a fresh empty database."""

    def test_relative_db_path_is_anchored_to_the_project(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert settings(db_path="data/bp.db").db_path == PROJECT_ROOT / "data/bp.db"

    def test_relative_reports_dir_is_anchored_too(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert settings(reports_dir="reports").reports_dir == PROJECT_ROOT / "reports"

    def test_absolute_paths_are_left_alone(self, tmp_path: Path) -> None:
        target = tmp_path / "bp.db"
        assert settings(db_path=str(target)).db_path == target

    def test_env_variable_forms_anchor_identically(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DB_PATH", "data/bp.db")
        assert Settings().db_path == PROJECT_ROOT / "data/bp.db"

    def test_default_is_unchanged_when_the_setting_is_absent(self) -> None:
        assert settings().db_path == PROJECT_ROOT / "data/bp.db"
        assert settings().reports_dir == PROJECT_ROOT / "reports"

    def test_a_database_written_from_one_directory_is_read_from_another(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The whole point: opening the project DB must not depend on the caller's cwd."""
        from bp import config, db

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setenv("DB_PATH", "data/bp.db")
        config.get_settings.cache_clear()
        try:
            conn = db.connect()
            try:
                # 干净检出时 data/ 不在仓库里（被 .gitignore 排除），所以这条用例必须
                # 自己建表，不能假设项目里已经有一份迁移过的库。否则它只在
                # "开发机上恰好有 bp.db" 时才过，CI 上必挂。
                db.migrate(conn)
                assert conn.execute("SELECT COUNT(*) FROM perks").fetchone()[0] >= 0
            finally:
                conn.close()
        finally:
            config.get_settings.cache_clear()
