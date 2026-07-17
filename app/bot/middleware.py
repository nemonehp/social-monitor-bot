from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import Settings
from app.db.repositories import AccessRepository
from app.db.session import SessionFactory


class AllowlistMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings):
        self.settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        async with SessionFactory() as session:
            allowed = await AccessRepository.is_allowed(
                session, user.id, self.settings.admin_telegram_id
            )
        if allowed:
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
