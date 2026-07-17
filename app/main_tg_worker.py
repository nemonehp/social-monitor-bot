import asyncio

from app.config import get_settings
from app.logging import configure_logging
from app.workers.tg_worker import TgWorker


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    await TgWorker(settings).run()


if __name__ == "__main__":
    asyncio.run(main())
