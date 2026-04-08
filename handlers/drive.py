"""
Google Drive handlers — browse, search, upload, download.
"""

import logging
import os
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import config
from services.google_auth import auth_service
from services.google_drive import GoogleDriveService, MIME_FOLDER

router = Router()
logger = logging.getLogger(__name__)

# Short-lived cache for Drive page tokens (key → token string)
_page_token_cache: dict[str, str] = {}
_page_token_counter = 0


def _store_page_token(token: str) -> str:
    global _page_token_counter
    _page_token_counter += 1
    key = str(_page_token_counter)
    _page_token_cache[key] = token
    return key


def _pop_page_token(key: str) -> str | None:
    return _page_token_cache.pop(key, None)

EMOJI = {
    "folder": "📁",
    "image": "🖼️",
    "video": "🎬",
    "audio": "🎵",
    "pdf": "📄",
    "spreadsheet": "📊",
    "document": "📝",
    "default": "📎",
}


def file_emoji(mime: str) -> str:
    if mime == MIME_FOLDER:
        return EMOJI["folder"]
    if mime.startswith("image/"):
        return EMOJI["image"]
    if mime.startswith("video/"):
        return EMOJI["video"]
    if mime.startswith("audio/"):
        return EMOJI["audio"]
    if "pdf" in mime:
        return EMOJI["pdf"]
    if "spreadsheet" in mime or "excel" in mime:
        return EMOJI["spreadsheet"]
    if "document" in mime or "word" in mime:
        return EMOJI["document"]
    return EMOJI["default"]


def format_size(size_str: str | None) -> str:
    if not size_str:
        return ""
    size = int(size_str)
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size // 1024} KB"
    if size < 1024 ** 3:
        return f"{size // 1024 ** 2} MB"
    return f"{size // 1024 ** 3} GB"


def build_file_list_keyboard(files: list[dict], folder_id: str, next_token: str | None) -> InlineKeyboardMarkup:
    buttons = []
    for f in files:
        fid = f["id"]
        name = f["name"][:35]
        mime = f.get("mimeType", "")
        emoji = file_emoji(mime)
        if mime == MIME_FOLDER:
            buttons.append([InlineKeyboardButton(text=f"{emoji} {name}/", callback_data=f"drive_folder:{fid}")])
        else:
            buttons.append([InlineKeyboardButton(text=f"{emoji} {name}", callback_data=f"drive_file:{fid}")])

    nav = []
    if folder_id != "root":
        nav.append(InlineKeyboardButton(text="⬆️ Назад", callback_data="drive_root"))
    if next_token:
        token_key = _store_page_token(next_token)
        nav.append(InlineKeyboardButton(text="➡️ Ещё", callback_data=f"drive_next:{folder_id}:{token_key}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_file_action_keyboard(file_id: str, mime: str, web_link: str | None) -> InlineKeyboardMarkup:
    buttons = []
    if not mime.startswith("application/vnd.google-apps"):
        buttons.append([InlineKeyboardButton(text="⬇️ Скачать", callback_data=f"drive_download:{file_id}")])
    buttons.append([InlineKeyboardButton(text="📤 В канал", callback_data=f"drive_to_channel:{file_id}")])
    if web_link:
        buttons.append([InlineKeyboardButton(text="🌐 Открыть в браузере", url=web_link)])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="drive_root")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_folder(target: Message | CallbackQuery, user_id: int, folder_id: str = "root"):
    creds = auth_service.get_credentials(user_id)
    if not creds:
        text = "❌ Нет авторизации. Выполните /auth"
        if isinstance(target, CallbackQuery):
            await target.message.answer(text)
        else:
            await target.answer(text)
        return

    drive = GoogleDriveService(creds)
    result = drive.list_files(folder_id=folder_id, page_size=15)
    files = result.get("files", [])
    next_token = result.get("nextPageToken")

    header = "📁 <b>Корневая папка</b>" if folder_id == "root" else "📁 <b>Содержимое папки</b>"
    if not files:
        text = f"{header}\n\n<i>Папка пуста</i>"
    else:
        lines = [header, ""]
        for f in files:
            size = format_size(f.get("size"))
            size_str = f" ({size})" if size else ""
            lines.append(f"{file_emoji(f.get('mimeType',''))} {f['name']}{size_str}")
        text = "\n".join(lines)

    kb = build_file_list_keyboard(files, folder_id, next_token)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


# ─── Commands ─────────────────────────────────────────────────────────────────

@router.message(Command("drive"))
@router.callback_query(F.data == "drive_root")
async def cmd_drive(event: Message | CallbackQuery):
    user_id = event.from_user.id
    await show_folder(event, user_id, "root")


@router.callback_query(F.data.startswith("drive_folder:"))
async def open_folder(call: CallbackQuery):
    folder_id = call.data.split(":")[1]
    await show_folder(call, call.from_user.id, folder_id)


@router.callback_query(F.data.startswith("drive_next:"))
async def next_page(call: CallbackQuery):
    _, folder_id, token_key = call.data.split(":", 2)
    token = _pop_page_token(token_key)
    if not token:
        await call.answer("Страница устарела, обновите список.", show_alert=True)
        return
    creds = auth_service.get_credentials(call.from_user.id)
    drive = GoogleDriveService(creds)
    result = drive.list_files(folder_id=folder_id, page_size=15, page_token=token)
    files = result.get("files", [])
    next_token = result.get("nextPageToken")
    kb = build_file_list_keyboard(files, folder_id, next_token)
    lines = ["📁 <b>Файлы (следующая страница)</b>", ""]
    for f in files:
        size = format_size(f.get("size"))
        lines.append(f"{file_emoji(f.get('mimeType',''))} {f['name']}" + (f" ({size})" if size else ""))
    await call.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("drive_file:"))
async def show_file(call: CallbackQuery):
    file_id = call.data.split(":")[1]
    creds = auth_service.get_credentials(call.from_user.id)
    if not creds:
        await call.answer("Нет авторизации", show_alert=True)
        return
    drive = GoogleDriveService(creds)
    meta = drive.get_file_metadata(file_id)
    size = format_size(meta.get("size"))
    text = (
        f"{file_emoji(meta.get('mimeType',''))} <b>{meta['name']}</b>\n\n"
        f"Тип: <code>{meta.get('mimeType','—')}</code>\n"
        f"Размер: {size or '—'}\n"
        f"Изменён: {meta.get('modifiedTime','—')[:10]}"
    )
    kb = build_file_action_keyboard(file_id, meta.get("mimeType", ""), meta.get("webViewLink"))
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("drive_download:"))
async def download_file(call: CallbackQuery, bot: Bot):
    file_id = call.data.split(":")[1]
    user_id = call.from_user.id
    creds = auth_service.get_credentials(user_id)
    if not creds:
        await call.answer("Нет авторизации", show_alert=True)
        return

    await call.answer("⏳ Скачиваю...")
    drive = GoogleDriveService(creds)
    meta = drive.get_file_metadata(file_id)
    name = meta["name"]
    size = int(meta.get("size", 0))

    if size > 50 * 1024 * 1024:
        await call.message.answer("❌ Файл больше 50 MB — Telegram не позволяет его отправить.")
        return

    local_path = os.path.join(config.TEMP_DIR, name)
    try:
        drive.download_file(file_id, local_path)
        await bot.send_document(
            call.from_user.id,
            FSInputFile(local_path, filename=name),
            caption=f"📥 {name}",
        )
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)


@router.callback_query(F.data.startswith("drive_to_channel:"))
async def post_to_channel(call: CallbackQuery, bot: Bot):
    if not config.TELEGRAM_CHANNEL_ID:
        await call.answer("❌ TELEGRAM_CHANNEL_ID не настроен", show_alert=True)
        return
    file_id = call.data.split(":")[1]
    user_id = call.from_user.id
    creds = auth_service.get_credentials(user_id)
    if not creds:
        await call.answer("Нет авторизации", show_alert=True)
        return

    await call.answer("⏳ Публикую в канал...")
    from services.scheduler import get_scheduler
    sched = get_scheduler(bot)
    ok = await sched.manual_post_file(bot, file_id, user_id)
    if ok:
        await call.message.answer("✅ Файл опубликован в канале!")
    else:
        await call.message.answer("❌ Не удалось опубликовать файл.")


# ─── Search ────────────────────────────────────────────────────────────────────

@router.message(Command("search"))
async def cmd_search(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("🔍 Использование: /search [запрос]")
        return

    query = parts[1]
    creds = auth_service.get_credentials(message.from_user.id)
    if not creds:
        await message.answer("❌ Нет авторизации. Выполните /auth")
        return

    drive = GoogleDriveService(creds)
    files = drive.search_files(query)

    if not files:
        await message.answer(f"🔍 По запросу «{query}» ничего не найдено.")
        return

    lines = [f"🔍 <b>Результаты для «{query}»</b>", ""]
    buttons = []
    for f in files[:10]:
        size = format_size(f.get("size"))
        lines.append(f"{file_emoji(f.get('mimeType',''))} {f['name']}" + (f" ({size})" if size else ""))
        buttons.append([InlineKeyboardButton(
            text=f"{file_emoji(f.get('mimeType',''))} {f['name'][:40]}",
            callback_data=f"drive_file:{f['id']}",
        )])

    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")])
    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


# ─── Upload ────────────────────────────────────────────────────────────────────

@router.message(F.document | F.photo | F.video)
async def handle_upload(message: Message, bot: Bot):
    user_id = message.from_user.id
    creds = auth_service.get_credentials(user_id)
    if not creds:
        return  # silently ignore if not authenticated

    if message.document:
        tg_file = message.document
        name = tg_file.file_name or "file"
        mime = tg_file.mime_type or "application/octet-stream"
    elif message.photo:
        tg_file = message.photo[-1]
        name = f"photo_{tg_file.file_id[:8]}.jpg"
        mime = "image/jpeg"
    elif message.video:
        tg_file = message.video
        name = tg_file.file_name or f"video_{tg_file.file_id[:8]}.mp4"
        mime = tg_file.mime_type or "video/mp4"
    else:
        return

    local_path = os.path.join(config.TEMP_DIR, name)
    await message.answer(f"⏳ Загружаю <b>{name}</b> на Google Drive...", parse_mode="HTML")
    try:
        tg_file_obj = await bot.get_file(tg_file.file_id)
        await bot.download_file(tg_file_obj.file_path, local_path)
        drive = GoogleDriveService(creds)
        uploaded = drive.upload_file(local_path, name, mime_type=mime)
        await message.answer(
            f"✅ <b>{name}</b> загружен на Drive!\n🔗 {uploaded.get('webViewLink','')}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        await message.answer("❌ Ошибка при загрузке.")
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)


@router.callback_query(F.data == "back_main")
async def back_main(call: CallbackQuery):
    from handlers.common import main_menu_keyboard
    auth = auth_service.is_authenticated(call.from_user.id)
    await call.message.edit_text(
        "🏠 Главное меню",
        reply_markup=main_menu_keyboard(auth),
    )
    await call.answer()
