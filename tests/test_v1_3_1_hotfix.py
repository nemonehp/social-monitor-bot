from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from app.db.enums import Platform
from app.services.capacity import (
    _CapacityInputs,
    _plan_from_inputs,
    capacity_alert_text,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        account_daily_budget_fraction=0.30,
        vk_operational_daily_request_budget=100_000,
        tg_operational_daily_request_budget=250_000,
        vk_estimated_requests_per_source_cycle=2.25,
        tg_estimated_requests_per_source_cycle=2.0,
        vk_max_accounts_per_ip=3,
        capacity_max_effective_interval_seconds=86_400,
        token_rate_limit_penalty_minutes=60,
    )


def test_capacity_plan_is_independent_for_vk_and_telegram() -> None:
    settings = _settings()
    now = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    vk = _plan_from_inputs(
        _CapacityInputs(
            platform=Platform.VK,
            source_intervals=(None,) * 83,
            account_count=0,
            proxy_ip_count=7,
            used_requests_today=0,
            account_safe_total=0,
        ),
        120,
        settings,
        now=now,
    )
    tg = _plan_from_inputs(
        _CapacityInputs(
            platform=Platform.TELEGRAM,
            source_intervals=(None,) * 77,
            account_count=14,
            proxy_ip_count=0,
            used_requests_today=0,
            account_safe_total=14 * 75_000,
        ),
        120,
        settings,
        now=now,
    )
    assert vk.paused
    assert vk.requested.required_accounts == 5
    assert vk.requested.proxy_ip_count == 7
    assert not tg.paused
    assert tg.effective_interval_seconds == 120


def test_vk_is_slowed_instead_of_blocking_other_platforms() -> None:
    settings = _settings()
    plan = _plan_from_inputs(
        _CapacityInputs(
            platform=Platform.VK,
            source_intervals=(None,) * 83,
            account_count=1,
            proxy_ip_count=1,
            used_requests_today=0,
            account_safe_total=30_000,
        ),
        120,
        settings,
        now=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
    )
    assert plan.limited
    assert plan.effective_interval_seconds is not None
    assert plan.effective_interval_seconds > 120
    assert plan.effective is not None and plan.effective.allowed


def test_capacity_warning_does_not_request_proxies_when_ips_are_sufficient() -> None:
    settings = _settings()
    plan = _plan_from_inputs(
        _CapacityInputs(
            platform=Platform.VK,
            source_intervals=(None,) * 83,
            account_count=0,
            proxy_ip_count=7,
            used_requests_today=0,
            account_safe_total=0,
        ),
        120,
        settings,
        now=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
    )
    text = capacity_alert_text(plan)
    assert "7; нужно 2 (достаточно)" in text
    assert "Добавьте рабочих аккаунтов: минимум 5" in text
    assert "Добавьте уникальных IP" not in text


def test_group_service_messages_are_silent_and_bind_stays_available() -> None:
    middleware = Path("app/bot/middleware.py").read_text(encoding="utf-8")
    assert 'event.chat.type != "private"' in middleware
    assert 'text.startswith("/start") and "bind_" in text' in middleware
    assert "return None" in middleware


def test_interval_is_saved_without_cross_platform_denial() -> None:
    handlers = Path("app/bot/handlers.py").read_text(encoding="utf-8")
    assert "_capacity_denial_for_interval" not in handlers
    assert 'SettingsRepository.set(session, "poll_interval_seconds", value)' in handlers
    assert "Недостаточная мощность VK не замедляет Telegram" in handlers


def test_duplicate_zero_account_alerts_are_disabled() -> None:
    scheduler = Path("app/services/scheduler.py").read_text(encoding="utf-8")
    assert '"vk_accounts_zero",\n            active=False' in scheduler
    assert '"tg_accounts_zero",\n            active=False' in scheduler
    assert "Нет доступных VK-токенов. Сбор VK остановлен." not in scheduler


def test_scheduler_uses_platform_specific_effective_intervals() -> None:
    scheduler = Path("app/services/scheduler.py").read_text(encoding="utf-8")
    repository = Path("app/db/repositories.py").read_text(encoding="utf-8")
    assert "effective_intervals" in scheduler
    assert "default_intervals=effective_intervals" in scheduler
    assert "default_intervals: dict[Platform, int] | None" in repository
