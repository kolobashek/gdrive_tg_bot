"""
Sync handlers — multiple channels, per-channel settings.
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


MIME_FILTER_LABELS = {
    None:       "🗂 Все типы",
    "image":    "🖼 Только фото",
    "video":    "🎬 Только видео",
    "audio":    "🎵 Только аудио",
    "document": "📄 Только документы",
}

SEND_MODE_LABELS = {
    SEND_MODE_COMPRESSED: "🖼 Сжатые медиа",
    SEND_MODE_FILE:       "📎 Файлы (оригинал)",
}


# ─── Main sync menu ───────────────────────────────────────────────────────────

@router.message(Command("sync"))
@router.callback_query(F.data == "sync_menu")
async def show_sync_menu(event: Message | CallbackQuery, bot: Bot):
    user_id = event.from_user.id
    sched = get_scheduler(bot)
    enabled = sched.is_enabled(user_id)
    channels = sched.get_channels(user_id)

    status = "🟢 Авто-синхронизация: ВКЛ" if enabled else "🔴 Авто-синхронизация: ВЫКЛ"
    ch_count = len(channels)
    text = (
        f"🔄 <b>Синхронизация Drive → Telegram</b>\n\n"
        f"{status}\n"
        f"Каналов настроено: <b>{ch_count}</b>"
    )

    toggle_text = "⏸ Остановить" if enabled else "▶️ Запустить"
    toggle_cb   = "sync_disable" if enabled else "sync_enable"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=toggle_cb)],
        [InlineKeyboardButton(text="📡 Каналы", callback_data="sync_channels")],
        [InlineKeyboardButton(text="🔄 Синхронизировать сейчас", callback_data="sync_now")],
        [InlineKeyboardButton(text="ℹ️ Статус", callback_data="sync_status")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
    ])

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


# ─── Channel list ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "sync_channels")
async def show_channels(call: CallbackQuery, bot: Bot):
    sched = get_scheduler(bot)
    channels = sched.get_channels(call.from_user.id)
    buttons = []
    for key, ch in channels.items():
        mime_label = MIME_FILTER_LABELS.get(ch.mime_filter, ch.mime_filter)
        mode_label = SEND_MODE_LABELS.get(ch.send_mode, ch.send_mode)
        buttons.append([InlineKeyboardButton(
            text=f"📡 {ch.title}  |  {mime_label}  |  {mode_label}",
            callback_data=f"sync_ch:{key}",
        )])
    buttons.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="sync_add_channel")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="sync_menu")])
    text = "📡 <b>Ваши каналы для синхронизации</b>\n\nНажмите на канал для настройки."
    if not channels:
        text = "📡 <b>Каналы не настроены</b>\n\nДобавьте первый канал."
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await call.answer()


# ─── Add channel ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "sync_add_channel")
async def add_channel_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(SyncStates.waiting_for_channel_forward)
    await state.update_data(editing_key=None)
    await call.message.edit_text(
        "📡 <b>Добавление канала</b>\n\n"
        "Перешлите любое сообщение из нужного канала.\n\n"
        "<i>Бот должен быть администратором в этом канале.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="sync_channels")
        ]]),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(SyncStates.waiting_for_channel_forward)
async def handle_channel_forward(message: Message, state: FSMContext, bot: Bot):
    fwd_chat = message.forward_from_chat
    if not fwd_chat or fwd_chat.type not in ("channel", "supergroup"):
        await message.answer(
            "❌ Перешлите сообщение из Telegram-канала (не из чата).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="↩️ К каналам", callback_data="sync_channels")
            ]]),
        )
        return

    data = await state.get_data()
    await state.clear()

    sched = get_scheduler(bot)
    editing_key = data.get("editing_key")
    channel_id = fwd_chat.id
    title = fwd_chat.title or str(channel_id)
    username = f" @{fwd_chat.username}" if fwd_chat.username else ""

    if editing_key:
        sched.update_channel(message.from_user.id, editing_key, id=channel_id, title=title)
        action = "обновлён"
    else:
        editing_key = sched.add_channel(message.from_user.id, channel_id, title)
        action = "добавлен"

    await message.answer(
        f"✅ Канал {action}:\n<b>{title}</b>{username}\nID: <code>{channel_id}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚙️ Настроить", callback_data=f"sync_ch:{editing_key}"),
            InlineKeyboardButton(text="📡 Все каналы", callback_data="sync_channels"),
        ]]),
    )


# ─── Channel settings ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sync_ch:"))
async def channel_settings(call: CallbackQuery, bot: Bot):
    ch_key = call.data.split(":", 1)[1]
    sched = get_scheduler(bot)
    ch = sched.get_channel(call.from_user.id, ch_key)
    if not ch:
        await call.answer("Канал не найден", show_alert=True)
        return

    folder_str = ch.folder_id if ch.folder_id != "root" else "вся Drive"
    mime_label = MIME_FILTER_LABELS.get(ch.mime_filter, ch.mime_filter)
    mode_label = SEND_MODE_LABELS.get(ch.send_mode, ch.send_mode)
    limit_str = str(ch.max_files) if ch.max_files else "∞"

    text = (
        f"📡 <b>{ch.title}</b>\n\n"
        f"ID: <code>{ch.channel_id}</code>\n"
        f"Папка Drive: <code>{folder_str}</code>\n"
        f"Фильтр типов: {mime_label}\n"
        f"Режим отправки: {mode_label}\n"
        f"Лимит за сессию: {limit_str} файлов"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 Папка Drive", callback_data=f"sync_ch_folder:{ch_key}")],
        [InlineKeyboardButton(text="🎛 Фильтр типов", callback_data=f"sync_ch_mime:{ch_key}")],
        [InlineKeyboardButton(text="📤 Режим отправки", callback_data=f"sync_ch_mode:{ch_key}")],
        [InlineKeyboardButton(text="🔢 Лимит файлов", callback_data=f"sync_ch_limit:{ch_key}")],
        [InlineKeyboardButton(text="🔄 Синхр. сейчас", callback_data=f"sync_ch_now:{ch_key}")],
        [InlineKeyboardButton(text="🗑 Удалить канал", callback_data=f"sync_ch_del:{ch_key}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="sync_channels")],
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ─── Folder ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sync_ch_folder:"))
async def ch_folder_menu(call: CallbackQuery, bot: Bot):
    ch_key = call.data.split(":", 1)[1]
    sched = get_scheduler(bot)
    ch = sched.get_channel(call.from_user.id, ch_key)
    if not ch:
        await call.answer("Канал не найден", show_alert=True)
        return

    # List user's Drive folders to pick from
    creds = auth_service.get_credentials(call.from_user.id)
    if not creds:
        await call.answer("Нет авторизации", show_alert=True)
        return

    from services.google_drive import GoogleDriveService
    drive = GoogleDriveService(creds)
    result = drive.list_folders(page_size=20)
    folders = result.get("files", [])

    buttons = [
        [InlineKeyboardButton(text="🗂 Вся Drive (root)", callback_data=f"sync_ch_setfolder:{ch_key}:root")],
    ]
    for f in folders:
        buttons.append([InlineKeyboardButton(
            text=f"📁 {f['name'][:40]}",
            callback_data=f"sync_ch_setfolder:{ch_key}:{f['id']}",
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"sync_ch:{ch_key}")])

    await call.message.edit_text(
        f"📁 <b>Папка для синхронизации</b>\n"
        f"Сейчас: <code>{'root' if ch.folder_id == 'root' else ch.folder_id}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("sync_ch_setfolder:"))
async def ch_set_folder(call: CallbackQuery, bot: Bot):
    _, ch_key, folder_id = call.data.split(":", 2)
    sched = get_scheduler(bot)
    sched.update_channel(call.from_user.id, ch_key, folder_id=folder_id)
    label = "вся Drive" if folder_id == "root" else folder_id
    await call.answer(f"✅ Папка: {label}")
    await channel_settings(call, bot)


# ─── MIME filter ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sync_ch_mime:"))
async def ch_mime_menu(call: CallbackQuery, bot: Bot):
    ch_key = call.data.split(":", 1)[1]
    buttons = []
    for value, label in MIME_FILTER_LABELS.items():
        cb_val = value or "none"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"sync_ch_setmime:{ch_key}:{cb_val}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"sync_ch:{ch_key}")])
    await call.message.edit_text(
        "🎛 <b>Фильтр типов файлов</b>\n\nКакие файлы отправлять в этот канал?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("sync_ch_setmime:"))
async def ch_set_mime(call: CallbackQuery, bot: Bot):
    _, ch_key, raw = call.data.split(":", 2)
    mime_filter = None if raw == "none" else raw
    sched = get_scheduler(bot)
    sched.update_channel(call.from_user.id, ch_key, mime_filter=mime_filter)
    await call.answer(f"✅ {MIME_FILTER_LABELS.get(mime_filter, raw)}")
    await channel_settings(call, bot)


# ─── Send mode ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sync_ch_mode:"))
async def ch_mode_menu(call: CallbackQuery, bot: Bot):
    ch_key = call.data.split(":", 1)[1]
    buttons = [
        [InlineKeyboardButton(text="🖼 Сжатые медиа (Telegram сжимает)", callback_data=f"sync_ch_setmode:{ch_key}:{SEND_MODE_COMPRESSED}")],
        [InlineKeyboardButton(text="📎 Файлы (оригинал, без сжатия)", callback_data=f"sync_ch_setmode:{ch_key}:{SEND_MODE_FILE}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"sync_ch:{ch_key}")],
    ]
    await call.message.edit_text(
        "📤 <b>Режим отправки файлов</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("sync_ch_setmode:"))
async def ch_set_mode(call: CallbackQuery, bot: Bot):
    _, ch_key, mode = call.data.split(":", 2)
    sched = get_scheduler(bot)
    sched.update_channel(call.from_user.id, ch_key, send_mode=mode)
    await call.answer(f"✅ {SEND_MODE_LABELS.get(mode, mode)}")
    await channel_settings(call, bot)


# ─── Limit ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sync_ch_limit:"))
async def ch_limit_menu(call: CallbackQuery, bot: Bot):
    ch_key = call.data.split(":", 1)[1]
    buttons = [
        [InlineKeyboardButton(text="1 файл (тест)", callback_data=f"sync_ch_setlimit:{ch_key}:1")],
        [InlineKeyboardButton(text="5 файлов", callback_data=f"sync_ch_setlimit:{ch_key}:5")],
        [InlineKeyboardButton(text="10 файлов", callback_data=f"sync_ch_setlimit:{ch_key}:10")],
        [InlineKeyboardButton(text="50 файлов", callback_data=f"sync_ch_setlimit:{ch_key}:50")],
        [InlineKeyboardButton(text="∞ Без ограничений", callback_data=f"sync_ch_setlimit:{ch_key}:0")],
        [InlineKeyboardButton(text="✏️ Своё число", callback_data=f"sync_ch_setlimit:{ch_key}:custom")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"sync_ch:{ch_key}")],
    ]
    await call.message.edit_text(
        "🔢 <b>Лимит файлов за одну синхронизацию</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("sync_ch_setlimit:"))
async def ch_set_limit(call: CallbackQuery, state: FSMContext, bot: Bot):
    _, ch_key, value = call.data.split(":", 2)
    if value == "custom":
        await state.set_state(SyncStates.waiting_for_max_files)
        await state.update_data(ch_key=ch_key)
        await call.message.edit_text(
            "✏️ Введите число файлов:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"sync_ch:{ch_key}")
            ]]),
        )
        await call.answer()
        return
    sched = get_scheduler(bot)
    max_f = int(value) if value != "0" else None
    sched.update_channel(call.from_user.id, ch_key, max_files=max_f)
    label = str(max_f) if max_f else "без ограничений"
    await call.answer(f"✅ Лимит: {label}")
    await channel_settings(call, bot)


@router.message(SyncStates.waiting_for_max_files)
async def handle_custom_limit(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    ch_key = data.get("ch_key")
    await state.clear()
    if not message.text.strip().isdigit() or int(message.text.strip()) < 1:
        await message.answer("❌ Введите целое число больше 0.")
        return
    sched = get_scheduler(bot)
    sched.update_channel(message.from_user.id, ch_key, max_files=int(message.text.strip()))
    await message.answer(
        f"✅ Лимит: <b>{message.text.strip()} файлов</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚙️ Настройки канала", callback_data=f"sync_ch:{ch_key}")
        ]]),
    )


# ─── Delete channel ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sync_ch_del:"))
async def ch_delete(call: CallbackQuery, bot: Bot):
    ch_key = call.data.split(":", 1)[1]
    sched = get_scheduler(bot)
    ch = sched.get_channel(call.from_user.id, ch_key)
    title = ch.title if ch else "?"
    sched.remove_channel(call.from_user.id, ch_key)
    await call.answer(f"🗑 Канал «{title}» удалён")
    await show_channels(call, bot)


# ─── Sync now (per channel) ───────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sync_ch_now:"))
async def ch_sync_now(call: CallbackQuery, bot: Bot):
    ch_key = call.data.split(":", 1)[1]
    user_id = call.from_user.id
    creds = auth_service.get_credentials(user_id)
    if not creds:
        await call.answer("❌ Нет авторизации", show_alert=True)
        return
    sched = get_scheduler(bot)
    ch = sched.get_channel(user_id, ch_key)
    if not ch:
        await call.answer("Канал не найден", show_alert=True)
        return
    await call.answer("⏳ Синхронизирую...")
    # Poll only this channel
    from services.google_drive import GoogleDriveService
    drive = GoogleDriveService(creds)
    uid_str = str(user_id)
    since_token = sched._state["page_tokens"].get(uid_str)
    new_files, new_token = await sched._api_call_with_backoff(
        drive.list_new_files_since, since_token=since_token
    )
    sched._state["page_tokens"][uid_str] = new_token
    posted = 0
    limit = ch.max_files
    for file in new_files:
        if limit is not None and posted >= limit:
            break
        if not ch.matches(file):
            continue
        if ch.is_duplicate(file):
            continue
        await sched._post_file(drive, file, ch)
        ch.mark_posted(file)
        posted += 1
    sched._save_state()
    await call.message.answer(f"✅ Готово. Опубликовано файлов: <b>{posted}</b>", parse_mode="HTML")


# ─── Global sync now ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "sync_now")
async def sync_now(call: CallbackQuery, bot: Bot):
    user_id = call.from_user.id
    creds = auth_service.get_credentials(user_id)
    if not creds:
        await call.answer("❌ Нет авторизации", show_alert=True)
        return
    sched = get_scheduler(bot)
    if not sched.has_any_channel(user_id):
        await call.answer("❌ Нет настроенных каналов", show_alert=True)
        return
    await call.answer("⏳ Синхронизирую все каналы...")
    try:
        await sched._poll_user(user_id)
        await call.message.answer("✅ Синхронизация завершена.")
    except Exception as e:
        logger.error(f"Manual sync error: {e}")
        await call.message.answer(f"❌ Ошибка: {e}")


# ─── Enable / Disable ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "sync_enable")
async def enable_sync(call: CallbackQuery, bot: Bot):
    user_id = call.from_user.id
    sched = get_scheduler(bot)
    if not auth_service.get_credentials(user_id):
        await call.answer("❌ Сначала авторизуйтесь через Google", show_alert=True)
        return
    if not sched.has_any_channel(user_id):
        await call.answer("❌ Сначала добавьте канал (📡 Каналы)", show_alert=True)
        return
    sched.enable_sync(user_id)
    await call.answer("✅ Авто-синхронизация включена!")
    await show_sync_menu(call, bot)


@router.callback_query(F.data == "sync_disable")
async def disable_sync(call: CallbackQuery, bot: Bot):
    get_scheduler(bot).disable_sync(call.from_user.id)
    await call.answer("⏸ Остановлена.")
    await show_sync_menu(call, bot)


# ─── Status ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "sync_status")
async def sync_status(call: CallbackQuery, bot: Bot):
    user_id = call.from_user.id
    sched = get_scheduler(bot)
    enabled = sched.is_enabled(user_id)
    channels = sched.get_channels(user_id)
    token = sched._state.get("page_tokens", {}).get(str(user_id), "нет")

    lines = [f"📊 <b>Статус синхронизации</b>\n",
             f"Статус: {'🟢 Активна' if enabled else '🔴 Отключена'}",
             f"Drive token: <code>{'нет' if token == 'нет' else token[:16]+'…'}</code>\n"]

    for key, ch in channels.items():
        posted = len(ch._d.get("posted_hashes", []))
        mime_label = MIME_FILTER_LABELS.get(ch.mime_filter, ch.mime_filter or "все")
        mode_label = SEND_MODE_LABELS.get(ch.send_mode, ch.send_mode)
        limit_str = str(ch.max_files) if ch.max_files else "∞"
        lines.append(
            f"📡 <b>{ch.title}</b>\n"
            f"  Папка: <code>{ch.folder_id}</code>\n"
            f"  Фильтр: {mime_label}  |  Режим: {mode_label}\n"
            f"  Лимит: {limit_str}  |  Опубликовано: {posted}"
        )

    await call.answer()
    await call.message.answer("\n".join(lines), parse_mode="HTML")
