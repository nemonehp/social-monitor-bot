from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
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
from app.utils.text import h

logger = structlog.get_logger()


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
        kind = "НОВАЯ ИСТОРИЯ" if item.item_type == ItemType.STORY else "НОВЫЙ ПОСТ"
        platform = "VK" if item.platform.value == "vk" else "TELEGRAM"
        location = " · ".join(x for x in [source.region, source.federal_district] if x)
        parts = [f"<b>{platform} · {kind}</b>", "", f"<b>{h(source.title or source.normalized_link)}</b>"]
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
            caption = caption_text
            try:
                if row.media_type == "video":
                    message = await self.bot.send_video(
                        delivery.target_chat_id,
                        video=file,
                        caption=caption,
                        reply_markup=keyboard,
                        supports_streaming=True,
                    )
                elif row.media_type == "document":
                    message = await self.bot.send_document(
                        delivery.target_chat_id,
                        document=file,
                        caption=caption,
                        reply_markup=keyboard,
                    )
                else:
                    message = await self.bot.send_photo(
                        delivery.target_chat_id,
                        photo=file,
                        caption=caption,
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

        visual = [row for row in media if row.media_type in {"photo", "video", "video_preview", "link_preview"}]
        documents = [row for row in media if row not in visual]
        try:
            for compatible_group in (visual, documents):
                for start in range(0, len(compatible_group), 10):
                    chunk = compatible_group[start : start + 10]
                    group = [self._input_media(row) for row in chunk]
                    sent = await self.bot.send_media_group(delivery.target_chat_id, media=group)
                    message_ids.extend(message.message_id for message in sent)
        except TelegramBadRequest as exc:
            # A malformed/unsupported attachment must never suppress the information signal.
            logger.warning("delivery_media_fallback", delivery_id=delivery.id, error=str(exc))
        card = await self.bot.send_message(
            delivery.target_chat_id,
            text[:4096],
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        message_ids.append(card.message_id)
        return message_ids

    async def slot(self, slot: int) -> None:
        while True:
            delivery = None
            try:
                async with SessionFactory() as session:
                    async with session.begin():
                        delivery = await DeliveryRepository.claim(
                            session, lease_seconds=self.settings.job_lease_seconds
                        )
                if not delivery:
                    await asyncio.sleep(0.5)
                    continue
                message_ids = await self._send(delivery)
                async with SessionFactory() as session:
                    async with session.begin():
                        await DeliveryRepository.sent(session, delivery.id, message_ids)
                await self.alerts.send_stateful(
                    "signal_chat_unavailable",
                    active=False,
                    active_text="",
                    recovery_text="✅ Доставка в сигнальный чат восстановлена.",
                    payload={"chat_id": delivery.target_chat_id},
                )
                logger.info("delivery_sent", delivery_id=delivery.id, message_ids=message_ids)
            except TelegramRetryAfter as exc:
                if delivery:
                    async with SessionFactory() as session:
                        async with session.begin():
                            await DeliveryRepository.retry(
                                session,
                                delivery.id,
                                str(exc),
                                delay_seconds=int(exc.retry_after) + 1,
                            )
            except TelegramForbiddenError as exc:
                if delivery:
                    async with SessionFactory() as session:
                        async with session.begin():
                            await DeliveryRepository.retry(
                                session,
                                delivery.id,
                                str(exc),
                                delay_seconds=300,
                            )
                    await self.alerts.send_stateful(
                        "signal_chat_unavailable",
                        active=True,
                        active_text=(
                            "⚠️ <b>Сигнальный чат недоступен</b>\n\n"
                            "Проверьте, что бот добавлен в чат и может отправлять сообщения и медиа."
                        ),
                        recovery_text="",
                        payload={"chat_id": delivery.target_chat_id, "error": str(exc)},
                    )
                logger.warning("signal_chat_unavailable", delivery_id=getattr(delivery, "id", None), error=str(exc))
            except TelegramAPIError as exc:
                if delivery:
                    async with SessionFactory() as session:
                        async with session.begin():
                            final = delivery.attempts >= self.settings.max_job_attempts
                            await DeliveryRepository.retry(
                                session,
                                delivery.id,
                                str(exc),
                                delay_seconds=min(600, 2 ** min(9, delivery.attempts)),
                                final=final,
                            )
                logger.warning("delivery_api_error", delivery_id=getattr(delivery, "id", None), error=str(exc))
            except Exception as exc:
                logger.exception("delivery_unexpected", delivery_id=getattr(delivery, "id", None))
                if delivery:
                    async with SessionFactory() as session:
                        async with session.begin():
                            await DeliveryRepository.retry(session, delivery.id, str(exc), delay_seconds=30)

    async def run(self) -> None:
        try:
            await asyncio.gather(
                *(self.slot(i + 1) for i in range(self.settings.delivery_concurrency))
            )
        finally:
            await self.bot.session.close()
