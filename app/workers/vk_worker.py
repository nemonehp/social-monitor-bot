from __future__ import annotations

import asyncio
import hashlib
import random
import socket

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

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
from app.db.enums import Platform
from app.db.models import Credential
from app.db.repositories import CredentialRepository, JobRepository, ProxyRepository, SourceRepository
from app.db.session import SessionFactory
from app.security import SecretBox
from app.services.alerts import AlertService
from app.services.capacity import record_api_usage
from app.services.vk_assignments import VkAssignment, build_vk_assignments
from app.workers.common import keep_job_lease, persist_collection_result

logger = structlog.get_logger()


class AssignmentPool:
    def __init__(self) -> None:
        self._entries: dict[int, VkAssignment] = {}
        self._in_use: set[int] = set()
        self._lock = asyncio.Lock()

    async def replace(self, entries: list[VkAssignment]) -> None:
        async with self._lock:
            self._entries = {entry.credential_id: entry for entry in entries}
            self._in_use.intersection_update(self._entries)

    @staticmethod
    def _score(source_id: int, credential_id: int) -> bytes:
        return hashlib.blake2b(f"{source_id}:{credential_id}".encode(), digest_size=16).digest()

    async def acquire_for(self, source_id: int, wait_seconds: float = 5.0) -> VkAssignment | None:
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            async with self._lock:
                # Select the preferred account from the complete pool, not merely
                # from accounts that happen to be idle at this millisecond. This
                # preserves source->account grouping and prevents accidental token
                # rotation caused by worker concurrency.
                preferred = max(
                    self._entries.values(),
                    key=lambda row: self._score(source_id, row.credential_id),
                    default=None,
                )
                if preferred and preferred.credential_id not in self._in_use:
                    self._in_use.add(preferred.credential_id)
                    return preferred
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.1)

    async def release(self, entry: VkAssignment) -> None:
        async with self._lock:
            self._in_use.discard(entry.credential_id)

    async def discard(self, entry: VkAssignment) -> None:
        async with self._lock:
            self._entries.pop(entry.credential_id, None)
            self._in_use.discard(entry.credential_id)


class VkWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.secret_box = SecretBox(settings.app_encryption_key)
        self.collector = VkCollector(settings)
        self.assignments = AssignmentPool()
        self.worker_id = f"vk-{socket.gethostname()}"
        self.alert_bot = Bot(
            settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.alerts = AlertService(self.alert_bot, settings)

    async def _mark_dead_and_alert(self, credential_id: int, error: str) -> None:
        credential_row = None
        transitioned = False
        async with SessionFactory() as session:
            async with session.begin():
                transitioned = await CredentialRepository.mark_dead(session, credential_id, error)
                await CredentialRepository.unbind_credential_proxy(session, credential_id)
                credential_row = await session.get(Credential, credential_id)
        if transitioned and credential_row:
            await self.alerts.send_dead_credential(credential_row)

    async def refresh_assignments(self) -> None:
        async with SessionFactory() as session:
            async with session.begin():
                rows = await build_vk_assignments(
                    session,
                    settings=self.settings,
                    secret_box=self.secret_box,
                )
        await self.assignments.replace(rows)
        logger.info(
            "vk_assignment_pool_refreshed",
            accounts=len(rows),
            unique_ips=len({row.external_ip for row in rows}),
        )

    async def pool_refresher(self) -> None:
        while True:
            try:
                await self.refresh_assignments()
            except Exception:
                logger.exception("vk_assignment_refresh_failed")
            await asyncio.sleep(30)

    async def run_slot(self, slot: int) -> None:
        worker_id = f"{self.worker_id}-{slot}"
        while True:
            assignment: VkAssignment | None = None
            job_id: int | None = None
            job = None
            source = None
            keep_assignment = True
            try:
                async with SessionFactory() as session:
                    async with session.begin():
                        job = await JobRepository.claim(
                            session,
                            platform=Platform.VK,
                            worker_id=worker_id,
                            lease_seconds=self.settings.job_lease_seconds,
                        )
                        if job:
                            job_id = job.id
                            source = await SourceRepository.get(session, job.source_id)
                if not job_id or not source:
                    await asyncio.sleep(0.5)
                    continue
                assignment = await self.assignments.acquire_for(source.id)
                if assignment is None:
                    async with SessionFactory() as session:
                        async with session.begin():
                            await JobRepository.retry(
                                session, job_id, "no safe VK account/IP assignment", delay_seconds=60
                            )
                    await asyncio.sleep(1)
                    continue

                async with keep_job_lease(job_id, worker_id, self.settings.job_lease_seconds):
                    result = await self.collector.collect(
                        source,
                        token=assignment.token,
                        proxy_url=assignment.proxy_url,
                    )
                    request_count = max(1, int(result.diagnostics.get("api_request_count") or 1))
                    async with SessionFactory() as session:
                        async with session.begin():
                            source = await SourceRepository.get(session, source.id)
                            if not source:
                                await JobRepository.complete(session, job_id)
                                continue
                            inserted, deliveries = await persist_collection_result(session, source, result)
                            await record_api_usage(
                                session,
                                credential_id=assignment.credential_id,
                                platform=Platform.VK,
                                request_count=request_count,
                            )
                            await CredentialRepository.mark_success(
                                session,
                                assignment.credential_id,
                                request_count=request_count,
                                health_verified=bool(result.diagnostics.get("credential_content_probe_ok")),
                            )
                            await ProxyRepository.mark_success(session, assignment.proxy_id)
                            if result.needs_immediate_retry:
                                await JobRepository.retry(
                                    session,
                                    job_id,
                                    "continuing bounded VK/integrity scan",
                                    delay_seconds=(
                                        self.settings.integrity_gap_retry_seconds
                                        if result.diagnostics.get("integrity_status") == "suspected_gap"
                                        else 1
                                    ),
                                )
                            else:
                                await JobRepository.complete(session, job_id)
                logger.info(
                    "vk_job_done",
                    job_id=job_id,
                    source_id=source.id,
                    credential_id=assignment.credential_id,
                    proxy_id=assignment.proxy_id,
                    proxy_ip=assignment.external_ip,
                    inserted=inserted,
                    deliveries=deliveries,
                    retry=result.needs_immediate_retry,
                )

            except CredentialDeadError as exc:
                assert assignment is not None
                keep_assignment = False
                await self._mark_dead_and_alert(assignment.credential_id, str(exc))
                async with SessionFactory() as session:
                    async with session.begin():
                        if job_id:
                            await JobRepository.retry(session, job_id, str(exc), delay_seconds=3)
                logger.warning(
                    "vk_credential_dead",
                    credential_id=assignment.credential_id,
                    label=assignment.credential_label,
                    error=str(exc),
                )
            except RateLimitedError as exc:
                assert assignment is not None
                keep_assignment = False
                delay = max(2, exc.retry_after) + random.randint(0, 3)
                async with SessionFactory() as session:
                    async with session.begin():
                        await record_api_usage(
                            session,
                            credential_id=assignment.credential_id,
                            platform=Platform.VK,
                            request_count=1,
                            rate_limit_events=1,
                        )
                        await CredentialRepository.cooldown(
                            session,
                            assignment.credential_id,
                            delay,
                            str(exc),
                            limited=delay >= self.settings.limited_alert_threshold_seconds,
                            rate_limited=True,
                        )
                        if job_id:
                            await JobRepository.retry(session, job_id, str(exc), delay_seconds=delay)
                logger.warning(
                    "vk_rate_limited",
                    credential_id=assignment.credential_id,
                    proxy_ip=assignment.external_ip,
                    delay=delay,
                )
            except (NetworkCollectorError, ProxyUnavailableError) as exc:
                assert assignment is not None
                async with SessionFactory() as session:
                    async with session.begin():
                        status = await ProxyRepository.mark_failure(
                            session,
                            assignment.proxy_id,
                            str(exc),
                            quarantine_after=self.settings.proxy_failures_to_quarantine,
                            remove_after=self.settings.proxy_failures_to_remove,
                            quarantine_minutes=self.settings.proxy_quarantine_minutes,
                            remove_after_hours=self.settings.proxy_remove_after_hours,
                            immediate_remove=False,
                        )
                        if status.value in {"quarantine", "removed"}:
                            await CredentialRepository.unbind_proxy(session, proxy_id=assignment.proxy_id)
                            keep_assignment = False
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
                                await SourceRepository.update_after_error(
                                    session, source.id, exc.code, str(exc)
                                )
                logger.warning(
                    "vk_network_error",
                    job_id=job_id,
                    proxy_id=assignment.proxy_id,
                    proxy_ip=assignment.external_ip,
                    error=str(exc),
                )
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
                                await SourceRepository.update_after_error(
                                    session, source.id, exc.code, str(exc)
                                )
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
                if assignment is not None:
                    if keep_assignment:
                        await self.assignments.release(assignment)
                    else:
                        await self.assignments.discard(assignment)

    async def run(self) -> None:
        await self.refresh_assignments()
        tasks = [asyncio.create_task(self.pool_refresher())]
        tasks.extend(
            asyncio.create_task(self.run_slot(i + 1)) for i in range(self.settings.vk_worker_concurrency)
        )
        try:
            await asyncio.gather(*tasks)
        finally:
            await self.alert_bot.session.close()
