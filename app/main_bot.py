from __future__ import annotations

import asyncio

from app.bot.app import build_bot
from app.config import get_settings
from app.db.repositories import AccessRepository
from app.db.session import SessionFactory
from app.logging import configure_logging


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    async with SessionFactory() as session:
        async with session.begin():
            await AccessRepository.ensure_admin(session, settings.admin_telegram_id)
    bot, dispatcher = build_bot(settings)
    try:
        await dispatcher.start_polling(bot, settings=settings)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
