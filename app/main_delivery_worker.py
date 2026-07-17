import asyncio

from app.config import get_settings
from app.logging import configure_logging
from app.services.delivery import DeliveryWorker


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    await DeliveryWorker(settings).run()


if __name__ == "__main__":
    asyncio.run(main())
