from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.getenv("BP_ENV_FILE", str(PROJECT_ROOT / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    db_path: Path = PROJECT_ROOT / "data" / "bp.db"
    reports_dir: Path = PROJECT_ROOT / "reports"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-flash"
    deepseek_max_output_tokens: int = 4000
    # Articles are analysed in the off-peak window; the batch stops when peak billing starts.
    llm_batch_deadline_hour_bj: int = 8

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_ssl: bool = True
    report_recipients: Annotated[list[str], NoDecode] = Field(
        default_factory=list, validation_alias=AliasChoices("REPORT_TO", "REPORT_RECIPIENTS")
    )

    # 18:05 Beijing: DeepSeek peak ends at 18:00, so this whole run bills at half price.
    pipeline_start_hour_bj: int = 18
    pipeline_start_minute_bj: int = 5
    # 10:00 Beijing: report delivery reads the database only and must not call the model.
    report_send_hour_bj: int = 10
    verification_max_age_days: int = 7
    expiring_soon_days: int = 7
    # The digest repeats the standing "confirmed and still valid" list every day when
    # true. Set false to have each perk appear once per state change instead.
    report_repeat_live_list: bool = True
    # Analysis is otherwise refused inside DeepSeek's peak window to halve the bill.
    force_analysis_anytime: bool = False
    # Per-night cap on how many queued articles the timer may send to the model.
    # Bounds one evening's bill even when the queue has grown by hundreds. 0 = no cap.
    analyse_batch_limit: int = 80

    # Optional: point at a SearXNG instance (self-hosted or trusted) to make keyword
    # discovery use it instead of the DuckDuckGo HTML endpoint.
    searxng_base_url: str = ""
    search_deny_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    max_items_per_source: int = 40

    user_agent: str = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 bp-perkwatch/0.1"
    http_timeout_seconds: float = 25.0
    fetch_delay_seconds: float = 1.5

    @field_validator("report_recipients", "search_deny_domains", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        """Env files list recipients as plain comma-separated text, not JSON."""
        if isinstance(value, str):
            return [x.strip() for x in re.split(r"[,;，；、\s]+", value) if x.strip()]
        return value

    @field_validator("db_path", "reports_dir")
    @classmethod
    def _anchor_to_project(cls, value: Path) -> Path:
        """``DB_PATH=data/bp.db`` is the natural thing to write in an env file, and it
        silently means "whatever directory the command happened to be run from" — which
        makes ``bp`` from anywhere else open a brand-new empty database. Anchor it."""
        return value if value.is_absolute() else PROJECT_ROOT / value

    @property
    def llm_enabled(self) -> bool:
        return bool(self.deepseek_api_key)

    @property
    def mail_enabled(self) -> bool:
        return bool(self.smtp_username and self.smtp_password and self.report_recipients)

    def recipients(self) -> list[str]:
        return list(self.report_recipients or [])


@lru_cache
def get_settings() -> Settings:
    return Settings()
