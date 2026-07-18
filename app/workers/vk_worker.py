from __future__ import annotations

import asyncio
import random
import socket
from dataclasses import dataclass

import structlog

from app.collectors.errors import (
    AccessDeniedError,
    CollectorError,
    CredentialDeadError,
    NetworkCollectorError,
    ProxyUnavailableError,
    RateLimitedError,
    RetryableCollectorError,
)
from app.collectors.vk import VkCollector
from app.config import Settings
from app.db.enums import CredentialPlatform, Platform
from app.db.repositories import (
    CredentialRepository,
    JobRepository,
    ProxyRepository,
    SourceRepository,
)
from app.db.session import SessionFactory
from app.security import SecretBox
from app.workers.common import keep_job_lease, persist_collection_result

logger = structlog.get_logger()


@dataclass(slots=True)
class PoolEntry:
    id: int
    value: str


class RotatingPool:
    def __init__(self):
        self._queue: asyncio.Queue[PoolEntry] = asyncio.Queue()
        self._ids: set[int] = set()
        self._in_use: set[int] = set()
        self._lock = asyncio.Lock()

    async def replace(self, entries: list[PoolEntry]) -> None:
        async with self._lock:
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            by_id = {entry.id: entry for entry in entries}
            self._ids = set(by_id)
            self._in_use.intersection_update(self._ids)
            for entry_id, entry in by_id.items():
                if entry_id not in self._in_use:
                    self._queue.put_nowait(entry)

    async def acquire(self, wait_seconds: float = 5.0) -> PoolEntry | None:
        try:
            entry = await asyncio.wait_for(self._queue.get(), timeout=wait_seconds)
        except TimeoutError:
            return None
        async with self._lock:
            self._in_use.add(entry.id)
        return entry

    async def release(self, entry: PoolEntry) -> None:
        async with self._lock:
            self._in_use.discard(entry.id)
            if entry.id in self._ids:
                self._queue.put_nowait(entry)

    async def discard(self, entry: PoolEntry) -> None:
        async with self._lock:
            self._ids.discard(entry.id)
            self._in_use.discard(entry.id)


class VkWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.secret_box = SecretBox(settings.app_encryption_key)
        self.collector = VkCollector(settings)
        self.credentials = RotatingPool()
        self.proxies = RotatingPool()
        self.worker_id = f"vk-{socket.gethostname()}"

    async def refresh_pools(self) -> None:
        async with SessionFactory() as session:
            credentials = await CredentialRepository.available(session, CredentialPlatform.VK)
            proxies = await ProxyRepository.available(session)
        await self.credentials.replace(
            [PoolEntry(row.id, self.secret_box.decrypt(row.secret_encrypted)) for row in credentials]
        )
        await self.proxies.replace(
            [PoolEntry(row.id, self.secret_box.decrypt(row.proxy_url_encrypted)) for row in proxies]
        )

    async def pool_refresher(self) -> None:
        while True:
            try:
                await self.refresh_pools()
            except Exception:
                logger.exception("vk_pool_refresh_failed")
            await asyncio.sleep(30)

    async def run_slot(self, slot: int) -> None:
        worker_id = f"{self.worker_id}-{slot}"
        while True:
            credential = await self.credentials.acquire()
            proxy = await self.proxies.acquire()
            if not credential or not proxy:
                if credential:
                    await self.credentials.release(credential)
                if proxy:
                    await self.proxies.release(proxy)
                await asyncio.sleep(3)
                continue

            job_id: int | None = None
            job = None
            source = None
            try:
                async with SessionFactory() as session:
                    async with session.begin():
                        job = await JobRepository.claim(
                            session,
                            platform=Platform.VK,
                            worker_id=worker_id,
                            lease_seconds=self.settings.job_lease_seconds,
                        )
                        if not job:
                            job_id = None
                        else:
                            job_id = job.id
                            source = await SourceRepository.get(session, job.source_id)
                    if not job_id or not source:
                        await asyncio.sleep(0.5)
                        continue

                async with keep_job_lease(
                    job_id,
                    worker_id,
                    self.settings.job_lease_seconds,
                ):
                    result = await self.collector.collect(
                        source,
                        token=credential.value,
                        proxy_url=proxy.value,
                    )
                    async with SessionFactory() as session:
                        async with session.begin():
                            source = await SourceRepository.get(session, source.id)
                            if not source:
                                await JobRepository.complete(session, job_id)
                                continue
                            inserted, deliveries = await persist_collection_result(session, source, result)
                            await CredentialRepository.mark_success(session, credential.id)
                            await ProxyRepository.mark_success(session, proxy.id)
                            if result.needs_immediate_retry:
                                await JobRepository.retry(
                                    session,
                                    job_id,
                                    "continuing bounded VK scan",
                                    delay_seconds=1,
                                )
                            else:
                                await JobRepository.complete(session, job_id)
                logger.info(
                    "vk_job_done",
                    job_id=job_id,
                    source_id=source.id,
                    inserted=inserted,
                    deliveries=deliveries,
                    retry=result.needs_immediate_retry,
                )

            except CredentialDeadError as exc:
                async with SessionFactory() as session:
                    async with session.begin():
                        await CredentialRepository.mark_dead(session, credential.id, str(exc))
                        if job_id:
                            await JobRepository.retry(session, job_id, str(exc), delay_seconds=3)
                await self.credentials.discard(credential)
                credential = None
                logger.warning("vk_credential_dead", error=str(exc))
            except RateLimitedError as exc:
                delay = max(2, exc.retry_after) + random.randint(0, 3)
                async with SessionFactory() as session:
                    async with session.begin():
                        await CredentialRepository.cooldown(session, credential.id, delay, str(exc))
                        if job_id:
                            await JobRepository.retry(session, job_id, str(exc), delay_seconds=delay)
                await self.credentials.discard(credential)
                credential = None
            except (NetworkCollectorError, ProxyUnavailableError) as exc:
                async with SessionFactory() as session:
                    async with session.begin():
                        status = await ProxyRepository.mark_failure(
                            session,
                            proxy.id,
                            str(exc),
                            quarantine_after=self.settings.proxy_failures_to_quarantine,
                            remove_after=self.settings.proxy_failures_to_remove,
                            quarantine_minutes=self.settings.proxy_quarantine_minutes,
                            immediate_remove=isinstance(exc, ProxyUnavailableError),
                        )
                        if job_id:
                            final = bool(job and job.attempts >= self.settings.max_job_attempts)
                            await JobRepository.retry(
                                session,
                                job_id,
                                str(exc),
                                delay_seconds=min(300, 2 ** min(8, job.attempts if job else 1)),
                                final=final,
                            )
                            if source:
                                await SourceRepository.update_after_error(session, source.id, exc.code, str(exc))
                if status.value in {"quarantine", "removed"}:
                    await self.proxies.discard(proxy)
                    proxy = None
                logger.warning("vk_network_error", job_id=job_id, error=str(exc))
            except RetryableCollectorError as exc:
                async with SessionFactory() as session:
                    async with session.begin():
                        if job_id:
                            final = bool(job and job.attempts >= self.settings.max_job_attempts)
                            await JobRepository.retry(
                                session,
                                job_id,
                                str(exc),
                                delay_seconds=min(300, 2 ** min(8, job.attempts if job else 1)),
                                final=final,
                            )
                            if source:
                                await SourceRepository.update_after_error(session, source.id, exc.code, str(exc))
                logger.warning("vk_retryable_error", job_id=job_id, error=str(exc))
            except AccessDeniedError as exc:
                async with SessionFactory() as session:
                    async with session.begin():
                        final = bool(job and job.attempts >= self.settings.max_credential_tries_per_source)
                        if job_id:
                            if final:
                                await JobRepository.complete(session, job_id)
                            else:
                                await JobRepository.retry(session, job_id, str(exc), delay_seconds=1)
                        if source and final:
                            await SourceRepository.update_after_error(
                                session, source.id, exc.code, str(exc), terminal=True
                            )
                logger.warning("vk_access_denied", job_id=job_id, final=final, error=str(exc))
            except CollectorError as exc:
                async with SessionFactory() as session:
                    async with session.begin():
                        if job_id:
                            await JobRepository.complete(session, job_id)
                        if source:
                            await SourceRepository.update_after_error(
                                session, source.id, exc.code, str(exc), terminal=True
                            )
                logger.warning("vk_terminal_error", job_id=job_id, error=str(exc))
            except Exception as exc:
                logger.exception("vk_worker_unexpected", job_id=job_id)
                async with SessionFactory() as session:
                    async with session.begin():
                        if job_id:
                            await JobRepository.retry(session, job_id, str(exc), delay_seconds=30)
            finally:
                if credential:
                    await self.credentials.release(credential)
                if proxy:
                    await self.proxies.release(proxy)

    async def run(self) -> None:
        await self.refresh_pools()
        tasks = [asyncio.create_task(self.pool_refresher())]
        tasks.extend(
            asyncio.create_task(self.run_slot(i + 1))
            for i in range(self.settings.vk_worker_concurrency)
        )
        await asyncio.gather(*tasks)
