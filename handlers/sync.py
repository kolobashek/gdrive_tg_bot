"""
Sync handlers — enable/disable auto-sync, manual sync trigger.
"""

import logging
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import config
from services.google_auth import auth_service
from services.scheduler import get_scheduler

router = Router()
logger = logging.getLogger(__name__)


def sync_menu_keyboard(user_id: int, bot: Bot) -> InlineKeyboardMarkup:
    sched = get_scheduler(bot)
    enabled = sched.is_enabled(user_id)
    toggle_text = "⏸ Остановить авто-синхронизацию" if enabled else "▶️ Запустить авто-синхронизацию"
    toggle_data = "sync_disable" if enabled else "sync_enable"
    status_text = "🟢 Авто-синхронизация: ВКЛ" if enabled else "🔴 Авто-синхронизация: ВЫКЛ"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=status_text, callback_data="sync_noop")],
        [InlineKeyboardButton(text=toggle_text, callback_data=toggle_data)],
        [InlineKeyboardButton(text="🔄 Синхронизировать сейчас", callback_data="sync_now")],
        [InlineKeyboardButton(text="ℹ️ Статус", callback_data="sync_status")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
    ])


@router.message(Command("sync"))
@router.callback_query(F.data == "sync_menu")
async def show_sync_menu(event: Message | CallbackQuery, bot: Bot):
    user_id = event.from_user.id
    interval_min = config.SYNC_INTERVAL_SECONDS // 60
    text = (
        f"🔄 <b>Синхронизация Drive → Telegram канал</b>\n\n"
        f"Интервал проверки: <b>{interval_min} мин</b>\n"
        f"Папка: <code>{config.SYNC_WATCH_FOLDER_ID}</code>\n"
        f"Канал: <code>{config.TELEGRAM_CHANNEL_ID or 'не настроен'}</code>\n\n"
        "Новые файлы из Drive автоматически публикуются в Telegram-канал."
    )
    kb = sync_menu_keyboard(user_id, bot)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "sync_enable")
async def enable_sync(call: CallbackQuery, bot: Bot):
    user_id = call.from_user.id
    creds = auth_service.get_credentials(user_id)
    if not creds:
        await call.answer("❌ Сначала авторизуйтесь через Google", show_alert=True)
        return
    if not config.TELEGRAM_CHANNEL_ID:
        await call.answer("❌ Укажите TELEGRAM_CHANNEL_ID в .env", show_alert=True)
        return
    sched = get_scheduler(bot)
    sched.enable_sync(user_id)
    await call.answer("✅ Авто-синхронизация включена!")
    await show_sync_menu(call, bot)


@router.callback_query(F.data == "sync_disable")
async def disable_sync(call: CallbackQuery, bot: Bot):
    sched = get_scheduler(bot)
    sched.disable_sync(call.from_user.id)
    await call.answer("⏸ Авто-синхронизация отключена.")
    await show_sync_menu(call, bot)


@router.callback_query(F.data == "sync_now")
async def sync_now(call: CallbackQuery, bot: Bot):
    user_id = call.from_user.id
    creds = auth_service.get_credentials(user_id)
    if not creds:
        await call.answer("❌ Нет авторизации", show_alert=True)
        return
    await call.answer("⏳ Запускаю синхронизацию...")
    sched = get_scheduler(bot)
    try:
        await sched._poll_user(user_id)
        await call.message.answer("✅ Синхронизация завершена. Новые файлы (если были) опубликованы в канале.")
    except Exception as e:
        logger.error(f"Manual sync error: {e}")
        await call.message.answer(f"❌ Ошибка синхронизации: {e}")


@router.callback_query(F.data == "sync_status")
async def sync_status(call: CallbackQuery, bot: Bot):
    user_id = call.from_user.id
    sched = get_scheduler(bot)
    enabled = sched.is_enabled(user_id)
    posted = len(sched._state.get("posted_file_ids", []))
    token = sched._state.get("page_tokens", {}).get(str(user_id), "нет")
    interval_min = config.SYNC_INTERVAL_SECONDS // 60

    text = (
        f"📊 <b>Статус синхронизации</b>\n\n"
        f"Статус: {'🟢 Активна' if enabled else '🔴 Отключена'}\n"
        f"Опубликовано файлов: <b>{posted}</b>\n"
        f"Интервал: <b>{interval_min} мин</b>\n"
        f"Drive page token: <code>{token[:20] if token != 'нет' else 'нет'}…</code>"
    )
    await call.answer()
    await call.message.answer(text, parse_mode="HTML")


@router.callback_query(F.data == "sync_noop")
async def sync_noop(call: CallbackQuery):
    await call.answer()
