from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot.keyboards import persistent_main_menu
from app.config import Settings
from app.db.repositories import AccessRepository
from app.db.session import SessionFactory


class AllowlistMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings):
        self.settings = settings
        self._reply_keyboard_chats: set[int] = set()

    async def _ensure_persistent_menu(self, event: TelegramObject) -> None:
        if not isinstance(event, Message) or event.chat.type != "private":
            return
        if event.chat.id in self._reply_keyboard_chats:
            return
        try:
            await event.answer(
                "Кнопка «Главное меню» закреплена снизу.",
                reply_markup=persistent_main_menu(),
            )
        except TelegramAPIError:
            return
        self._reply_keyboard_chats.add(event.chat.id)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Group/service messages must never trigger private allowlist warnings.
        # The only supported group interaction is the one-time /start bind_...
        # command; its handler performs the administrator and token checks.
        if isinstance(event, Message) and event.chat.type != "private":
            text = event.text or ""
            if text.startswith("/start") and "bind_" in text:
                return await handler(event, data)
            return None
        if isinstance(event, CallbackQuery):
            message = event.message
            if isinstance(message, Message) and message.chat.type != "private":
                return None

        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        async with SessionFactory() as session:
            allowed = await AccessRepository.is_allowed(session, user.id, self.settings.admin_telegram_id)
        if allowed:
            await self._ensure_persistent_menu(event)
            if (
                isinstance(event, CallbackQuery)
                and (event.data or "").startswith("admin:")
                and user.id != self.settings.admin_telegram_id
            ):
                await event.answer("Только для администратора", show_alert=True)
                return None
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("Доступ к боту не разрешён.")
        return None
