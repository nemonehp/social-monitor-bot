from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    admin_telegram_id: int
    database_url: str
    app_encryption_key: str

    default_poll_interval_seconds: int = 120
    min_poll_interval_seconds: int = 30
    scheduler_tick_seconds: int = 2
    job_lease_seconds: int = 300
    max_job_attempts: int = 8
    max_credential_tries_per_source: int = 5

    vk_worker_concurrency: int = 6
    tg_max_active_accounts: int = 20
    delivery_concurrency: int = 4

    vk_page_size: int = 100
    vk_max_pages_per_run: int = 20
    vk_per_token_min_interval_seconds: float = 0.35
    tg_batch_messages: int = 500
    media_retention_hours: int = 72
    job_history_days: int = 7
    delivery_history_days: int = 30

    ip_check_url: str = "https://ipwho.is/"
    tg_require_non_ru: bool = True
    proxy_failures_to_quarantine: int = 2
    proxy_failures_to_remove: int = 5
    proxy_quarantine_minutes: int = 15
    proxy_low_watermark: int = 2
    alert_cooldown_minutes: int = 30

    vk_api_version: str = "5.131"
    vk_api_base: str = "https://api.vk.com/method"

    media_root: Path = Field(default=Path("data/media"))
    log_level: str = "INFO"

    @field_validator("default_poll_interval_seconds")
    @classmethod
    def validate_poll_interval(cls, value: int, info):
        minimum = info.data.get("min_poll_interval_seconds", 30)
        if value < minimum:
            raise ValueError(f"DEFAULT_POLL_INTERVAL_SECONDS must be >= {minimum}")
        return value

    @field_validator("app_encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: str) -> str:
        if value == "replace_with_fernet_key" or len(value) < 40:
            raise ValueError("APP_ENCRYPTION_KEY must be a valid Fernet key")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]
    settings.media_root.mkdir(parents=True, exist_ok=True)
    return settings
