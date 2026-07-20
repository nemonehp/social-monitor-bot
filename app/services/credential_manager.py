from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.collectors.errors import NetworkCollectorError, ProxyUnavailableError
from app.collectors.vk import VkAccountIdentity, VkCollector
from app.config import Settings
from app.db.enums import CredentialPlatform, Platform
from app.db.repositories import CredentialRepository, ProxyRepository
from app.db.session import SessionFactory
from app.security import SecretBox
from app.services.account_importer import TgAccountInput, VkAccountInput
from app.services.capacity import record_api_usage


@dataclass(frozen=True, slots=True)
class VkImportResult:
    created: int
    updated: int
    duplicates_removed: int
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _VkProbeRoute:
    proxy_url: str
    external_ip: str


class CredentialManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.secret_box = SecretBox(settings.app_encryption_key)
        self.vk_collector = VkCollector(settings)

    async def _vk_probe_routes(self) -> list[_VkProbeRoute]:
        async with SessionFactory() as session:
            proxies = await ProxyRepository.available(session, limit=500)
        # One route per external IP. Identity checks are distributed instead of
        # sending every newly imported account through the server IP.
        result: list[_VkProbeRoute] = []
        seen_ips: set[str] = set()
        for proxy in proxies:
            if not proxy.external_ip or proxy.external_ip in seen_ips:
                continue
            seen_ips.add(proxy.external_ip)
            result.append(
                _VkProbeRoute(
                    proxy_url=self.secret_box.decrypt(proxy.proxy_url_encrypted),
                    external_ip=proxy.external_ip,
                )
            )
        return result

    async def _resolve_vk_identity(
        self,
        account: VkAccountInput,
        routes: list[_VkProbeRoute],
        route_offset: int,
    ) -> VkAccountIdentity:
        if routes:
            # Try each distinct IP once for route-level failures. Token/API
            # failures are raised immediately by VkCollector and are not hidden.
            for step in range(len(routes)):
                route = routes[(route_offset + step) % len(routes)]
                try:
                    return await self.vk_collector.resolve_current_account(
                        account.token,
                        proxy_url=route.proxy_url,
                    )
                except (ProxyUnavailableError, NetworkCollectorError):
                    continue
        # Direct fallback keeps account recovery possible when the proxy pool is
        # temporarily unavailable. The normal worker still requires safe proxy
        # affinity before using the account for collection.
        return await self.vk_collector.resolve_current_account(account.token)

    async def save_vk(self, accounts: list[VkAccountInput]) -> VkImportResult:
        created = updated = duplicates_removed = 0
        errors: list[str] = []
        routes = await self._vk_probe_routes()

        for offset, account in enumerate(accounts):
            try:
                identity = await self._resolve_vk_identity(account, routes, offset)
                supplied_user_id = account.config.get("user_id")
                if (
                    supplied_user_id not in (None, "")
                    and int(str(supplied_user_id)) != identity.user_id
                ):
                    raise ValueError(
                        "user_id в OAuth-ответе не совпадает с владельцем проверенного токена"
                    )
                config = {
                    **account.config,
                    "user_id": identity.user_id,
                    "display_name": identity.display_name,
                    "screen_name": identity.screen_name,
                    "identity_verified_at": datetime.now(UTC).isoformat(),
                }
                async with SessionFactory() as session:
                    async with session.begin():
                        row, is_created, removed = await CredentialRepository.upsert_vk_identity(
                            session,
                            account_id=identity.user_id,
                            encrypted_secret=self.secret_box.encrypt(account.token),
                            config=config,
                            expires_at=account.expires_at,
                        )
                        now = datetime.now(UTC)
                        row.requests_count += 1
                        row.last_success_at = now
                        row.last_health_check_at = now
                        row.last_health_ok_at = now
                        await record_api_usage(
                            session,
                            credential_id=row.id,
                            platform=Platform.VK,
                            request_count=1,
                            source_checks=0,
                        )
                created += int(is_created)
                updated += int(not is_created)
                duplicates_removed += removed
            except Exception as exc:  # noqa: BLE001 - every input line must be isolated
                message = str(exc).replace(account.token, "<redacted>")
                errors.append(f"Строка {account.line_number}: {message}")

        return VkImportResult(
            created=created,
            updated=updated,
            duplicates_removed=duplicates_removed,
            errors=tuple(errors),
        )

    async def save_tg(self, accounts: list[TgAccountInput]) -> tuple[int, int]:
        created = updated = 0
        async with SessionFactory() as session:
            async with session.begin():
                for account in accounts:
                    _, is_created = await CredentialRepository.add(
                        session,
                        CredentialPlatform.TELEGRAM,
                        account.label,
                        self.secret_box.encrypt(
                            json.dumps({"session": account.session, "api_hash": account.api_hash})
                        ),
                        {
                            "api_id": account.api_id,
                            "device_model": account.device_model,
                            "system_version": account.system_version,
                            "app_version": account.app_version,
                            "system_lang_code": account.system_lang_code,
                            "lang_code": account.lang_code,
                        },
                    )
                    created += int(is_created)
                    updated += int(not is_created)
        return created, updated
