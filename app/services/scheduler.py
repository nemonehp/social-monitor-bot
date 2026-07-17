from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import delete, func, select

from app.config import Settings
from app.db.enums import CredentialPlatform, CredentialStatus, DeliveryStatus, JobStatus, ProxyStatus
from app.db.models import CollectionJob, Credential, Delivery, Proxy
from app.db.repositories import (
    AccessRepository,
    DeliveryRepository,
    JobRepository,
    ProxyRepository,
    SettingsRepository,
)
from app.db.session import SessionFactory
from app.security import SecretBox
from app.services.alerts import AlertService
from app.services.media_cleanup import cleanup_delivered_media
from app.services.network import check_direct_ip, check_proxy, check_vk_access

logger = structlog.get_logger()


class Scheduler:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.alerts = AlertService(self.bot, settings)
        self.secret_box = SecretBox(settings.app_encryption_key)
        self._tick = 0

    async def bootstrap(self) -> None:
        async with SessionFactory() as session:
            async with session.begin():
                await AccessRepository.ensure_admin(session, self.settings.admin_telegram_id)
                if await SettingsRepository.get(session, "poll_interval_seconds", None) is None:
                    await SettingsRepository.set(
                        session, "poll_interval_seconds", self.settings.default_poll_interval_seconds
                    )

    async def schedule(self) -> None:
        async with SessionFactory() as session:
            async with session.begin():
                interval = int(
                    await SettingsRepository.get(
                        session,
                        "poll_interval_seconds",
                        self.settings.default_poll_interval_seconds,
                    )
                )
                recovered_jobs = await JobRepository.recover_expired(session)
                recovered_deliveries = await DeliveryRepository.recover_expired(session)
                created = await JobRepository.schedule_due_sources(
                    session, default_interval=interval, limit=2000
                )
        if created or recovered_jobs or recovered_deliveries:
            logger.info(
                "scheduler_tick",
                created=created,
                recovered_jobs=recovered_jobs,
                recovered_deliveries=recovered_deliveries,
            )

    async def recheck_proxies(self) -> None:
        async with SessionFactory() as session:
            rows = list(
                await session.scalars(
                    select(Proxy)
                    .where(Proxy.status != ProxyStatus.REMOVED)
                    .order_by(Proxy.last_check_at.nullsfirst())
                    .limit(20)
                )
            )
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        for row in rows:
            if row.last_check_at and row.last_check_at > cutoff and row.status == ProxyStatus.HEALTHY:
                continue
            url = self.secret_box.decrypt(row.proxy_url_encrypted)
            try:
                info = await check_proxy(url, self.settings.ip_check_url)
                if info.country_code != "RU":
                    raise RuntimeError(f"Proxy IP is {info.country_code}, expected RU")
                await check_vk_access(url)
                async with SessionFactory() as session:
                    async with session.begin():
                        await ProxyRepository.mark_success(session, row.id, info.latency_ms)
            except Exception as exc:
                async with SessionFactory() as session:
                    async with session.begin():
                        await ProxyRepository.mark_failure(
                            session,
                            row.id,
                            str(exc),
                            quarantine_after=self.settings.proxy_failures_to_quarantine,
                            remove_after=self.settings.proxy_failures_to_remove,
                            quarantine_minutes=self.settings.proxy_quarantine_minutes,
                            immediate_remove="expected RU" in str(exc),
                        )

    async def cleanup_history(self) -> tuple[int, int]:
        now = datetime.now(timezone.utc)
        async with SessionFactory() as session:
            async with session.begin():
                jobs = await session.execute(
                    delete(CollectionJob).where(
                        CollectionJob.status.in_([JobStatus.DONE, JobStatus.FAILED]),
                        CollectionJob.updated_at < now - timedelta(days=self.settings.job_history_days),
                    )
                )
                deliveries = await session.execute(
                    delete(Delivery).where(
                        Delivery.status.in_([DeliveryStatus.SENT, DeliveryStatus.FAILED]),
                        Delivery.updated_at < now - timedelta(days=self.settings.delivery_history_days),
                    )
                )
        return int(jobs.rowcount or 0), int(deliveries.rowcount or 0)

    async def health_alerts(self) -> None:
        async with SessionFactory() as session:
            proxy_counts = await ProxyRepository.counts(session)
            active_vk = int(
                await session.scalar(
                    select(func.count()).select_from(Credential).where(
                        Credential.platform == CredentialPlatform.VK,
                        Credential.status == CredentialStatus.ACTIVE,
                    )
                )
                or 0
            )
            active_tg = int(
                await session.scalar(
                    select(func.count()).select_from(Credential).where(
                        Credential.platform == CredentialPlatform.TELEGRAM,
                        Credential.status == CredentialStatus.ACTIVE,
                    )
                )
                or 0
            )
            pending_jobs = int(
                await session.scalar(
                    select(func.count()).select_from(CollectionJob).where(
                        CollectionJob.status.in_([JobStatus.PENDING, JobStatus.RETRY])
                    )
                )
                or 0
            )
        working_proxies = proxy_counts.get("healthy", 0) + proxy_counts.get("degraded", 0)
        try:
            direct_ip = await check_direct_ip(self.settings.ip_check_url)
            direct_is_ru = direct_ip.country_code == "RU"
            await self.alerts.send_stateful(
                "direct_ip_ru",
                active=direct_is_ru,
                active_text=(
                    "⚠️ <b>Прямой IP сервера находится в РФ</b>\n\n"
                    f"IP: {direct_ip.ip}. Telegram-worker приостановит работу."
                ),
                recovery_text=f"✅ Прямой маршрут снова не РФ: {direct_ip.country_code}, {direct_ip.ip}",
                payload={"ip": direct_ip.ip, "country": direct_ip.country_code},
            )
        except Exception as exc:
            logger.warning("direct_ip_check_failed", error=str(exc))

        await self.alerts.send_stateful(
            "vk_proxy_low",
            active=working_proxies < self.settings.proxy_low_watermark,
            active_text=(
                "⚠️ <b>Прокси VK заканчиваются</b>\n\n"
                f"Рабочих: {working_proxies}\n"
                f"В карантине: {proxy_counts.get('quarantine', 0)}\n"
                f"Удалено: {proxy_counts.get('removed', 0)}"
            ),
            recovery_text=f"✅ Пул VK-прокси восстановлен. Рабочих: {working_proxies}",
            payload=proxy_counts,
        )
        await self.alerts.send_stateful(
            "vk_accounts_zero",
            active=active_vk == 0,
            active_text="⚠️ Нет активных VK-токенов. Сбор VK остановлен.",
            recovery_text=f"✅ VK-токены снова доступны: {active_vk}",
            payload={"active": active_vk},
        )
        await self.alerts.send_stateful(
            "tg_accounts_zero",
            active=active_tg == 0,
            active_text="⚠️ Нет активных Telegram-сессий. Сбор Telegram остановлен.",
            recovery_text=f"✅ Telegram-сессии снова доступны: {active_tg}",
            payload={"active": active_tg},
        )
        if pending_jobs > 10000:
            await self.alerts.send_stateful(
                "queue_backlog",
                active=True,
                active_text=f"⚠️ Очередь проверок выросла до {pending_jobs} задач.",
                recovery_text="✅ Очередь проверок вернулась в норму.",
                payload={"pending": pending_jobs},
            )
        else:
            await self.alerts.send_stateful(
                "queue_backlog",
                active=False,
                active_text="",
                recovery_text="✅ Очередь проверок вернулась в норму.",
                payload={"pending": pending_jobs},
            )

    async def run(self) -> None:
        await self.bootstrap()
        try:
            while True:
                try:
                    await self.schedule()
                    self._tick += 1
                    if self._tick % max(1, 300 // self.settings.scheduler_tick_seconds) == 0:
                        await self.recheck_proxies()
                        await self.health_alerts()
                    if self._tick % max(1, 3600 // self.settings.scheduler_tick_seconds) == 0:
                        removed = await cleanup_delivered_media(self.settings.media_retention_hours)
                        if removed:
                            logger.info("media_cleanup", removed=removed)
                    if self._tick % max(1, 86400 // self.settings.scheduler_tick_seconds) == 0:
                        jobs_removed, deliveries_removed = await self.cleanup_history()
                        logger.info(
                            "history_cleanup",
                            jobs_removed=jobs_removed,
                            deliveries_removed=deliveries_removed,
                        )
                except Exception:
                    logger.exception("scheduler_failed")
                await asyncio.sleep(self.settings.scheduler_tick_seconds)
        finally:
            await self.bot.session.close()
