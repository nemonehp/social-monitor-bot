import asyncio

from app.config import get_settings
from app.logging import configure_logging
from app.workers.vk_worker import VkWorker


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    await VkWorker(settings).run()


if __name__ == "__main__":
    asyncio.run(main())
