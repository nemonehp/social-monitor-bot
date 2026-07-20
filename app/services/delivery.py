from __future__ import annotations

import asyncio
import html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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
from app.db.enums import ItemType, Platform
from app.db.models import Delivery, Item, Media
from app.db.repositories import DeliveryRepository, SettingsRepository
from app.db.session import SessionFactory
from app.services.alerts import AlertService
from app.services.forum_topics import recreate_topic, topic_key
from app.services.media_cleanup import cleanup_item_media
from app.utils.platforms import platform_badge
from app.utils.text import h, split_text, vk_text_to_html

logger = structlog.get_logger()
MOSCOW = ZoneInfo("Europe/Moscow")


@asynccontextmanager
async def keep_delivery_leases(
    delivery_ids: list[int],
    lease_seconds: int,
) -> AsyncIterator[None]:
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
                        extended = await DeliveryRepository.extend_leases(session, ids, lease_seconds)
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
        self.bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.alerts = AlertService(self.bot, settings)

    @staticmethod
    def _published_text(item: Item) -> str:
        published = item.published_at or item.created_at or datetime.now(UTC)
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        return published.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")

    @staticmethod
    def _repost_line(item: Item) -> str:
        raw = getattr(item, "raw_json", None) or {}
        if item.platform == Platform.VK:
            repost = raw.get("monitor_repost") or {}
            if isinstance(repost, dict) and repost.get("is_repost"):
                title = h(repost.get("title") or "Оригинальная запись")
                url = str(repost.get("url") or item.original_url or "")
                safe_url = html.escape(url, quote=True)
                return (
                    f'<b>РЕПОСТ</b> · <a href="{safe_url}">{title}</a>' if url else f"<b>РЕПОСТ</b> · {title}"
                )
        if item.platform == Platform.TELEGRAM:
            forward = raw.get("monitor_forward") or {}
            if isinstance(forward, dict) and forward.get("is_forward"):
                title = h(forward.get("from_name") or "Исходное сообщение")
                return f"<b>РЕПОСТ</b> · {title}"
        return ""

    @classmethod
    def _header_lines(cls, delivery: Delivery) -> list[str]:
        item = delivery.item
        source = item.source
        kind = "ИСТОРИЯ" if item.item_type == ItemType.STORY else "ПОСТ"
        lines = [f"<b>{platform_badge(item.platform)} · {kind} · {cls._published_text(item)}</b>"]
        repost = cls._repost_line(item)
        if repost:
            lines.append(repost)
        title = source.title or source.normalized_link
        if title:
            safe_title = h(title)
            source_url = html.escape(
                getattr(item, "original_url", "") or source.normalized_link or "", quote=True
            )
            title_line = (
                f'<b><a href="{source_url}">{safe_title}</a></b>' if source_url else f"<b>{safe_title}</b>"
            )
            lines.extend(["", title_line])
        location = " · ".join(
            value
            for value in [
                getattr(source, "subcategory", "") or source.region,
                getattr(source, "category", "") or source.federal_district,
            ]
            if value
        )
        if location:
            lines.append(h(location))
        return lines

    @classmethod
    def _header(cls, delivery: Delivery) -> str:
        """Compatibility renderer used by diagnostics and older tests."""
        item = delivery.item
        header = "\n".join(cls._header_lines(delivery))
        text = cls._render_body(item, getattr(item, "text", "") or "")
        return "\n\n".join(part for part in [header, text] if part)

    @staticmethod
    def _render_body(item: Item, body: str) -> str:
        return vk_text_to_html(body) if item.platform == Platform.VK else h(body)

    @staticmethod
    def _content_counts(item: Item) -> dict[str, int]:
        value = (getattr(item, "raw_json", None) or {}).get("monitor_content_counts") or {}
        if not isinstance(value, dict):
            return {}
        result: dict[str, int] = {}
        for key, count in value.items():
            try:
                amount = int(count)
            except (TypeError, ValueError):
                continue
            if amount > 0:
                result[str(key)] = amount
        return result

    @classmethod
    def _content_notice(cls, item: Item, attached: list[Media]) -> str:
        counts = cls._content_counts(item)
        if not counts:
            return ""
        shown_photos = sum(
            1 for row in attached if row.media_type in {"photo", "link_preview", "document_preview"}
        )
        shown_videos = sum(1 for row in attached if row.media_type == "video_preview")
        parts: list[str] = []
        photo_count = counts.get("photo", 0)
        video_count = counts.get("video", 0)
        audio_count = counts.get("audio", 0)
        document_count = counts.get("doc", 0) + counts.get("document", 0)
        link_count = counts.get("link", 0)
        if video_count:
            suffix = f" (превью {shown_videos})" if shown_videos else " (без превью)"
            parts.append(f"{video_count} видео{suffix}")
        if photo_count > shown_photos:
            parts.append(f"{photo_count} фото (показано {shown_photos})")
        if audio_count:
            parts.append(f"{audio_count} аудио")
        if document_count:
            parts.append(f"{document_count} документов")
        if link_count and shown_photos == 0:
            parts.append(f"{link_count} ссылок")
        return f"<b>Контент:</b> {' · '.join(parts)}" if parts else ""

    @classmethod
    def _cards(cls, delivery: Delivery, attached: list[Media]) -> list[str]:
        item = delivery.item
        header = "\n".join(cls._header_lines(delivery))
        chunks = list(split_text(item.text, 3500)) if item.text else []
        notice = cls._content_notice(item, attached)
        if not chunks:
            return ["\n".join(part for part in [header, "", notice] if part)]
        cards: list[str] = []
        for index, chunk in enumerate(chunks):
            body = cls._render_body(item, chunk)
            quote = f"<blockquote expandable>{body}</blockquote>"
            if index == 0:
                cards.append("\n".join(part for part in [header, "", quote, "", notice] if part))
            else:
                cards.append(f"<b>Продолжение {index + 1}/{len(chunks)}</b>\n\n{quote}")
        return cards

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
        return [
            row
            for row in sorted(media, key=lambda value: value.position)
            if row.local_path and Path(row.local_path).is_file()
        ]

    @staticmethod
    def _input_media(row: Media):
        file = FSInputFile(row.local_path)
        if (
            row.media_type == "photo"
            or row.media_type.endswith("preview")
            or row.media_type == "link_preview"
        ):
            return InputMediaPhoto(media=file)
        if row.media_type == "video":
            return InputMediaVideo(media=file, supports_streaming=True)
        return InputMediaDocument(media=file)

    async def _thread_id(self, delivery: Delivery) -> int | None:
        if not hasattr(self, "settings"):
            return None
        async with SessionFactory() as session:
            mapping: dict[str, Any] = await SettingsRepository.get(session, "signal_topic_map", {}) or {}
        value = mapping.get(topic_key(delivery.item.platform, delivery.item.item_type))
        return int(value) if value is not None else None

    async def _send_cards(
        self,
        delivery: Delivery,
        cards: list[str],
        *,
        thread_id: int | None,
    ) -> list[int]:
        message_ids: list[int] = []
        keyboard = self._keyboard(delivery)
        for index, card in enumerate(cards):
            message = await self.bot.send_message(
                delivery.target_chat_id,
                card,
                message_thread_id=thread_id,
                reply_markup=keyboard if index == len(cards) - 1 else None,
                disable_web_page_preview=True,
            )
            message_ids.append(message.message_id)
        return message_ids

    async def _send(self, delivery: Delivery) -> list[int]:
        media = self._existing_media(delivery.item.media)
        cards = self._cards(delivery, media)
        thread_id = await self._thread_id(delivery)
        message_ids: list[int] = []
        # A single compact card fits safely into a Telegram media caption. Longer
        # text is deliberately sent once after media and never duplicated.
        compact_caption = cards[0] if len(cards) == 1 and len(cards[0]) <= 950 else None
        if media:
            try:
                for start in range(0, len(media), 10):
                    chunk = media[start : start + 10]
                    is_first = start == 0
                    caption = compact_caption if is_first else None
                    if len(chunk) == 1:
                        row = chunk[0]
                        file = FSInputFile(row.local_path)
                        reply_markup = self._keyboard(delivery) if caption else None
                        if row.media_type == "video":
                            message = await self.bot.send_video(
                                delivery.target_chat_id,
                                video=file,
                                supports_streaming=True,
                                message_thread_id=thread_id,
                                caption=caption,
                                reply_markup=reply_markup,
                            )
                        elif row.media_type in {"document", "audio"}:
                            message = await self.bot.send_document(
                                delivery.target_chat_id,
                                document=file,
                                message_thread_id=thread_id,
                                caption=caption,
                                reply_markup=reply_markup,
                            )
                        else:
                            message = await self.bot.send_photo(
                                delivery.target_chat_id,
                                photo=file,
                                message_thread_id=thread_id,
                                caption=caption,
                                reply_markup=reply_markup,
                            )
                        message_ids.append(message.message_id)
                    else:
                        media_group = [self._input_media(row) for row in chunk]
                        if caption:
                            media_group[0].caption = caption
                            media_group[0].parse_mode = ParseMode.HTML
                        sent = await self.bot.send_media_group(
                            delivery.target_chat_id,
                            media=media_group,
                            message_thread_id=thread_id,
                        )
                        message_ids.extend(message.message_id for message in sent)
            except TelegramBadRequest as exc:
                error = str(exc).lower()
                if thread_id and ("thread" in error or "topic" in error) and not message_ids:
                    key = topic_key(delivery.item.platform, delivery.item.item_type)
                    replacement = await recreate_topic(self.bot, delivery.target_chat_id, key)
                    if replacement:
                        return await self._send(delivery)
                logger.warning("delivery_media_fallback", delivery_id=delivery.id, error=str(exc))
        if compact_caption is None or not media:
            message_ids.extend(await self._send_cards(delivery, cards, thread_id=thread_id))
        return message_ids

    async def _process_delivery(self, delivery: Delivery, lease_ids: list[int]) -> tuple[bool, int]:
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
                    "Проверьте права бота на отправку сообщений, медиа и управление темами."
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
