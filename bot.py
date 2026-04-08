"""
Google Drive & Photos Telegram Bot
Main entry point
"""

import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from handlers import drive, photos, sync, common
from services.scheduler import SyncScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    bot = Bot(token=config.TELEGRAM_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Register routers
    dp.include_router(common.router)
    dp.include_router(drive.router)
    dp.include_router(photos.router)
    dp.include_router(sync.router)

    # Start background scheduler
    scheduler = SyncScheduler(bot)
    asyncio.create_task(scheduler.run())

    logger.info("Bot started")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
