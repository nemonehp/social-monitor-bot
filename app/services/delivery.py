from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

from app.config import Settings
from app.db.enums import ItemType
from app.db.models import Delivery, Media
from app.db.repositories import DeliveryRepository
from app.db.session import SessionFactory
from app.services.alerts import AlertService
from app.services.media_cleanup import cleanup_item_media
from app.utils.platforms import platform_badge
from app.utils.text import h

logger = structlog.get_logger()
MOSCOW = ZoneInfo("Europe/Moscow")


@asynccontextmanager
async def keep_delivery_leases(
    delivery_ids: list[int],
    lease_seconds: int,
) -> AsyncIterator[None]:
    """Keep a claimed source batch private while Telegram is sending it.

    A temporary database hiccup must not turn an already accepted Telegram send
    into an exception followed by a duplicate retry. Keep retrying the heartbeat,
    log lease problems, and let the normal delivery transaction decide the result.
    """
    ids = list(dict.fromkeys(delivery_ids))
    interval = max(5, min(60, lease_seconds // 3))
    stopped = asyncio.Event()

    async def heartbeat() -> None:
        while True:
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                async with SessionFactory() as session:
                    async with session.begin():
                        extended = await DeliveryRepository.extend_leases(
                            session,
                            ids,
                            lease_seconds,
                        )
                if extended == 0:
                    logger.warning("delivery_lease_lost", delivery_ids=ids)
                    return
            except Exception:
                logger.exception("delivery_lease_heartbeat_failed", delivery_ids=ids)

    task = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        stopped.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class DeliveryWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.bot = Bot(
            settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.alerts = AlertService(self.bot, settings)

    @staticmethod
    def _safe_body(value: str, limit: int) -> str:
        escaped = h(value)
        if len(escaped) <= limit:
            return escaped
        truncated = escaped[: max(1, limit - 1)]
        if truncated.rfind("&") > truncated.rfind(";"):
            truncated = truncated[: truncated.rfind("&")]
        return truncated + "…"

    @classmethod
    def _header(cls, delivery: Delivery, body_limit: int = 3000) -> str:
        item = delivery.item
        source = item.source
        kind = "ИСТОРИЯ" if item.item_type == ItemType.STORY else "ПОСТ"
        platform_label = platform_badge(item.platform)
        published = item.published_at or item.created_at or datetime.now(UTC)
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        published_text = published.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")
        parts = [f"<b>{platform_label} · {kind} · {published_text}</b>"]
        title = source.title or source.normalized_link
        if title:
            parts.extend(["", f"<b>{h(title)}</b>"])
        location = " · ".join(
            value
            for value in [
                getattr(source, "subcategory", "") or source.region,
                getattr(source, "category", "") or source.federal_district,
            ]
            if value
        )
        if location:
            parts.append(h(location))
        if item.text:
            parts.extend(["", cls._safe_body(item.text, body_limit)])
        return "\n".join(parts)

    @staticmethod
    def _keyboard(delivery: Delivery) -> InlineKeyboardMarkup | None:
        url = delivery.item.original_url
        if not url:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Открыть оригинал", url=url)]]
        )

    @staticmethod
    def _existing_media(media: list[Media]) -> list[Media]:
        result: list[Media] = []
        for row in sorted(media, key=lambda x: x.position):
            if row.local_path and Path(row.local_path).is_file():
                result.append(row)
        return result

    @staticmethod
    def _input_media(row: Media):
        file = FSInputFile(row.local_path)
        if row.media_type == "photo" or row.media_type.endswith("preview") or row.media_type == "link_preview":
            return InputMediaPhoto(media=file)
        if row.media_type == "video":
            return InputMediaVideo(media=file, supports_streaming=True)
        return InputMediaDocument(media=file)

    async def _send(self, delivery: Delivery) -> list[int]:
        text = self._header(delivery, body_limit=3000)
        keyboard = self._keyboard(delivery)
        media = self._existing_media(delivery.item.media)
        message_ids: list[int] = []

        if not media:
            message = await self.bot.send_message(
                delivery.target_chat_id,
                text[:4096],
                reply_markup=keyboard,
                disable_web_page_preview=False,
            )
            return [message.message_id]

        if len(media) == 1:
            row = media[0]
            file = FSInputFile(row.local_path)
            # Telegram captions are limited to 1024 characters. When the card is
            # longer, send the media without an excerpt and then send the full card
            # exactly once. The old behaviour repeated the beginning of every long
            # media post in both the caption and the following message.
            fits_caption = len(text) <= 1000
            caption = text if fits_caption else None
            media_keyboard = keyboard if fits_caption else None
            try:
                if row.media_type == "video":
                    message = await self.bot.send_video(
                        delivery.target_chat_id,
                        video=file,
                        caption=caption,
                        reply_markup=media_keyboard,
                        supports_streaming=True,
                    )
                elif row.media_type == "document":
                    message = await self.bot.send_document(
                        delivery.target_chat_id,
                        document=file,
                        caption=caption,
                        reply_markup=media_keyboard,
                    )
                else:
                    message = await self.bot.send_photo(
                        delivery.target_chat_id,
                        photo=file,
                        caption=caption,
                        reply_markup=media_keyboard,
                    )
                message_ids.append(message.message_id)
                if not fits_caption:
                    card = await self.bot.send_message(
                        delivery.target_chat_id,
                        text[:4096],
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
                    message_ids.append(card.message_id)
                return message_ids
            except TelegramBadRequest as exc:
                logger.warning("delivery_single_media_fallback", delivery_id=delivery.id, error=str(exc))
                message = await self.bot.send_message(
                    delivery.target_chat_id,
                    text[:4096],
                    reply_markup=keyboard,
                    disable_web_page_preview=False,
                )
                return [message.message_id]

        visual = [
            row
            for row in media
            if row.media_type in {"photo", "video", "video_preview", "document_preview", "link_preview"}
        ]
        documents = [row for row in media if row not in visual]
        try:
            for compatible_group in (visual, documents):
                for start in range(0, len(compatible_group), 10):
                    chunk = compatible_group[start : start + 10]
                    group = [self._input_media(row) for row in chunk]
                    sent = await self.bot.send_media_group(delivery.target_chat_id, media=group)
                    message_ids.extend(message.message_id for message in sent)
        except TelegramBadRequest as exc:
            logger.warning("delivery_media_fallback", delivery_id=delivery.id, error=str(exc))
        card = await self.bot.send_message(
            delivery.target_chat_id,
            text[:4096],
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        message_ids.append(card.message_id)
        return message_ids

    async def _process_delivery(
        self,
        delivery: Delivery,
        lease_ids: list[int],
    ) -> tuple[bool, int]:
        try:
            async with SessionFactory() as session:
                async with session.begin():
                    attempts = await DeliveryRepository.start_attempt(session, delivery.id)
            if attempts is None:
                logger.warning("delivery_lease_lost_before_send", delivery_id=delivery.id)
                return False, 0
            delivery.attempts = attempts
            async with keep_delivery_leases(lease_ids, self.settings.job_lease_seconds):
                message_ids = await self._send(delivery)
            async with SessionFactory() as session:
                async with session.begin():
                    await DeliveryRepository.sent(session, delivery.id, message_ids)
            if self.settings.media_delete_after_delivery:
                removed = await cleanup_item_media(delivery.item_id)
                if removed:
                    logger.info("delivery_media_deleted", item_id=delivery.item_id, removed=removed)
            await self.alerts.send_stateful(
                "signal_chat_unavailable",
                active=False,
                active_text="",
                payload={"chat_id": delivery.target_chat_id},
            )
            logger.info("delivery_sent", delivery_id=delivery.id, message_ids=message_ids)
            return True, 0
        except TelegramRetryAfter as exc:
            delay = int(exc.retry_after) + 1
            async with SessionFactory() as session:
                async with session.begin():
                    await DeliveryRepository.retry(session, delivery.id, str(exc), delay_seconds=delay)
            return False, delay
        except TelegramForbiddenError as exc:
            async with SessionFactory() as session:
                async with session.begin():
                    await DeliveryRepository.retry(session, delivery.id, str(exc), delay_seconds=300)
            await self.alerts.send_stateful(
                "signal_chat_unavailable",
                active=True,
                active_text=(
                    "⚠️ <b>Сигнальный чат недоступен</b>\n\n"
                    "Проверьте, что бот добавлен в чат и может отправлять сообщения и медиа."
                ),
                payload={"chat_id": delivery.target_chat_id, "error": str(exc)},
                repeat_while_active=True,
                cooldown_minutes=self.settings.health_alert_repeat_minutes,
            )
            logger.warning("signal_chat_unavailable", delivery_id=delivery.id, error=str(exc))
            return False, 300
        except TelegramAPIError as exc:
            final = delivery.attempts >= self.settings.max_job_attempts
            delay = min(600, 2 ** min(9, delivery.attempts))
            async with SessionFactory() as session:
                async with session.begin():
                    await DeliveryRepository.retry(
                        session,
                        delivery.id,
                        str(exc),
                        delay_seconds=delay,
                        final=final,
                    )
            logger.warning("delivery_api_error", delivery_id=delivery.id, error=str(exc))
            return False, delay
        except Exception as exc:
            logger.exception("delivery_unexpected", delivery_id=delivery.id)
            async with SessionFactory() as session:
                async with session.begin():
                    await DeliveryRepository.retry(session, delivery.id, str(exc), delay_seconds=30)
            return False, 30

    async def run(self) -> None:
        if self.settings.delivery_concurrency != 1:
            logger.warning(
                "delivery_concurrency_forced_to_one",
                configured=self.settings.delivery_concurrency,
                reason="source batches must never interleave",
            )
        try:
            while True:
                async with SessionFactory() as session:
                    async with session.begin():
                        batch = await DeliveryRepository.claim_batch(
                            session,
                            lease_seconds=self.settings.job_lease_seconds,
                            batch_size=self.settings.delivery_batch_size,
                        )
                if not batch:
                    await asyncio.sleep(0.5)
                    continue
                logger.info(
                    "delivery_batch_claimed",
                    source_id=batch[0].item.source_id,
                    target_chat_id=batch[0].target_chat_id,
                    size=len(batch),
                )
                for index, delivery in enumerate(batch):
                    lease_ids = [row.id for row in batch[index:]]
                    success, delay = await self._process_delivery(delivery, lease_ids)
                    if success:
                        continue
                    remaining = [row.id for row in batch[index + 1 :]]
                    if remaining:
                        async with SessionFactory() as session:
                            async with session.begin():
                                await DeliveryRepository.release(session, remaining, delay_seconds=delay)
                    break
        finally:
            await self.bot.session.close()
