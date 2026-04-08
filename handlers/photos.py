"""
Google Photos handlers — browse images/videos via Google Drive API.
(Google Photos Library API was shut down in 2025.)
"""

import logging
import os
from datetime import datetime, timezone, timedelta
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
)

from config import config
from services.google_auth import auth_service
from services.google_drive import GoogleDriveService
from services.file_utils import generate_caption, max_upload_bytes

router = Router()
logger = logging.getLogger(__name__)


class PhotoSearchState(StatesGroup):
    waiting_for_query = State()


# ─── Page token cache ─────────────────────────────────────────────────────────
_token_cache: dict[str, str] = {}
_token_counter = 0


def _store(token: str) -> str:
    global _token_counter
    _token_counter += 1
    key = str(_token_counter)
    _token_cache[key] = token
    return key


def _pop(key: str) -> str | None:
    return _token_cache.pop(key, None)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _emoji(mime: str) -> str:
    return "🎬" if "video" in mime else "🖼️"


def _since_iso(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _files_keyboard(
    files: list[dict],
    next_token: str | None,
    back_cb: str = "photos_menu",
) -> InlineKeyboardMarkup:
    buttons = []
    for f in files:
        name = f["name"][:38]
        buttons.append([InlineKeyboardButton(
            text=f"{_emoji(f.get('mimeType', ''))} {name}",
            callback_data=f"photo_file:{f['id']}",
        )])
    nav = []
    if next_token:
        key = _store(next_token)
        nav.append(InlineKeyboardButton(text="➡️ Ещё", callback_data=f"photos_page:{key}:{back_cb}"))
    nav.append(InlineKeyboardButton(text="🔙 Назад", callback_data=back_cb))
    buttons.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _file_action_keyboard(file_id: str, web_link: str | None) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="⬇️ Скачать мне", callback_data=f"photo_download:{file_id}")],
        [InlineKeyboardButton(text="📤 В канал", callback_data=f"photo_to_channel:{file_id}")],
    ]
    if web_link:
        buttons.append([InlineKeyboardButton(text="🌐 Открыть в браузере", url=web_link)])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="photos_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _photos_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🖼️ Фото", callback_data="photos_filter:image"),
            InlineKeyboardButton(text="🎬 Видео", callback_data="photos_filter:video"),
            InlineKeyboardButton(text="🗂 Всё", callback_data="photos_filter:all"),
        ],
        [InlineKeyboardButton(text="📁 По папкам", callback_data="photos_folders")],
        [InlineKeyboardButton(text="📅 По дате", callback_data="photos_dates")],
        [InlineKeyboardButton(text="🔍 Поиск", callback_data="photos_search")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
    ])


def _dates_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сегодня", callback_data="photos_date:1")],
        [InlineKeyboardButton(text="📅 Последние 7 дней", callback_data="photos_date:7")],
        [InlineKeyboardButton(text="📅 Последние 30 дней", callback_data="photos_date:30")],
        [InlineKeyboardButton(text="📅 Последние 365 дней", callback_data="photos_date:365")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="photos_menu")],
    ])


# ─── Main menu ────────────────────────────────────────────────────────────────

@router.message(Command("photos"))
@router.callback_query(F.data == "photos_albums")
@router.callback_query(F.data == "photos_menu")
async def show_photos_menu(event: Message | CallbackQuery):
    user_id = event.from_user.id
    creds = auth_service.get_credentials(user_id)
    if not creds:
        text = "❌ Нет авторизации. Выполните /auth"
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return

    text = "🖼️ <b>Фото и видео</b>\n\nВыберите режим просмотра:"
    kb = _photos_main_menu()
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


# ─── Filter: all / image / video ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("photos_filter:"))
async def photos_by_filter(call: CallbackQuery):
    mime_filter = call.data.split(":")[1]  # all | image | video
    creds = auth_service.get_credentials(call.from_user.id)
    drive = GoogleDriveService(creds)
    result = drive.list_images(page_size=15, mime_filter=None if mime_filter == "all" else mime_filter)
    files = result.get("files", [])
    next_token = result.get("nextPageToken")

    labels = {"image": "🖼️ Фото", "video": "🎬 Видео", "all": "🗂 Все медиафайлы"}
    title = labels.get(mime_filter, "Медиафайлы")

    if not files:
        text = f"{title}\n\n<i>Файлы не найдены.</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Назад", callback_data="photos_menu")
        ]])
    else:
        text = f"<b>{title}</b>\n\nПоследние файлы:"
        kb = _files_keyboard(files, next_token, back_cb="photos_menu")

    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ─── By date ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "photos_dates")
async def photos_dates_menu(call: CallbackQuery):
    await call.message.edit_text(
        "📅 <b>Фильтр по дате</b>\n\nВыберите период:",
        reply_markup=_dates_menu(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("photos_date:"))
async def photos_by_date(call: CallbackQuery):
    days = int(call.data.split(":")[1])
    creds = auth_service.get_credentials(call.from_user.id)
    drive = GoogleDriveService(creds)
    result = drive.list_images(page_size=15, since_date=_since_iso(days))
    files = result.get("files", [])
    next_token = result.get("nextPageToken")

    labels = {1: "сегодня", 7: "за 7 дней", 30: "за 30 дней", 365: "за год"}
    title = f"📅 Медиафайлы {labels.get(days, f'за {days} дней')}"

    if not files:
        text = f"<b>{title}</b>\n\n<i>Файлы не найдены.</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Назад", callback_data="photos_dates")
        ]])
    else:
        text = f"<b>{title}</b>:"
        kb = _files_keyboard(files, next_token, back_cb="photos_dates")

    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ─── By folder ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "photos_folders")
async def photos_folders(call: CallbackQuery):
    creds = auth_service.get_credentials(call.from_user.id)
    drive = GoogleDriveService(creds)
    result = drive.list_folders(page_size=20)
    folders = result.get("files", [])
    next_token = result.get("nextPageToken")

    if not folders:
        await call.message.edit_text(
            "📁 <b>Папки</b>\n\n<i>Папки не найдены.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 Назад", callback_data="photos_menu")
            ]]),
            parse_mode="HTML",
        )
        await call.answer()
        return

    buttons = []
    for f in folders:
        buttons.append([InlineKeyboardButton(
            text=f"📁 {f['name'][:40]}",
            callback_data=f"photos_in_folder:{f['id']}",
        )])
    nav = []
    if next_token:
        key = _store(next_token)
        nav.append(InlineKeyboardButton(text="➡️ Ещё", callback_data=f"photos_folders_page:{key}"))
    nav.append(InlineKeyboardButton(text="🔙 Назад", callback_data="photos_menu"))
    buttons.append(nav)

    await call.message.edit_text(
        "📁 <b>Папки</b>\n\nВыберите папку:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("photos_folders_page:"))
async def photos_folders_next(call: CallbackQuery):
    token_key = call.data.split(":")[1]
    token = _pop(token_key)
    if not token:
        await call.answer("Страница устарела.", show_alert=True)
        return
    creds = auth_service.get_credentials(call.from_user.id)
    drive = GoogleDriveService(creds)
    result = drive.list_folders(page_size=20, page_token=token)
    folders = result.get("files", [])
    next_token = result.get("nextPageToken")

    buttons = []
    for f in folders:
        buttons.append([InlineKeyboardButton(
            text=f"📁 {f['name'][:40]}",
            callback_data=f"photos_in_folder:{f['id']}",
        )])
    nav = []
    if next_token:
        key = _store(next_token)
        nav.append(InlineKeyboardButton(text="➡️ Ещё", callback_data=f"photos_folders_page:{key}"))
    nav.append(InlineKeyboardButton(text="🔙 Назад", callback_data="photos_menu"))
    buttons.append(nav)

    await call.message.edit_text(
        "📁 <b>Папки (продолжение)</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("photos_in_folder:"))
async def photos_in_folder(call: CallbackQuery):
    folder_id = call.data.split(":")[1]
    creds = auth_service.get_credentials(call.from_user.id)
    drive = GoogleDriveService(creds)

    folder_meta = drive.get_file_metadata(folder_id)
    folder_name = folder_meta.get("name", "Папка")

    result = drive.list_images(page_size=15, folder_id=folder_id)
    files = result.get("files", [])
    next_token = result.get("nextPageToken")

    if not files:
        text = f"📁 <b>{folder_name}</b>\n\n<i>Медиафайлы не найдены.</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 К папкам", callback_data="photos_folders")
        ]])
    else:
        text = f"📁 <b>{folder_name}</b>\n\nФайлов: {len(files)}+"
        kb = _files_keyboard(files, next_token, back_cb="photos_folders")

    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ─── Pagination (generic) ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("photos_page:"))
async def photos_next_page(call: CallbackQuery):
    parts = call.data.split(":", 2)
    token_key = parts[1]
    back_cb = parts[2] if len(parts) > 2 else "photos_menu"
    token = _pop(token_key)
    if not token:
        await call.answer("Страница устарела, обновите список.", show_alert=True)
        return
    creds = auth_service.get_credentials(call.from_user.id)
    drive = GoogleDriveService(creds)
    result = drive.list_images(page_size=15, page_token=token)
    files = result.get("files", [])
    next_token = result.get("nextPageToken")
    await call.message.edit_text(
        "🗂 <b>Медиафайлы (продолжение)</b>",
        reply_markup=_files_keyboard(files, next_token, back_cb=back_cb),
        parse_mode="HTML",
    )
    await call.answer()


# ─── Search ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "photos_search")
async def photos_search_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(PhotoSearchState.waiting_for_query)
    await call.message.edit_text(
        "🔍 <b>Поиск фото и видео</b>\n\nВведите название файла:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="photos_menu")
        ]]),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(PhotoSearchState.waiting_for_query)
async def photos_search_execute(message: Message, state: FSMContext):
    await state.clear()
    query = message.text.strip()
    creds = auth_service.get_credentials(message.from_user.id)
    drive = GoogleDriveService(creds)
    result = drive.list_images(page_size=15, name_query=query)
    files = result.get("files", [])
    next_token = result.get("nextPageToken")

    if not files:
        await message.answer(
            f"🔍 По запросу «{query}» ничего не найдено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 Назад", callback_data="photos_menu")
            ]]),
        )
    else:
        await message.answer(
            f"🔍 <b>Результаты для «{query}»</b>:",
            reply_markup=_files_keyboard(files, next_token, back_cb="photos_menu"),
            parse_mode="HTML",
        )


# ─── File info & actions ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("photo_file:"))
async def show_photo_info(call: CallbackQuery):
    file_id = call.data.split(":")[1]
    creds = auth_service.get_credentials(call.from_user.id)
    if not creds:
        await call.answer("Нет авторизации", show_alert=True)
        return
    drive = GoogleDriveService(creds)
    meta = drive.get_file_metadata(file_id)
    name = meta["name"]
    mime = meta.get("mimeType", "—")
    modified = meta.get("modifiedTime", "—")[:10]
    size_bytes = meta.get("size")
    size_str = f"{int(size_bytes) // 1024} KB" if size_bytes else "—"

    text = (
        f"{_emoji(mime)} <b>{name}</b>\n\n"
        f"Тип: <code>{mime}</code>\n"
        f"Размер: {size_str}\n"
        f"Изменён: {modified}"
    )
    await call.message.edit_text(
        text,
        reply_markup=_file_action_keyboard(file_id, meta.get("webViewLink")),
        parse_mode="HTML",
    )
    await call.answer()


async def _send_media(bot: Bot, chat_id: int | str, drive: GoogleDriveService, file_id: str):
    meta = drive.get_file_metadata(file_id)
    name = meta["name"]
    size = int(meta.get("size", 0))
    if size > 50 * 1024 * 1024:
        raise ValueError("Файл больше 50 MB — Telegram не позволяет его отправить.")
    local_path = os.path.join(config.TEMP_DIR, name)
    try:
        drive.download_file(file_id, local_path)
        mime = meta.get("mimeType", "")
        if "video" in mime:
            await bot.send_video(chat_id, FSInputFile(local_path, filename=name), caption=f"🎬 {name}")
        else:
            await bot.send_photo(chat_id, FSInputFile(local_path, filename=name), caption=f"🖼️ {name}")
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)


@router.callback_query(F.data.startswith("photo_download:"))
async def download_photo(call: CallbackQuery, bot: Bot):
    file_id = call.data.split(":")[1]
    creds = auth_service.get_credentials(call.from_user.id)
    if not creds:
        await call.answer("Нет авторизации", show_alert=True)
        return
    await call.answer("⏳ Скачиваю...")
    drive = GoogleDriveService(creds)
    try:
        await _send_media(bot, call.from_user.id, drive, file_id)
    except ValueError as e:
        await call.message.answer(f"❌ {e}")


@router.callback_query(F.data.startswith("photo_to_channel:"))
async def photo_to_channel(call: CallbackQuery, bot: Bot):
    from services.scheduler import get_scheduler
    user_id = call.from_user.id
    sched = get_scheduler(bot)
    channel_id = sched.get_channel_id(user_id)
    if not channel_id:
        await call.answer("❌ Канал не выбран. Настройте в /sync", show_alert=True)
        return
    file_id = call.data.split(":")[1]
    creds = auth_service.get_credentials(user_id)
    if not creds:
        await call.answer("Нет авторизации", show_alert=True)
        return
    drive = GoogleDriveService(creds)
    meta = drive.get_file_metadata(file_id)
    size = int(meta.get("size", 0))
    mime = meta.get("mimeType", "")
    limit = max_upload_bytes()

    if size <= limit:
        await call.answer("⏳ Публикую...")
        await sched._post_file_to_channel(drive, meta, channel_id, sched.get_send_mode(user_id))
        await call.message.answer("✅ Файл опубликован в канале!")
        return

    # Too large — offer options
    size_mb = size // (1024 * 1024)
    limit_mb = limit // (1024 * 1024)
    is_media = mime.startswith(MIME_IMAGE) or mime.startswith(MIME_VIDEO)
    buttons = []
    if is_media:
        buttons.append([InlineKeyboardButton(
            text="🖼 Сжать и отправить как медиа",
            callback_data=f"post_large_compress:{file_id}",
        )])
    buttons.append([InlineKeyboardButton(
        text="📦 Разбить на архивы",
        callback_data=f"post_large_archive:{file_id}",
    )])
    buttons.append([InlineKeyboardButton(
        text="🔗 Только ссылка на Drive",
        callback_data=f"post_large_link:{file_id}",
    )])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="photos_menu")])

    await call.message.answer(
        f"⚠️ <b>{meta['name']}</b>\n"
        f"Размер {size_mb} MB превышает лимит {limit_mb} MB.\n\nКак отправить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await call.answer()
