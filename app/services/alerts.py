from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.config import Settings
from app.db.models import Credential
from app.db.repositories import AlertRepository, CredentialRepository
from app.db.session import SessionFactory
from app.utils.platforms import platform_badge
from app.utils.text import h

MOSCOW = ZoneInfo("Europe/Moscow")


class AlertService:
    def __init__(self, bot: Bot, settings: Settings):
        self.bot = bot
        self.settings = settings

    async def send_admin(self, text: str) -> bool:
        try:
            await self.bot.send_message(self.settings.admin_telegram_id, text)
            return True
        except TelegramAPIError:
            return False

    async def send_stateful(
        self,
        alert_key: str,
        *,
        active: bool,
        active_text: str,
        payload: dict,
        recovery_text: str = "",
        cooldown_minutes: int | None = None,
        repeat_while_active: bool = False,
        send_recovery: bool = False,
    ) -> None:
        async with SessionFactory() as session:
            async with session.begin():
                should_send = await AlertRepository.should_send(
                    session,
                    alert_key,
                    active=active,
                    payload=payload,
                    cooldown_minutes=(
                        self.settings.alert_cooldown_minutes if cooldown_minutes is None else cooldown_minutes
                    ),
                    repeat_while_active=repeat_while_active,
                    send_recovery=send_recovery,
                )
                if not should_send:
                    return
        text = active_text if active else recovery_text
        if not text or not await self.send_admin(text):
            return
        async with SessionFactory() as session:
            async with session.begin():
                await AlertRepository.mark_sent(session, alert_key)

    @staticmethod
    def _dead_credential_text(credential: Credential) -> str:
        platform = platform_badge(credential.platform.value)
        when = credential.dead_since or datetime.now(UTC)
        when_msk = when.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")
        return (
            "🔴 <b>Аккаунт исключён из рабочего пула</b>\n\n"
            f"Платформа: {platform}\n"
            f"Аккаунт: <code>{h(credential.label)}</code>\n"
            f"Причина: {h(credential.last_error or 'авторизация отозвана')}\n"
            f"Время: {when_msk} МСК\n\n"
            "Автоматически этот аккаунт восстановлен не будет. Нужна замена токена или новая сессия."
        )

    async def send_dead_credential(self, credential: Credential) -> bool:
        sent = await self.send_admin(self._dead_credential_text(credential))
        if sent:
            async with SessionFactory() as session:
                async with session.begin():
                    await CredentialRepository.mark_dead_notified(session, credential.id)
        return sent
