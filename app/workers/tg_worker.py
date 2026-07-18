from __future__ import annotations

import asyncio
import json
import socket

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.collectors.errors import (
    AccessDeniedError,
    CollectorError,
    CredentialDeadError,
    RateLimitedError,
    RetryableCollectorError,
)
from app.collectors.telegram import TelegramCollector
from app.config import Settings
from app.db.enums import CredentialPlatform, Platform
from app.db.models import Credential
from app.db.repositories import CredentialRepository, JobRepository, SourceRepository
from app.db.session import SessionFactory
from app.security import SecretBox
from app.services.alerts import AlertService
from app.services.network import check_direct_ip
from app.workers.common import keep_job_lease, persist_collection_result

logger = structlog.get_logger()


class TgWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.secret_box = SecretBox(settings.app_encryption_key)
        self.collector = TelegramCollector(settings)
        self.worker_id = f"tg-{socket.gethostname()}"
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
                credential_row = await session.get(Credential, credential_id)
        if transitioned and credential_row:
            await self.alerts.send_dead_credential(credential_row)

    async def start_client(self, credential: Credential) -> TelegramClient:
        config = credential.config_json or {}
        secret = json.loads(self.secret_box.decrypt(credential.secret_encrypted))
        client = TelegramClient(
            StringSession(str(secret["session"])),
            int(config["api_id"]),
            str(secret["api_hash"]),
            device_model=str(config.get("device_model") or "Desktop"),
            system_version=str(config.get("system_version") or "Linux"),
            app_version=str(config.get("app_version") or "1.0"),
            system_lang_code=str(config.get("system_lang_code") or "en"),
            lang_code=str(config.get("lang_code") or "en"),
            request_retries=3,
            connection_retries=5,
            auto_reconnect=True,
            flood_sleep_threshold=0,
            receive_updates=False,
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise CredentialDeadError("Telegram StringSession is not authorized")
        return client

    async def account_loop(self, credential: Credential, slot: int) -> None:
        worker_id = f"{self.worker_id}-{slot}-{credential.id}"
        client: TelegramClient | None = None
        try:
            client = await self.start_client(credential)
            logger.info("tg_account_started", credential_id=credential.id, label=credential.label)
            while True:
                job_id: int | None = None
                source = None
                job = None
                try:
                    async with SessionFactory() as session:
                        async with session.begin():
                            job = await JobRepository.claim(
                                session,
                                platform=Platform.TELEGRAM,
                                worker_id=worker_id,
                                lease_seconds=self.settings.job_lease_seconds,
                            )
                            if job:
                                job_id = job.id
                                source = await SourceRepository.get(session, job.source_id)
                    if not job or not source:
                        await asyncio.sleep(0.75)
                        continue

                    async with keep_job_lease(
                        job.id,
                        worker_id,
                        self.settings.job_lease_seconds,
                    ):
                        result = await self.collector.collect(source, client)
                        async with SessionFactory() as session:
                            async with session.begin():
                                source = await SourceRepository.get(session, source.id)
                                if not source:
                                    await JobRepository.complete(session, job.id)
                                    continue
                                inserted, deliveries = await persist_collection_result(session, source, result)
                                await CredentialRepository.mark_success(
                                    session,
                                    credential.id,
                                    health_verified=bool(
                                        result.diagnostics.get("credential_content_probe_ok")
                                    ),
                                )
                                if result.needs_immediate_retry:
                                    await JobRepository.retry(
                                        session,
                                        job.id,
                                        "continuing bounded Telegram scan",
                                        delay_seconds=1,
                                    )
                                else:
                                    await JobRepository.complete(session, job.id)
                    logger.info(
                        "tg_job_done",
                        job_id=job.id,
                        source_id=source.id,
                        inserted=inserted,
                        deliveries=deliveries,
                    )
                except RateLimitedError as exc:
                    delay = max(5, exc.retry_after)
                    async with SessionFactory() as session:
                        async with session.begin():
                            await CredentialRepository.cooldown(
                                session,
                                credential.id,
                                delay,
                                str(exc),
                                limited=delay >= self.settings.limited_alert_threshold_seconds,
                            )
                            if job_id:
                                await JobRepository.retry(session, job_id, str(exc), delay_seconds=delay)
                    logger.warning("tg_flood_wait", credential_id=credential.id, delay=delay)
                    return
                except CredentialDeadError as exc:
                    await self._mark_dead_and_alert(credential.id, str(exc))
                    async with SessionFactory() as session:
                        async with session.begin():
                            if job_id:
                                await JobRepository.retry(session, job_id, str(exc), delay_seconds=3)
                    logger.warning("tg_credential_dead", credential_id=credential.id, error=str(exc))
                    return
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
                    logger.warning("tg_retryable_error", job_id=job_id, error=str(exc))
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
                    logger.warning("tg_access_denied", job_id=job_id, final=final, error=str(exc))
                except CollectorError as exc:
                    async with SessionFactory() as session:
                        async with session.begin():
                            if job_id:
                                await JobRepository.complete(session, job_id)
                            if source:
                                await SourceRepository.update_after_error(
                                    session, source.id, exc.code, str(exc), terminal=True
                                )
                except Exception as exc:
                    logger.exception("tg_worker_unexpected", job_id=job_id)
                    async with SessionFactory() as session:
                        async with session.begin():
                            if job_id:
                                await JobRepository.retry(session, job_id, str(exc), delay_seconds=30)
        except CredentialDeadError as exc:
            await self._mark_dead_and_alert(credential.id, str(exc))
        except Exception as exc:
            logger.exception("tg_account_start_failed", credential_id=credential.id)
            async with SessionFactory() as session:
                async with session.begin():
                    await CredentialRepository.cooldown(session, credential.id, 300, str(exc))
        finally:
            if client:
                await client.disconnect()

    async def run(self) -> None:
        tasks: dict[int, asyncio.Task] = {}
        next_ip_check = 0.0
        direct_route_ok = not self.settings.tg_require_non_ru
        try:
            while True:
                loop = asyncio.get_running_loop()
                if self.settings.tg_require_non_ru and loop.time() >= next_ip_check:
                    next_ip_check = loop.time() + 300
                    try:
                        ip_info = await check_direct_ip(self.settings.ip_check_url)
                        direct_route_ok = ip_info.country_code != "RU"
                        if not direct_route_ok:
                            logger.error("telegram_direct_ip_is_ru", ip=ip_info.ip)
                    except Exception:
                        direct_route_ok = False
                        logger.exception("telegram_ip_check_failed")
                    if not direct_route_ok and tasks:
                        for task in tasks.values():
                            task.cancel()
                        await asyncio.gather(*tasks.values(), return_exceptions=True)
                        tasks.clear()
                if not direct_route_ok:
                    await asyncio.sleep(30)
                    continue
                for credential_id, task in list(tasks.items()):
                    if task.done():
                        try:
                            task.result()
                        except Exception:
                            logger.exception("tg_account_task_failed", credential_id=credential_id)
                        tasks.pop(credential_id, None)
                async with SessionFactory() as session:
                    credentials = await CredentialRepository.available(
                        session,
                        CredentialPlatform.TELEGRAM,
                        limit=self.settings.tg_max_active_accounts,
                    )
                for index, credential in enumerate(credentials):
                    if credential.id not in tasks:
                        tasks[credential.id] = asyncio.create_task(
                            self.account_loop(credential, index + 1)
                        )
                if not tasks:
                    logger.warning("no_telegram_accounts")
                await asyncio.sleep(15)
        finally:
            for task in tasks.values():
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks.values(), return_exceptions=True)
            await self.alert_bot.session.close()
