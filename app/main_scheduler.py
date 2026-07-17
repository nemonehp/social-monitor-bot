import asyncio

from app.config import get_settings
from app.logging import configure_logging
from app.services.scheduler import Scheduler


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    await Scheduler(settings).run()


if __name__ == "__main__":
    asyncio.run(main())
