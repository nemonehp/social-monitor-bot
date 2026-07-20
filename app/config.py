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
    delivery_concurrency: int = 1
    delivery_batch_size: int = 0

    vk_page_size: int = 100
    vk_max_pages_per_run: int = 20
    vk_per_token_min_interval_seconds: float = 0.35
    tg_batch_messages: int = 500

    # Conservative operational budgets. Telegram and VK do not publish one fixed
    # universal daily read limit for these methods; the guard deliberately uses
    # only this fraction and further adapts after real FLOOD/rate-limit responses.
    account_daily_budget_fraction: float = 0.30
    vk_operational_daily_request_budget: int = 100_000
    tg_operational_daily_request_budget: int = 250_000
    vk_estimated_requests_per_source_cycle: float = 2.25
    tg_estimated_requests_per_source_cycle: float = 2.0
    vk_max_accounts_per_ip: int = 3
    capacity_guard_enabled: bool = True
    capacity_alert_repeat_minutes: int = 360
    capacity_max_effective_interval_seconds: int = 86_400
    integrity_gap_retry_seconds: int = 15
    integrity_gap_alert_after: int = 3
    token_rate_limit_penalty_minutes: int = 60
    vk_assignment_epoch_minutes: int = 60
    credential_health_probe_posts: int = 5
    collection_overlap_seconds: int = 120
    media_max_previews_per_item: int = 4
    media_max_preview_bytes: int = 3_000_000
    media_max_download_bytes: int = 12_000_000
    media_max_image_edge: int = 1600
    media_min_preview_edge: int = 320
    video_preview_overlay: bool = True
    media_retention_hours: int = 24
    media_delete_after_delivery: bool = True
    job_history_days: int = 7
    delivery_history_days: int = 30

    ip_check_url: str = "https://ipwho.is/"
    tg_require_non_ru: bool = True
    proxy_failures_to_quarantine: int = 2
    proxy_failures_to_remove: int = 5
    proxy_quarantine_minutes: int = 15
    proxy_remove_after_hours: int = 3
    proxy_low_ratio: float = 0.5
    proxy_low_watermark: int = 1
    alert_cooldown_minutes: int = 30
    health_alert_repeat_minutes: int = 360
    limited_alert_threshold_seconds: int = 1800
    daily_report_hour_moscow: int = 0
    daily_report_top_sources: int = 5

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
