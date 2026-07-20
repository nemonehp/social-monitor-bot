from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.enums import CredentialPlatform, Platform
from app.db.models import Proxy
from app.db.repositories import CredentialRepository, ProxyRepository
from app.security import SecretBox
from app.services.capacity import adaptive_account_budget, usage_map_today


@dataclass(frozen=True, slots=True)
class VkAssignment:
    credential_id: int
    credential_label: str
    token: str
    proxy_id: int
    proxy_url: str
    external_ip: str


def plan_proxy_affinity(
    credentials: list[tuple[int, int | None, int]],
    proxies: list[tuple[int, str]],
    *,
    max_accounts_per_ip: int,
) -> dict[int, int]:
    """Plan stable credential->proxy bindings without exceeding an IP cap."""
    proxy_by_id = {proxy_id: external_ip for proxy_id, external_ip in proxies if external_ip}
    representative_by_ip: dict[str, int] = {}
    for proxy_id, external_ip in proxies:
        if external_ip:
            representative_by_ip.setdefault(external_ip, proxy_id)
    load = {external_ip: 0 for external_ip in representative_by_ip}
    selected: dict[int, int] = {}
    for credential_id, current_proxy_id, _requests in sorted(credentials):
        current_ip = proxy_by_id.get(current_proxy_id or -1)
        if current_ip and load[current_ip] < max_accounts_per_ip:
            selected[credential_id] = representative_by_ip[current_ip]
            load[current_ip] += 1
    for credential_id, _current_proxy_id, _requests in sorted(credentials, key=lambda row: (row[2], row[0])):
        if credential_id in selected:
            continue
        candidates = [
            (count, external_ip, representative_by_ip[external_ip])
            for external_ip, count in load.items()
            if count < max_accounts_per_ip
        ]
        if not candidates:
            break
        _count, external_ip, proxy_id = min(candidates)
        selected[credential_id] = proxy_id
        load[external_ip] += 1
    return selected


def assignment_epoch(settings: Settings) -> str:
    seconds = max(60, settings.vk_assignment_epoch_minutes * 60)
    return str(int(datetime.now(UTC).timestamp()) // seconds)


async def build_vk_assignments(
    session: AsyncSession,
    *,
    settings: Settings,
    secret_box: SecretBox,
) -> list[VkAssignment]:
    credentials = await CredentialRepository.available(session, CredentialPlatform.VK, limit=500)
    proxies = [row for row in await ProxyRepository.available(session, limit=500) if row.external_ip]
    if not credentials or not proxies:
        return []

    # One representative proxy route per external IP. Keeping the route stable is
    # more important than rotating through credentials for every source request.
    proxy_by_ip: dict[str, Proxy] = {}
    for proxy in proxies:
        proxy_by_ip.setdefault(proxy.external_ip, proxy)
    usage = await usage_map_today(session, [row.id for row in credentials])
    credentials = [
        row
        for row in credentials
        if usage.get(row.id, 0)
        < adaptive_account_budget(Platform.VK, settings, row.rate_limit_events, row.last_rate_limit_at)
    ]

    epoch = assignment_epoch(settings)
    proxy_by_id = {proxy.id: proxy for proxy in proxy_by_ip.values()}
    selected_proxy_ids = plan_proxy_affinity(
        [
            (credential.id, credential.assigned_proxy_id, credential.requests_count)
            for credential in credentials
        ],
        [(proxy.id, proxy.external_ip) for proxy in proxy_by_ip.values()],
        max_accounts_per_ip=settings.vk_max_accounts_per_ip,
    )
    selected = {
        credential_id: proxy_by_id[proxy_id] for credential_id, proxy_id in selected_proxy_ids.items()
    }

    result: list[VkAssignment] = []
    for credential in credentials:
        assigned_proxy = selected.get(credential.id)
        if assigned_proxy is None:
            if credential.assigned_proxy_id is not None:
                await CredentialRepository.unbind_credential_proxy(session, credential.id)
            continue
        if (
            credential.assigned_proxy_id != assigned_proxy.id
            or credential.assigned_external_ip != assigned_proxy.external_ip
            or credential.assignment_epoch != epoch
        ):
            await CredentialRepository.bind_proxy(
                session,
                credential.id,
                assigned_proxy.id,
                assigned_proxy.external_ip,
                epoch,
            )
        result.append(
            VkAssignment(
                credential_id=credential.id,
                credential_label=credential.label,
                token=secret_box.decrypt(credential.secret_encrypted),
                proxy_id=assigned_proxy.id,
                proxy_url=secret_box.decrypt(assigned_proxy.proxy_url_encrypted),
                external_ip=assigned_proxy.external_ip,
            )
        )
    return result
