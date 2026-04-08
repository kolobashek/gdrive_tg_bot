"""
Sync handlers — enable/disable auto-sync, manual sync trigger, channel selection.
"""

import logging
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import config
from services.google_auth import auth_service
from services.scheduler import get_scheduler, SEND_MODE_COMPRESSED, SEND_MODE_FILE

router = Router()
logger = logging.getLogger(__name__)


class SyncStates(StatesGroup):
    waiting_for_channel_forward = State()
    waiting_for_max_files = State()


def sync_menu_keyboard(user_id: int, bot: Bot) -> InlineKeyboardMarkup:
    sched = get_scheduler(bot)
    enabled = sched.is_enabled(user_id)
    toggle_text = "⏸ Остановить" if enabled else "▶️ Запустить авто-синхронизацию"
    toggle_data = "sync_disable" if enabled else "sync_enable"
    status_text = "🟢 Авто-синхронизация: ВКЛ" if enabled else "🔴 Авто-синхронизация: ВЫКЛ"

    mode = sched.get_send_mode(user_id)
    mode_text = "📎 Режим: файлы (оригинал)" if mode == SEND_MODE_FILE else "🖼 Режим: сжатые фото/видео"

    max_f = sched.get_max_files(user_id)
    limit_text = f"🔢 Лимит: {max_f} файлов" if max_f else "🔢 Лимит: без ограничений"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=status_text, callback_data="sync_noop")],
        [InlineKeyboardButton(text=toggle_text, callback_data=toggle_data)],
        [InlineKeyboardButton(text="📡 Выбрать канал", callback_data="sync_set_channel")],
        [InlineKeyboardButton(text=mode_text, callback_data="sync_toggle_mode")],
        [InlineKeyboardButton(text=limit_text, callback_data="sync_set_limit")],
        [InlineKeyboardButton(text="🔄 Синхронизировать сейчас", callback_data="sync_now")],
        [InlineKeyboardButton(text="ℹ️ Статус", callback_data="sync_status")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
    ])


@router.message(Command("sync"))
@router.callback_query(F.data == "sync_menu")
async def show_sync_menu(event: Message | CallbackQuery, bot: Bot):
    user_id = event.from_user.id
    sched = get_scheduler(bot)
    channel = sched.get_channel(user_id)

    if channel:
        channel_display = channel.get("title") or str(channel["id"])
        channel_str = f"<b>{channel_display}</b> (<code>{channel['id']}</code>)"
    else:
        channel_str = "<i>не выбран</i>"

    interval_min = config.SYNC_INTERVAL_SECONDS // 60
    text = (
        f"🔄 <b>Синхронизация Drive → Telegram канал</b>\n\n"
        f"Интервал проверки: <b>{interval_min} мин</b>\n"
        f"Папка Drive: <code>{config.SYNC_WATCH_FOLDER_ID}</code>\n"
        f"Канал: {channel_str}\n\n"
        "Новые файлы из Drive автоматически публикуются в Telegram-канал."
    )
    kb = sync_menu_keyboard(user_id, bot)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


# ─── Channel selection via forwarded message ──────────────────────────────────

@router.callback_query(F.data == "sync_set_channel")
async def sync_set_channel_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(SyncStates.waiting_for_channel_forward)
    await call.message.edit_text(
        "📡 <b>Выбор канала для синхронизации</b>\n\n"
        "Перешлите любое сообщение из нужного канала.\n\n"
        "<i>Бот должен быть администратором в канале, "
        "чтобы публиковать файлы.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="sync_menu")
        ]]),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(SyncStates.waiting_for_channel_forward)
async def handle_channel_forward(message: Message, state: FSMContext, bot: Bot):
    await state.clear()

    # Check forwarded message from a channel
    fwd_chat = message.forward_from_chat
    if not fwd_chat:
        await message.answer(
            "❌ Это не пересланное сообщение из канала. Попробуйте ещё раз через /sync → 📡 Выбрать канал."
        )
        return

    if fwd_chat.type not in ("channel", "supergroup"):
        await message.answer(
            f"❌ Ожидается канал, но получен тип: <code>{fwd_chat.type}</code>.\n"
            "Перешлите сообщение из Telegram-канала.",
            parse_mode="HTML",
        )
        return

    channel_id = fwd_chat.id
    title = fwd_chat.title or str(channel_id)
    username = f" @{fwd_chat.username}" if fwd_chat.username else ""

    sched = get_scheduler(bot)
    sched.set_channel(message.from_user.id, channel_id, title)

    await message.answer(
        f"✅ <b>Канал установлен:</b>\n"
        f"{title}{username}\n"
        f"ID: <code>{channel_id}</code>\n\n"
        "Теперь можно запустить авто-синхронизацию.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Меню синхронизации", callback_data="sync_menu")
        ]]),
    )


# ─── Enable / Disable ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "sync_enable")
async def enable_sync(call: CallbackQuery, bot: Bot):
    user_id = call.from_user.id
    creds = auth_service.get_credentials(user_id)
    if not creds:
        await call.answer("❌ Сначала авторизуйтесь через Google", show_alert=True)
        return
    sched = get_scheduler(bot)
    if not sched.get_channel_id(user_id):
        await call.answer("❌ Сначала выберите канал (📡 Выбрать канал)", show_alert=True)
        return
    sched.enable_sync(user_id)
    await call.answer("✅ Авто-синхронизация включена!")
    await show_sync_menu(call, bot)


@router.callback_query(F.data == "sync_disable")
async def disable_sync(call: CallbackQuery, bot: Bot):
    sched = get_scheduler(bot)
    sched.disable_sync(call.from_user.id)
    await call.answer("⏸ Авто-синхронизация отключена.")
    await show_sync_menu(call, bot)


# ─── Manual sync / Status ─────────────────────────────────────────────────────

@router.callback_query(F.data == "sync_now")
async def sync_now(call: CallbackQuery, bot: Bot):
    user_id = call.from_user.id
    creds = auth_service.get_credentials(user_id)
    if not creds:
        await call.answer("❌ Нет авторизации", show_alert=True)
        return
    sched = get_scheduler(bot)
    if not sched.get_channel_id(user_id):
        await call.answer("❌ Сначала выберите канал (📡 Выбрать канал)", show_alert=True)
        return
    await call.answer("⏳ Запускаю синхронизацию...")
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
    channel = sched.get_channel(user_id)
    posted = len(sched._state.get("posted_file_ids", []))
    token = sched._state.get("page_tokens", {}).get(str(user_id), "нет")
    interval_min = config.SYNC_INTERVAL_SECONDS // 60

    if channel:
        ch_str = channel.get("title") or str(channel["id"])
    else:
        ch_str = "не выбран"

    text = (
        f"📊 <b>Статус синхронизации</b>\n\n"
        f"Статус: {'🟢 Активна' if enabled else '🔴 Отключена'}\n"
        f"Канал: <b>{ch_str}</b>\n"
        f"Опубликовано файлов: <b>{posted}</b>\n"
        f"Интервал: <b>{interval_min} мин</b>\n"
        f"Drive page token: <code>{'…' if token == 'нет' else token[:20] + '…'}</code>"
    )
    await call.answer()
    await call.message.answer(text, parse_mode="HTML")


@router.callback_query(F.data == "sync_toggle_mode")
async def sync_toggle_mode(call: CallbackQuery, bot: Bot):
    sched = get_scheduler(bot)
    current = sched.get_send_mode(call.from_user.id)
    new_mode = SEND_MODE_FILE if current == SEND_MODE_COMPRESSED else SEND_MODE_COMPRESSED
    sched.set_send_mode(call.from_user.id, new_mode)
    label = "файлы (оригинал)" if new_mode == SEND_MODE_FILE else "сжатые фото/видео"
    await call.answer(f"✅ Режим: {label}")
    await show_sync_menu(call, bot)


@router.callback_query(F.data == "sync_set_limit")
async def sync_set_limit(call: CallbackQuery, state: FSMContext, bot: Bot):
    sched = get_scheduler(bot)
    current = sched.get_max_files(call.from_user.id)
    buttons = [
        [InlineKeyboardButton(text="1 файл (тест)", callback_data="sync_limit:1")],
        [InlineKeyboardButton(text="5 файлов", callback_data="sync_limit:5")],
        [InlineKeyboardButton(text="10 файлов", callback_data="sync_limit:10")],
        [InlineKeyboardButton(text="50 файлов", callback_data="sync_limit:50")],
        [InlineKeyboardButton(text="∞ Без ограничений", callback_data="sync_limit:0")],
        [InlineKeyboardButton(text="✏️ Своё число", callback_data="sync_limit:custom")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="sync_menu")],
    ]
    current_str = str(current) if current else "без ограничений"
    await call.message.edit_text(
        f"🔢 <b>Лимит файлов за одну синхронизацию</b>\n\n"
        f"Сейчас: <b>{current_str}</b>\n\n"
        "Удобно для тестирования — обработает не более N новых файлов за раз.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("sync_limit:"))
async def sync_apply_limit(call: CallbackQuery, state: FSMContext, bot: Bot):
    value = call.data.split(":")[1]
    if value == "custom":
        await state.set_state(SyncStates.waiting_for_max_files)
        await call.message.edit_text(
            "✏️ Введите число файлов (например, 3):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена", callback_data="sync_menu")
            ]]),
        )
        await call.answer()
        return
    sched = get_scheduler(bot)
    max_f = int(value) if value != "0" else None
    sched.set_max_files(call.from_user.id, max_f)
    label = str(max_f) if max_f else "без ограничений"
    await call.answer(f"✅ Лимит: {label}")
    await show_sync_menu(call, bot)


@router.message(SyncStates.waiting_for_max_files)
async def sync_custom_limit(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    text = message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await message.answer("❌ Введите целое число больше 0.")
        return
    sched = get_scheduler(bot)
    sched.set_max_files(message.from_user.id, int(text))
    await message.answer(
        f"✅ Лимит установлен: <b>{text} файлов</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Меню синхронизации", callback_data="sync_menu")
        ]]),
    )


@router.callback_query(F.data == "sync_noop")
async def sync_noop(call: CallbackQuery):
    await call.answer()
