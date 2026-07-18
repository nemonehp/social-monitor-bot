from __future__ import annotations

import asyncio
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
from app.utils.text import h

logger = structlog.get_logger()
MOSCOW = ZoneInfo("Europe/Moscow")


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
        platform_icon = "🟦" if item.platform.value == "vk" else "✈️"
        published = item.published_at or item.created_at or datetime.now(UTC)
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        published_text = published.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")
        parts = [f"<b>{platform_icon} · {kind} · {published_text}</b>"]
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
        caption_text = self._header(delivery, body_limit=650)
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
            try:
                if row.media_type == "video":
                    message = await self.bot.send_video(
                        delivery.target_chat_id,
                        video=file,
                        caption=caption_text,
                        reply_markup=keyboard,
                        supports_streaming=True,
                    )
                elif row.media_type == "document":
                    message = await self.bot.send_document(
                        delivery.target_chat_id,
                        document=file,
                        caption=caption_text,
                        reply_markup=keyboard,
                    )
                else:
                    message = await self.bot.send_photo(
                        delivery.target_chat_id,
                        photo=file,
                        caption=caption_text,
                        reply_markup=keyboard,
                    )
                message_ids.append(message.message_id)
                if len(text) > len(caption_text):
                    extra = await self.bot.send_message(
                        delivery.target_chat_id,
                        text[:4096],
                        reply_markup=keyboard,
                    )
                    message_ids.append(extra.message_id)
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

    async def _process_delivery(self, delivery: Delivery) -> tuple[bool, int]:
        try:
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
                    success, delay = await self._process_delivery(delivery)
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
