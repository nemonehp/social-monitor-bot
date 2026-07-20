from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.config import Settings
from app.db.repositories import ProxyRepository
from app.db.session import SessionFactory
from app.security import SecretBox
from app.services.network import check_proxy, check_vk_access
from app.utils.proxies import ParsedProxy, parse_proxy_line


@dataclass(slots=True)
class ProxyCheckResult:
    raw: str
    parsed: ParsedProxy | None
    ok: bool
    reason: str = ""
    country_code: str = ""
    external_ip: str = ""
    latency_ms: int = 0


class ProxyManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.secret_box = SecretBox(settings.app_encryption_key)

    async def check_one(self, raw: str) -> ProxyCheckResult:
        try:
            parsed = parse_proxy_line(raw)
            info = await check_proxy(parsed.url, self.settings.ip_check_url)
            if info.country_code != "RU":
                return ProxyCheckResult(
                    raw=raw,
                    parsed=parsed,
                    ok=False,
                    reason=f"IP не РФ: {info.country_code}",
                    country_code=info.country_code,
                    external_ip=info.ip,
                    latency_ms=info.latency_ms,
                )
            await check_vk_access(parsed.url)
            return ProxyCheckResult(
                raw=raw,
                parsed=parsed,
                ok=True,
                country_code=info.country_code,
                external_ip=info.ip,
                latency_ms=info.latency_ms,
            )
        except Exception as exc:
            return ProxyCheckResult(raw=raw, parsed=None, ok=False, reason=str(exc))

    async def check_many(self, text: str, concurrency: int = 10) -> list[ProxyCheckResult]:
        lines = [
            line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
        ]
        semaphore = asyncio.Semaphore(concurrency)

        async def wrapped(line: str) -> ProxyCheckResult:
            async with semaphore:
                return await self.check_one(line)

        return await asyncio.gather(*(wrapped(line) for line in lines))

    async def save_working(self, results: list[ProxyCheckResult]) -> tuple[int, int]:
        created = 0
        updated = 0
        async with SessionFactory() as session:
            async with session.begin():
                for result in results:
                    if not result.ok or not result.parsed:
                        continue
                    _, is_created = await ProxyRepository.add(
                        session,
                        canonical_url=result.parsed.url,
                        encrypted_url=self.secret_box.encrypt(result.parsed.url),
                        display=result.parsed.display,
                        scheme=result.parsed.scheme,
                        country_code=result.country_code,
                        external_ip=result.external_ip,
                        latency_ms=result.latency_ms,
                    )
                    created += int(is_created)
                    updated += int(not is_created)
        return created, updated
