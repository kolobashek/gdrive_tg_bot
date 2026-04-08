"""
Google Drive & Photos Telegram Bot
Main entry point
"""

import asyncio
import logging
import traceback
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent, BotCommand

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

    @dp.errors()
    async def global_error_handler(event: ErrorEvent):
        exc = event.exception
        logger.error(f"Unhandled exception: {exc}\n{traceback.format_exc()}")

        # Try to notify the user
        update = event.update
        try:
            if update.message:
                await update.message.answer(
                    "⚠️ Произошла ошибка при обработке запроса. "
                    "Разработчики уже уведомлены."
                )
            elif update.callback_query:
                await update.callback_query.answer(
                    "⚠️ Ошибка при обработке запроса. Разработчики уведомлены.",
                    show_alert=True,
                )
        except Exception:
            pass
        return True

    # Register routers
    dp.include_router(common.router)
    dp.include_router(drive.router)
    dp.include_router(photos.router)
    dp.include_router(sync.router)

    # Start background scheduler
    scheduler = SyncScheduler(bot)
    asyncio.create_task(scheduler.run())

    await bot.set_my_commands([
        BotCommand(command="start",  description="Главное меню"),
        BotCommand(command="auth",   description="Авторизация Google"),
        BotCommand(command="logout", description="Выйти из Google"),
        BotCommand(command="drive",  description="Google Drive"),
        BotCommand(command="photos", description="Google Фото"),
        BotCommand(command="sync",   description="Синхронизация"),
        BotCommand(command="search", description="Поиск файлов"),
        BotCommand(command="help",   description="Помощь"),
    ])

    logger.info("Bot started")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
