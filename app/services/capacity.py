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


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    """Requested and safe effective interval for one platform.

    A global interval is a user preference, not an all-or-nothing transaction.
    Each platform independently receives the closest safe interval.  ``None``
    means that no finite interval can be supported with the currently usable
    credentials/IPs, so only that platform is paused.
    """

    platform: Platform
    requested_interval_seconds: int
    effective_interval_seconds: int | None
    requested: CapacitySnapshot
    effective: CapacitySnapshot | None

    @property
    def paused(self) -> bool:
        return self.effective_interval_seconds is None

    @property
    def limited(self) -> bool:
        return (
            self.effective_interval_seconds is not None
            and self.effective_interval_seconds > self.requested_interval_seconds
        )

    @property
    def restricted(self) -> bool:
        return self.paused or self.limited

    @property
    def active_snapshot(self) -> CapacitySnapshot:
        return self.effective or self.requested


@dataclass(frozen=True, slots=True)
class _CapacityInputs:
    platform: Platform
    source_intervals: tuple[int | None, ...]
    account_count: int
    proxy_ip_count: int
    used_requests_today: int
    account_safe_total: int


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


async def _load_capacity_inputs(
    session: AsyncSession, platform: Platform, settings: Settings
) -> _CapacityInputs:
    source_intervals = tuple(
        await session.scalars(
            select(Source.poll_interval_seconds).where(
                Source.platform == platform, Source.status == SourceStatus.ACTIVE
            )
        )
    )
    credentials = await _eligible_credentials(session, platform)
    usage = await usage_map_today(session, [row.id for row in credentials])
    budgets = {
        row.id: adaptive_account_budget(platform, settings, row.rate_limit_events, row.last_rate_limit_at)
        for row in credentials
    }
    usable = [row for row in credentials if usage.get(row.id, 0) < budgets[row.id]]
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

    return _CapacityInputs(
        platform=platform,
        source_intervals=source_intervals,
        account_count=len(usable),
        proxy_ip_count=proxy_ip_count,
        used_requests_today=used_today,
        account_safe_total=account_safe_total,
    )


def _snapshot_from_inputs(
    inputs: _CapacityInputs,
    interval_seconds: int,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> CapacitySnapshot:
    timestamp = now or datetime.now(UTC)
    source_count = len(inputs.source_intervals)
    cost = (
        settings.vk_estimated_requests_per_source_cycle
        if inputs.platform == Platform.VK
        else settings.tg_estimated_requests_per_source_cycle
    )
    estimated = math.ceil(
        sum(cycles_per_day(value or interval_seconds) * cost for value in inputs.source_intervals)
    )
    nominal_per_account = safe_budget_per_account(inputs.platform, settings)
    required_accounts = math.ceil(estimated / nominal_per_account) if estimated else 0
    required_proxy_ips = (
        math.ceil(required_accounts / settings.vk_max_accounts_per_ip)
        if inputs.platform == Platform.VK and required_accounts
        else 0
    )

    safe_total = inputs.account_safe_total
    if inputs.platform == Platform.VK:
        safe_total = min(
            safe_total,
            inputs.proxy_ip_count * settings.vk_max_accounts_per_ip * nominal_per_account,
        )
    remaining = max(0, safe_total - inputs.used_requests_today)
    utilization = estimated / safe_total if safe_total else (math.inf if estimated else 0.0)

    elapsed_seconds = timestamp.hour * 3600 + timestamp.minute * 60 + timestamp.second
    remaining_fraction = max(0.0, (SECONDS_PER_DAY - elapsed_seconds) / SECONDS_PER_DAY)
    requests_needed_for_rest_of_day = math.ceil(estimated * remaining_fraction)

    allowed = True
    reason = ""
    if source_count and inputs.account_count == 0:
        allowed = False
        reason = "no_accounts"
    elif inputs.platform == Platform.VK and source_count and inputs.proxy_ip_count == 0:
        allowed = False
        reason = "no_proxy_ips"
    elif source_count and inputs.account_count < required_accounts:
        allowed = False
        reason = "insufficient_accounts"
    elif (
        inputs.platform == Platform.VK
        and source_count
        and inputs.proxy_ip_count < required_proxy_ips
    ):
        allowed = False
        reason = "insufficient_proxy_ips"
    elif source_count and remaining < requests_needed_for_rest_of_day:
        allowed = False
        reason = "daily_budget_exhausted"

    return CapacitySnapshot(
        platform=inputs.platform,
        interval_seconds=interval_seconds,
        source_count=source_count,
        account_count=inputs.account_count,
        proxy_ip_count=inputs.proxy_ip_count,
        estimated_requests_per_day=estimated,
        safe_requests_per_day=safe_total,
        used_requests_today=inputs.used_requests_today,
        remaining_requests_today=remaining,
        required_accounts=required_accounts,
        required_proxy_ips=required_proxy_ips,
        utilization=utilization,
        allowed=allowed,
        reason=reason,
    )


def _plan_from_inputs(
    inputs: _CapacityInputs,
    requested_interval_seconds: int,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> CapacityPlan:
    requested = _snapshot_from_inputs(inputs, requested_interval_seconds, settings, now=now)
    if requested.allowed:
        return CapacityPlan(
            inputs.platform,
            requested_interval_seconds,
            requested_interval_seconds,
            requested,
            requested,
        )

    if requested.source_count == 0:
        return CapacityPlan(
            inputs.platform,
            requested_interval_seconds,
            requested_interval_seconds,
            requested,
            requested,
        )

    # No interval can compensate for a completely absent credential/IP pool.
    if inputs.account_count == 0 or (inputs.platform == Platform.VK and inputs.proxy_ip_count == 0):
        return CapacityPlan(inputs.platform, requested_interval_seconds, None, requested, None)

    maximum = max(
        requested_interval_seconds,
        int(settings.capacity_max_effective_interval_seconds),
    )
    slowest = _snapshot_from_inputs(inputs, maximum, settings, now=now)
    if not slowest.allowed:
        return CapacityPlan(inputs.platform, requested_interval_seconds, None, requested, None)

    low = requested_interval_seconds
    high = maximum
    while low + 1 < high:
        middle = (low + high) // 2
        candidate = _snapshot_from_inputs(inputs, middle, settings, now=now)
        if candidate.allowed:
            high = middle
        else:
            low = middle
    effective = _snapshot_from_inputs(inputs, high, settings, now=now)
    return CapacityPlan(inputs.platform, requested_interval_seconds, high, requested, effective)


async def calculate_capacity(
    session: AsyncSession,
    *,
    platform: Platform,
    interval_seconds: int,
    settings: Settings,
) -> CapacitySnapshot:
    inputs = await _load_capacity_inputs(session, platform, settings)
    return _snapshot_from_inputs(inputs, interval_seconds, settings)


async def calculate_capacity_plan(
    session: AsyncSession,
    *,
    platform: Platform,
    requested_interval_seconds: int,
    settings: Settings,
) -> CapacityPlan:
    inputs = await _load_capacity_inputs(session, platform, settings)
    return _plan_from_inputs(inputs, requested_interval_seconds, settings)


async def calculate_all_capacities(
    session: AsyncSession, interval_seconds: int, settings: Settings
) -> dict[Platform, CapacitySnapshot]:
    return {
        platform: await calculate_capacity(
            session, platform=platform, interval_seconds=interval_seconds, settings=settings
        )
        for platform in (Platform.VK, Platform.TELEGRAM)
    }


async def calculate_all_capacity_plans(
    session: AsyncSession, requested_interval_seconds: int, settings: Settings
) -> dict[Platform, CapacityPlan]:
    return {
        platform: await calculate_capacity_plan(
            session,
            platform=platform,
            requested_interval_seconds=requested_interval_seconds,
            settings=settings,
        )
        for platform in (Platform.VK, Platform.TELEGRAM)
    }


def _number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def capacity_alert_text(plan: CapacityPlan) -> str:
    label = "VK" if plan.platform == Platform.VK else "Telegram"
    requested = plan.requested
    if plan.paused:
        title = f"⚠️ <b>{label}: сбор приостановлен</b>"
        effective_line = "Фактический интервал: пауза"
    else:
        title = f"⚠️ <b>{label}: частота ограничена безопасным уровнем</b>"
        effective_line = f"Фактический интервал: {plan.effective_interval_seconds} сек."

    lines = [
        title,
        "",
        f"Запрошенный интервал: {plan.requested_interval_seconds} сек.",
        effective_line,
        f"Источников: {requested.source_count}",
        f"Нагрузка при запрошенной частоте: {_number(requested.estimated_requests_per_day)} ед./сутки",
        f"Безопасная ёмкость: {_number(requested.safe_requests_per_day)} ед./сутки",
        f"Рабочих аккаунтов: {requested.account_count}; для запрошенной частоты нужно {requested.required_accounts}",
    ]
    if plan.platform == Platform.VK:
        ip_state = "достаточно" if requested.proxy_ip_count >= requested.required_proxy_ips else "недостаточно"
        lines.extend(
            [
                f"Уникальных рабочих IP: {requested.proxy_ip_count}; нужно {requested.required_proxy_ips} ({ip_state})",
                "Ограничение: не более 3 VK-аккаунтов на один IP",
            ]
        )

    reason_text = {
        "no_accounts": "Причина: нет рабочих аккаунтов этой платформы.",
        "no_proxy_ips": "Причина: нет рабочих прокси с определённым внешним IP.",
        "insufficient_accounts": "Причина: рабочих аккаунтов недостаточно для запрошенной частоты.",
        "insufficient_proxy_ips": "Причина: уникальных IP недостаточно для безопасного размещения VK-аккаунтов.",
        "daily_budget_exhausted": "Причина: безопасный бюджет запросов на текущие сутки исчерпан.",
    }.get(requested.reason, "Причина: текущей мощности недостаточно для запрошенной частоты.")
    lines.extend(["", reason_text])

    if plan.paused:
        lines.append("Очередь и checkpoint сохранены; после появления мощности сбор продолжится автоматически.")
    else:
        lines.append(
            f"{label} продолжает работать с интервалом {plan.effective_interval_seconds} сек.; "
            "другие платформы не замедляются."
        )
    if requested.account_deficit:
        lines.append(f"Добавьте рабочих аккаунтов: минимум {requested.account_deficit}.")
    if requested.proxy_ip_deficit:
        lines.append(f"Добавьте уникальных IP: минимум {requested.proxy_ip_deficit}.")
    return "\n".join(lines)


def capacity_plan_line(plan: CapacityPlan) -> str:
    badge = "🟢 VK" if plan.platform == Platform.VK else "🔵 TG"
    snapshot = plan.active_snapshot
    if plan.paused:
        state = f"ПАУЗА · запрошено {plan.requested_interval_seconds} сек."
    elif plan.limited:
        state = (
            f"ограничено до {plan.effective_interval_seconds} сек. "
            f"(запрошено {plan.requested_interval_seconds})"
        )
    else:
        state = f"работает · {plan.requested_interval_seconds} сек."
    line = (
        f"{badge}: {state} · нагрузка {_number(snapshot.estimated_requests_per_day)} / "
        f"ёмкость {_number(snapshot.safe_requests_per_day)} ед./сутки · "
        f"аккаунты {snapshot.account_count}/{snapshot.required_accounts}"
    )
    if plan.platform == Platform.VK:
        line += f" · IP {snapshot.proxy_ip_count}/{snapshot.required_proxy_ips}"
    return line


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
