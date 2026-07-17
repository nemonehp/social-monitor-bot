from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.config import Settings
from app.db.repositories import AlertRepository
from app.db.session import SessionFactory


class AlertService:
    def __init__(self, bot: Bot, settings: Settings):
        self.bot = bot
        self.settings = settings

    async def send_stateful(
        self,
        alert_key: str,
        *,
        active: bool,
        active_text: str,
        recovery_text: str,
        payload: dict,
    ) -> None:
        async with SessionFactory() as session:
            async with session.begin():
                should_send = await AlertRepository.should_send(
                    session,
                    alert_key,
                    active=active,
                    payload=payload,
                    cooldown_minutes=self.settings.alert_cooldown_minutes,
                )
                if not should_send:
                    return
        text = active_text if active else recovery_text
        try:
            await self.bot.send_message(self.settings.admin_telegram_id, text)
        except TelegramAPIError:
            return
        async with SessionFactory() as session:
            async with session.begin():
                await AlertRepository.mark_sent(session, alert_key)
