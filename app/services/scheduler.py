from __future__ import annotations

import asyncio
import math
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import delete, func, select

from app.config import Settings
from app.db.enums import (
    CredentialPlatform,
    CredentialStatus,
    DeliveryStatus,
    ItemType,
    JobStatus,
    Platform,
    ProxyStatus,
)
from app.db.models import CollectionJob, Credential, Delivery, Item, Proxy, Source
from app.db.repositories import (
    AccessRepository,
    CredentialRepository,
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
from app.utils.text import h

logger = structlog.get_logger()
MOSCOW = ZoneInfo("Europe/Moscow")


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
                if await SettingsRepository.get(session, "daily_report_last_date", None) is None:
                    yesterday_msk = datetime.now(MOSCOW).date() - timedelta(days=1)
                    await SettingsRepository.set(
                        session, "daily_report_last_date", yesterday_msk.isoformat()
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
                    .limit(50)
                )
            )
        cutoff = datetime.now(UTC) - timedelta(minutes=5)
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
                            remove_after_hours=self.settings.proxy_remove_after_hours,
                            immediate_remove=False,
                        )

    async def cleanup_history(self) -> tuple[int, int]:
        now = datetime.now(UTC)
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

    async def notify_dead_credentials(self) -> None:
        async with SessionFactory() as session:
            rows = await CredentialRepository.unreported_dead(session)
        for credential in rows:
            await self.alerts.send_dead_credential(credential)

    async def health_alerts(self) -> None:
        now = datetime.now(UTC)
        async with SessionFactory() as session:
            proxy_counts = await ProxyRepository.counts(session)
            vk_pool_accounts = int(
                await session.scalar(
                    select(func.count()).select_from(Credential).where(
                        Credential.platform == CredentialPlatform.VK,
                        Credential.status.in_(
                            [
                                CredentialStatus.ACTIVE,
                                CredentialStatus.COOLDOWN,
                                CredentialStatus.LIMITED,
                            ]
                        ),
                    )
                )
                or 0
            )
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
            tg_pool_accounts = int(
                await session.scalar(
                    select(func.count()).select_from(Credential).where(
                        Credential.platform == CredentialPlatform.TELEGRAM,
                        Credential.status.in_(
                            [
                                CredentialStatus.ACTIVE,
                                CredentialStatus.COOLDOWN,
                                CredentialStatus.LIMITED,
                            ]
                        ),
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
            limited_rows = list(
                await session.scalars(
                    select(Credential).where(
                        Credential.status == CredentialStatus.LIMITED,
                        Credential.cooldown_until
                        >= now + timedelta(seconds=self.settings.limited_alert_threshold_seconds),
                    )
                )
            )
            non_limited_ids = list(
                await session.scalars(
                    select(Credential.id).where(Credential.status != CredentialStatus.LIMITED)
                )
            )

        working_proxies = proxy_counts.get("healthy", 0) + proxy_counts.get("degraded", 0)
        proxy_threshold = max(
            self.settings.proxy_low_watermark,
            math.ceil(vk_pool_accounts * self.settings.proxy_low_ratio),
        )
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
                payload={"ip": direct_ip.ip, "country": direct_ip.country_code},
                repeat_while_active=True,
                cooldown_minutes=self.settings.health_alert_repeat_minutes,
            )
        except Exception as exc:
            logger.warning("direct_ip_check_failed", error=str(exc))

        await self.alerts.send_stateful(
            "vk_proxy_low",
            active=vk_pool_accounts > 0 and working_proxies < proxy_threshold,
            active_text=(
                "⚠️ <b>Недостаточно рабочих VK-прокси</b>\n\n"
                f"Рабочих: {working_proxies}\n"
                f"Нужно минимум: {proxy_threshold}\n"
                f"VK-аккаунтов в пуле: {vk_pool_accounts}\n"
                f"В карантине: {proxy_counts.get('quarantine', 0)}\n"
                f"Удалено: {proxy_counts.get('removed', 0)}"
            ),
            payload={**proxy_counts, "threshold": proxy_threshold, "accounts": vk_pool_accounts},
            repeat_while_active=True,
            cooldown_minutes=self.settings.health_alert_repeat_minutes,
        )
        await self.alerts.send_stateful(
            "vk_accounts_zero",
            active=vk_pool_accounts == 0,
            active_text="⚠️ Нет доступных VK-токенов. Сбор VK остановлен.",
            payload={"active": active_vk, "pool": vk_pool_accounts},
            repeat_while_active=True,
            cooldown_minutes=self.settings.health_alert_repeat_minutes,
        )
        await self.alerts.send_stateful(
            "tg_accounts_zero",
            active=tg_pool_accounts == 0,
            active_text="⚠️ Нет доступных Telegram-сессий. Сбор Telegram остановлен.",
            payload={"active": active_tg, "pool": tg_pool_accounts},
            repeat_while_active=True,
            cooldown_minutes=self.settings.health_alert_repeat_minutes,
        )
        await self.alerts.send_stateful(
            "queue_backlog",
            active=pending_jobs > 10000,
            active_text=f"⚠️ Очередь проверок выросла до {pending_jobs} задач.",
            payload={"pending": pending_jobs},
            repeat_while_active=True,
            cooldown_minutes=self.settings.health_alert_repeat_minutes,
        )
        for credential in limited_rows:
            cooldown_until = credential.cooldown_until or now
            remaining = max(0, int((cooldown_until - now).total_seconds()))
            await self.alerts.send_stateful(
                f"credential_limited:{credential.id}",
                active=True,
                active_text=(
                    "⚠️ <b>Аккаунт надолго ограничен API</b>\n\n"
                    f"Платформа: {credential.platform.value.upper()}\n"
                    f"Аккаунт: <code>{h(credential.label)}</code>\n"
                    f"Осталось ожидать: {remaining // 60} мин."
                ),
                payload={"credential_id": credential.id, "remaining": remaining},
                repeat_while_active=False,
            )
        for credential_id in non_limited_ids:
            await self.alerts.send_stateful(
                f"credential_limited:{credential_id}",
                active=False,
                active_text="",
                payload={"credential_id": credential_id},
            )

    async def _daily_stats(self, report_date: date) -> str:
        start_msk = datetime.combine(report_date, time.min, tzinfo=MOSCOW)
        end_msk = start_msk + timedelta(days=1)
        start_utc = start_msk.astimezone(UTC)
        end_utc = end_msk.astimezone(UTC)
        async with SessionFactory() as session:
            grouped = list(
                (
                    await session.execute(
                        select(Item.platform, Item.item_type, func.count())
                        .where(Item.published_at >= start_utc, Item.published_at < end_utc)
                        .group_by(Item.platform, Item.item_type)
                    )
                ).all()
            )
            total_all = int(await session.scalar(select(func.count()).select_from(Item)) or 0)
            top_rows = list(
                (
                    await session.execute(
                        select(
                            Source.title,
                            Source.normalized_link,
                            Item.source_id,
                            func.count().label("items_count"),
                            func.count().filter(Item.item_type == ItemType.POST).label("posts_count"),
                            func.count().filter(Item.item_type == ItemType.STORY).label("stories_count"),
                        )
                        .join(Source, Source.id == Item.source_id)
                        .where(Item.published_at >= start_utc, Item.published_at < end_utc)
                        .group_by(Source.title, Source.normalized_link, Item.source_id)
                        .order_by(func.count().desc(), Item.source_id)
                        .limit(self.settings.daily_report_top_sources)
                    )
                ).all()
            )
            credential_counts = await CredentialRepository.counts(session)
            proxy_counts = await ProxyRepository.counts(session)

        counts = {(platform, item_type): int(count) for platform, item_type, count in grouped}
        tg_posts = counts.get((Platform.TELEGRAM, ItemType.POST), 0)
        tg_stories = counts.get((Platform.TELEGRAM, ItemType.STORY), 0)
        vk_posts = counts.get((Platform.VK, ItemType.POST), 0)
        vk_stories = counts.get((Platform.VK, ItemType.STORY), 0)
        daily_total = tg_posts + tg_stories + vk_posts + vk_stories
        lines = [
            f"📊 <b>Статистика за {report_date.strftime('%d.%m.%Y')}</b>",
            "",
            f"Собрано за сутки: <b>{daily_total}</b>",
            f"Постов: {tg_posts + vk_posts}",
            f"Историй: {tg_stories + vk_stories}",
            "",
            f"✈️ Telegram: {tg_posts + tg_stories} · посты {tg_posts} · истории {tg_stories}",
            f"🟦 VK: {vk_posts + vk_stories} · посты {vk_posts} · истории {vk_stories}",
            f"Всего объектов в базе: {total_all}",
        ]
        if top_rows:
            lines.extend(["", "<b>Самые активные источники:</b>"])
            for index, row in enumerate(top_rows, start=1):
                title = row.title or row.normalized_link
                lines.append(
                    f"{index}. {h(title)} — {int(row.items_count)} "
                    f"(посты {int(row.posts_count)}, истории {int(row.stories_count)})"
                )
        lines.extend(
            [
                "",
                "<b>Инфраструктура:</b>",
                f"VK-токены: active {credential_counts.get('vk:active', 0)} · "
                f"limited {credential_counts.get('vk:limited', 0)} · "
                f"dead {credential_counts.get('vk:dead', 0)}",
                f"Telegram-сессии: active {credential_counts.get('telegram:active', 0)} · "
                f"limited {credential_counts.get('telegram:limited', 0)} · "
                f"dead {credential_counts.get('telegram:dead', 0)}",
                f"Прокси: рабочие {proxy_counts.get('healthy', 0) + proxy_counts.get('degraded', 0)} · "
                f"карантин {proxy_counts.get('quarantine', 0)} · "
                f"удалено {proxy_counts.get('removed', 0)}",
            ]
        )
        return "\n".join(lines)

    async def send_daily_report_if_due(self) -> None:
        now_msk = datetime.now(MOSCOW)
        if now_msk.hour < self.settings.daily_report_hour_moscow:
            return
        report_date = now_msk.date() - timedelta(days=1)
        async with SessionFactory() as session:
            last_report = await SettingsRepository.get(session, "daily_report_last_date", "")
        if str(last_report) == report_date.isoformat():
            return
        text = await self._daily_stats(report_date)
        if not await self.alerts.send_admin(text):
            return
        async with SessionFactory() as session:
            async with session.begin():
                await SettingsRepository.set(session, "daily_report_last_date", report_date.isoformat())
        logger.info("daily_report_sent", report_date=report_date.isoformat())

    async def run(self) -> None:
        await self.bootstrap()
        try:
            while True:
                try:
                    await self.schedule()
                    self._tick += 1
                    if self._tick % max(1, 30 // self.settings.scheduler_tick_seconds) == 0:
                        await self.notify_dead_credentials()
                    if self._tick % max(1, 60 // self.settings.scheduler_tick_seconds) == 0:
                        await self.send_daily_report_if_due()
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
