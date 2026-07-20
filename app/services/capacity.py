from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.enums import CredentialPlatform, CredentialStatus, Platform, ProxyStatus, SourceStatus
from app.db.models import ApiUsage, Credential, Proxy, Source

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    platform: Platform
    interval_seconds: int
    source_count: int
    account_count: int
    proxy_ip_count: int
    estimated_requests_per_day: int
    safe_requests_per_day: int
    used_requests_today: int
    remaining_requests_today: int
    required_accounts: int
    required_proxy_ips: int
    utilization: float
    allowed: bool
    reason: str = ""

    @property
    def account_deficit(self) -> int:
        return max(0, self.required_accounts - self.account_count)

    @property
    def proxy_ip_deficit(self) -> int:
        return max(0, self.required_proxy_ips - self.proxy_ip_count)


def cycles_per_day(interval_seconds: int) -> int:
    return math.ceil(SECONDS_PER_DAY / max(1, interval_seconds))


def estimate_requests_per_day(
    *, platform: Platform, source_count: int, interval_seconds: int, settings: Settings
) -> int:
    cycles = cycles_per_day(interval_seconds)
    cost = (
        settings.vk_estimated_requests_per_source_cycle
        if platform == Platform.VK
        else settings.tg_estimated_requests_per_source_cycle
    )
    return math.ceil(source_count * cycles * cost)


def safe_budget_per_account(platform: Platform, settings: Settings) -> int:
    operational = (
        settings.vk_operational_daily_request_budget
        if platform == Platform.VK
        else settings.tg_operational_daily_request_budget
    )
    return max(1, math.floor(operational * settings.account_daily_budget_fraction))


def adaptive_account_budget(
    platform: Platform,
    settings: Settings,
    rate_limit_events: int,
    last_rate_limit_at: datetime | None = None,
) -> int:
    """Reduce the operational budget after real API throttling.

    Published platform limits are dynamic. This is deliberately an internal
    safety budget and not a claim about an official fixed daily quota.
    """
    base = safe_budget_per_account(platform, settings)
    if last_rate_limit_at is not None:
        timestamp = last_rate_limit_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        penalty_age = datetime.now(UTC) - timestamp
        if penalty_age.total_seconds() >= settings.token_rate_limit_penalty_minutes * 60:
            rate_limit_events = 0
    penalty_percent = min(max(rate_limit_events, 0), 3) * 20
    remaining_percent = max(25, 100 - penalty_percent)
    return max(1, base * remaining_percent // 100)


async def _eligible_credentials(session: AsyncSession, platform: Platform) -> list[Credential]:
    now = datetime.now(UTC)
    credential_platform = CredentialPlatform.VK if platform == Platform.VK else CredentialPlatform.TELEGRAM
    return list(
        await session.scalars(
            select(Credential).where(
                Credential.platform == credential_platform,
                or_(
                    Credential.status == CredentialStatus.ACTIVE,
                    and_(
                        Credential.status.in_([CredentialStatus.COOLDOWN, CredentialStatus.LIMITED]),
                        Credential.cooldown_until <= now,
                    ),
                ),
                or_(Credential.expires_at.is_(None), Credential.expires_at > now),
            )
        )
    )


async def usage_map_today(session: AsyncSession, credential_ids: list[int]) -> dict[int, int]:
    if not credential_ids:
        return {}
    today = datetime.now(UTC).date()
    rows = await session.execute(
        select(ApiUsage.credential_id, ApiUsage.request_count).where(
            ApiUsage.credential_id.in_(credential_ids), ApiUsage.usage_date == today
        )
    )
    return {int(credential_id): int(request_count) for credential_id, request_count in rows}


async def calculate_capacity(
    session: AsyncSession,
    *,
    platform: Platform,
    interval_seconds: int,
    settings: Settings,
) -> CapacitySnapshot:
    source_intervals = list(
        await session.scalars(
            select(Source.poll_interval_seconds).where(
                Source.platform == platform, Source.status == SourceStatus.ACTIVE
            )
        )
    )
    source_count = len(source_intervals)
    credentials = await _eligible_credentials(session, platform)
    usage = await usage_map_today(session, [row.id for row in credentials])
    budgets = {
        row.id: adaptive_account_budget(platform, settings, row.rate_limit_events, row.last_rate_limit_at)
        for row in credentials
    }
    usable = [row for row in credentials if usage.get(row.id, 0) < budgets[row.id]]
    account_count = len(usable)
    used_today = sum(min(usage.get(row.id, 0), budgets[row.id]) for row in usable)
    account_safe_total = sum(budgets[row.id] for row in usable)

    proxy_ip_count = 0
    if platform == Platform.VK:
        proxy_ip_count = int(
            await session.scalar(
                select(func.count(func.distinct(Proxy.external_ip))).where(
                    Proxy.status.in_([ProxyStatus.HEALTHY, ProxyStatus.DEGRADED]),
                    Proxy.external_ip != "",
                )
            )
            or 0
        )

    cost = (
        settings.vk_estimated_requests_per_source_cycle
        if platform == Platform.VK
        else settings.tg_estimated_requests_per_source_cycle
    )
    estimated = math.ceil(sum(cycles_per_day(value or interval_seconds) * cost for value in source_intervals))
    nominal_per_account = safe_budget_per_account(platform, settings)
    required_accounts = math.ceil(estimated / nominal_per_account) if estimated else 0
    required_proxy_ips = (
        math.ceil(required_accounts / settings.vk_max_accounts_per_ip)
        if platform == Platform.VK and required_accounts
        else 0
    )
    safe_total = account_safe_total
    if platform == Platform.VK:
        safe_total = min(
            safe_total,
            proxy_ip_count * settings.vk_max_accounts_per_ip * nominal_per_account,
        )
    remaining = max(0, safe_total - used_today)
    utilization = estimated / safe_total if safe_total else (math.inf if estimated else 0.0)

    allowed = True
    reason = ""
    if source_count and account_count < required_accounts:
        allowed = False
        reason = "insufficient_accounts"
    if platform == Platform.VK and source_count and proxy_ip_count < required_proxy_ips:
        allowed = False
        reason = "insufficient_proxy_ips"
    # Daily budget is a hard stop. The scheduler leaves checkpoints untouched and
    # resumes automatically after UTC day rollover or extra capacity is added.
    expected_today = math.ceil(
        estimated
        * (
            (datetime.now(UTC).hour * 3600 + datetime.now(UTC).minute * 60 + datetime.now(UTC).second)
            / SECONDS_PER_DAY
        )
    )
    if source_count and remaining <= max(1, estimated - expected_today):
        allowed = False
        reason = "daily_budget_exhausted"

    return CapacitySnapshot(
        platform=platform,
        interval_seconds=interval_seconds,
        source_count=source_count,
        account_count=account_count,
        proxy_ip_count=proxy_ip_count,
        estimated_requests_per_day=estimated,
        safe_requests_per_day=safe_total,
        used_requests_today=used_today,
        remaining_requests_today=remaining,
        required_accounts=required_accounts,
        required_proxy_ips=required_proxy_ips,
        utilization=utilization,
        allowed=allowed,
        reason=reason,
    )


async def calculate_all_capacities(
    session: AsyncSession, interval_seconds: int, settings: Settings
) -> dict[Platform, CapacitySnapshot]:
    return {
        platform: await calculate_capacity(
            session, platform=platform, interval_seconds=interval_seconds, settings=settings
        )
        for platform in (Platform.VK, Platform.TELEGRAM)
    }


def capacity_alert_text(snapshot: CapacitySnapshot) -> str:
    label = "VK" if snapshot.platform == Platform.VK else "Telegram"
    lines = [
        f"⚠️ <b>Недостаточно безопасной мощности для {label}</b>",
        "",
        f"Источников: {snapshot.source_count}",
        f"Интервал: {snapshot.interval_seconds} сек.",
        f"Расчётная нагрузка: {snapshot.estimated_requests_per_day:,} ед./сутки".replace(",", " "),
        f"Безопасная ёмкость: {snapshot.safe_requests_per_day:,} ед./сутки".replace(",", " "),
        f"Использовано сегодня: {snapshot.used_requests_today:,}".replace(",", " "),
        f"Аккаунтов доступно: {snapshot.account_count}",
        f"Аккаунтов требуется: {snapshot.required_accounts}",
    ]
    if snapshot.platform == Platform.VK:
        lines.extend(
            [
                f"Уникальных рабочих IP: {snapshot.proxy_ip_count}",
                f"IP требуется: {snapshot.required_proxy_ips}",
                "Лимит привязки: 3 VK-аккаунта на IP",
            ]
        )
    lines.extend(
        [
            "",
            "Новые проверки этой платформы приостановлены; очередь и checkpoint сохранены.",
            "Добавьте аккаунты/прокси с новыми IP либо увеличьте интервал проверки.",
        ]
    )
    return "\n".join(lines)


async def record_api_usage(
    session: AsyncSession,
    *,
    credential_id: int,
    platform: Platform,
    request_count: int,
    source_checks: int = 1,
    rate_limit_events: int = 0,
) -> None:
    today = datetime.now(UTC).date()
    values = {
        "credential_id": credential_id,
        "platform": platform.value,
        "usage_date": today,
        "request_count": max(0, request_count),
        "source_checks": max(0, source_checks),
        "rate_limit_events": max(0, rate_limit_events),
    }
    statement = insert(ApiUsage).values(**values)
    statement = statement.on_conflict_do_update(
        constraint="uq_api_usage_credential_date",
        set_={
            "request_count": ApiUsage.request_count + values["request_count"],
            "source_checks": ApiUsage.source_checks + values["source_checks"],
            "rate_limit_events": ApiUsage.rate_limit_events + values["rate_limit_events"],
            "updated_at": datetime.now(UTC),
        },
    )
    await session.execute(statement)


async def usage_today(session: AsyncSession, credential_id: int, usage_date: date | None = None) -> int:
    target = usage_date or datetime.now(UTC).date()
    return int(
        await session.scalar(
            select(ApiUsage.request_count).where(
                ApiUsage.credential_id == credential_id, ApiUsage.usage_date == target
            )
        )
        or 0
    )
