from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from app.db.enums import ItemType, Platform
from app.db.repositories import SettingsRepository
from app.db.session import SessionFactory

TOPIC_SPECS = {
    "vk_post": ("🟢 VK · ПОСТЫ", 0x8EEE98),
    "vk_story": ("🟢 VK · ИСТОРИИ", 0x8EEE98),
    "telegram_post": ("🔵 TG · ПОСТЫ", 0x6FB9F0),
    "telegram_story": ("🔵 TG · ИСТОРИИ", 0x6FB9F0),
    "statistics": ("🟡 СТАТИСТИКА", 0xFFD67E),
}


@dataclass(frozen=True, slots=True)
class SignalChatSetup:
    ok: bool
    text: str
    chat_id: int | None = None
    topic_map: dict[str, int] | None = None


def topic_key(platform: Platform, item_type: ItemType) -> str:
    return f"{platform.value}_{item_type.value}"


async def configure_signal_chat(bot: Bot, chat_id: int) -> SignalChatSetup:
    try:
        chat = await bot.get_chat(chat_id)
    except TelegramAPIError as exc:
        return SignalChatSetup(False, f"Не удалось проверить чат: {exc}. Повторите подключение.")
    if chat.type == "group":
        return SignalChatSetup(
            False,
            "Эта группа ещё не является супергруппой. Преобразуйте её в супергруппу, "
            "включите «Темы», затем повторите подключение.",
        )
    if chat.type != "supergroup":
        return SignalChatSetup(False, "Подключить можно только супергруппу с включёнными темами.")
    if not bool(getattr(chat, "is_forum", False)):
        return SignalChatSetup(
            False,
            "Это супергруппа, но режим «Темы» выключен. Включите темы и повторите подключение.",
        )

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
    except TelegramAPIError as exc:
        return SignalChatSetup(
            False,
            f"Не удалось проверить права бота: {exc}. Выдайте права администратора и повторите подключение.",
        )
    if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
        return SignalChatSetup(False, "Назначьте бота администратором и повторите подключение.")
    if member.status == ChatMemberStatus.ADMINISTRATOR and not bool(
        getattr(member, "can_manage_topics", False)
    ):
        return SignalChatSetup(
            False,
            "Выдайте боту право «Управление темами» и повторите подключение.",
        )

    pending_key = f"signal_topic_pending:{chat_id}"
    async with SessionFactory() as session:
        existing_chat_id = await SettingsRepository.get(session, "signal_chat_id", None)
        existing = await SettingsRepository.get(session, "signal_topic_map", {})
        pending = await SettingsRepository.get(session, pending_key, {})
    seed = existing if existing_chat_id is not None and int(existing_chat_id) == chat_id else pending
    topic_map: dict[str, int] = {
        str(key): int(value) for key, value in (seed or {}).items() if str(value).isdigit()
    }
    # Bot API cannot enumerate all forum topics. Persist every newly created ID
    # immediately in a chat-specific staging map, so a partial API failure can
    # be retried without duplicating the topics already created in this attempt.
    for key, (name, color) in TOPIC_SPECS.items():
        if topic_map.get(key):
            continue
        try:
            topic = await bot.create_forum_topic(chat_id, name=name, icon_color=color)
        except TelegramAPIError as exc:
            return SignalChatSetup(
                False,
                "Не удалось создать все служебные темы. Уже созданные темы сохранены; "
                f"исправьте права/настройки и повторите подключение. Ошибка Telegram: {exc}",
                chat_id=chat_id,
                topic_map=topic_map,
            )
        topic_map[key] = topic.message_thread_id
        async with SessionFactory() as session:
            async with session.begin():
                await SettingsRepository.set(session, pending_key, topic_map)

    async with SessionFactory() as session:
        async with session.begin():
            await SettingsRepository.set(session, "signal_chat_id", chat_id)
            await SettingsRepository.set(session, "signal_topic_map", topic_map)
            await SettingsRepository.set(session, pending_key, {})
            await SettingsRepository.set(session, "signal_bind_token", "")
    return SignalChatSetup(
        True,
        "✅ Сигнальный чат подключён. Темы созданы, обычная переписка не изменена.",
        chat_id=chat_id,
        topic_map=topic_map,
    )


async def resolve_topic_id(platform: Platform, item_type: ItemType) -> int | None:
    async with SessionFactory() as session:
        mapping: dict[str, Any] = await SettingsRepository.get(session, "signal_topic_map", {}) or {}
    value = mapping.get(topic_key(platform, item_type))
    return int(value) if value is not None else None


async def recreate_topic(bot: Bot, chat_id: int, key: str) -> int | None:
    spec = TOPIC_SPECS.get(key)
    if spec is None:
        return None
    name, color = spec
    try:
        topic = await bot.create_forum_topic(chat_id, name=name, icon_color=color)
    except (TelegramBadRequest, TelegramAPIError):
        return None
    async with SessionFactory() as session:
        async with session.begin():
            mapping = await SettingsRepository.get(session, "signal_topic_map", {}) or {}
            mapping[str(key)] = topic.message_thread_id
            await SettingsRepository.set(session, "signal_topic_map", mapping)
    return topic.message_thread_id
