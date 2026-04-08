"""
Google Photos handlers — albums, media items, download to bot/channel.
"""

import logging
import os
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, URLInputFile
)

from config import config
from services.google_auth import auth_service
from services.google_photos import GooglePhotosService

router = Router()
logger = logging.getLogger(__name__)


def albums_keyboard(albums: list[dict], next_token: str | None) -> InlineKeyboardMarkup:
    buttons = []
    for album in albums:
        aid = album["id"]
        title = album.get("title", "Без названия")[:40]
        count = album.get("mediaItemsCount", "?")
        buttons.append([InlineKeyboardButton(
            text=f"🗂 {title} ({count})",
            callback_data=f"photos_album:{aid}",
        )])
    nav = []
    if next_token:
        nav.append(InlineKeyboardButton(text="➡️ Ещё", callback_data=f"photos_albums_next:{next_token}"))
    nav.append(InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main"))
    buttons.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def media_keyboard(items: list[dict], album_id: str, next_token: str | None) -> InlineKeyboardMarkup:
    buttons = []
    for item in items:
        iid = item["id"]
        filename = item.get("filename", "photo")[:35]
        mtype = item.get("mediaMetadata", {})
        emoji = "🎬" if "video" in item.get("mimeType", "") else "🖼️"
        buttons.append([InlineKeyboardButton(
            text=f"{emoji} {filename}",
            callback_data=f"photos_item:{iid}",
        )])
    nav = []
    if next_token:
        nav.append(InlineKeyboardButton(
            text="➡️ Ещё",
            callback_data=f"photos_album_next:{album_id}:{next_token}",
        ))
    nav.append(InlineKeyboardButton(text="⬅️ Альбомы", callback_data="photos_albums"))
    buttons.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def item_action_keyboard(item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬇️ Скачать мне", callback_data=f"photos_download:{item_id}")],
        [InlineKeyboardButton(text="📤 В канал", callback_data=f"photos_to_channel:{item_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="photos_albums")],
    ])


@router.message(Command("photos"))
@router.callback_query(F.data == "photos_albums")
async def show_albums(event: Message | CallbackQuery):
    user_id = event.from_user.id
    creds = auth_service.get_credentials(user_id)
    if not creds:
        text = "❌ Нет авторизации. Выполните /auth"
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return

    photos = GooglePhotosService(creds)
    result = photos.list_albums(page_size=15)
    albums = result.get("albums", [])
    next_token = result.get("nextPageToken")

    if not albums:
        text = "🖼️ <b>Google Фото</b>\n\n<i>Альбомы не найдены.</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")
        ]])
    else:
        text = f"🖼️ <b>Ваши альбомы</b> ({len(albums)} из {len(albums)}):"
        kb = albums_keyboard(albums, next_token)

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("photos_albums_next:"))
async def albums_next_page(call: CallbackQuery):
    token = call.data.split(":", 1)[1]
    creds = auth_service.get_credentials(call.from_user.id)
    photos = GooglePhotosService(creds)
    result = photos.list_albums(page_size=15, page_token=token)
    albums = result.get("albums", [])
    next_token = result.get("nextPageToken")
    text = f"🖼️ <b>Альбомы (продолжение)</b>:"
    await call.message.edit_text(text, reply_markup=albums_keyboard(albums, next_token), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("photos_album:"))
async def show_album_items(call: CallbackQuery):
    album_id = call.data.split(":")[1]
    creds = auth_service.get_credentials(call.from_user.id)
    photos = GooglePhotosService(creds)

    album = photos.get_album(album_id)
    title = album.get("title", "Альбом")
    result = photos.list_media_in_album(album_id, page_size=15)
    items = result.get("mediaItems", [])
    next_token = result.get("nextPageToken")

    if not items:
        text = f"🗂 <b>{title}</b>\n\n<i>Альбом пуст.</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Альбомы", callback_data="photos_albums")
        ]])
    else:
        text = f"🗂 <b>{title}</b>\n\nФайлов: {len(items)}"
        kb = media_keyboard(items, album_id, next_token)

    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("photos_album_next:"))
async def album_next_page(call: CallbackQuery):
    _, album_id, token = call.data.split(":", 2)
    creds = auth_service.get_credentials(call.from_user.id)
    photos = GooglePhotosService(creds)
    result = photos.list_media_in_album(album_id, page_size=15, page_token=token)
    items = result.get("mediaItems", [])
    next_token = result.get("nextPageToken")
    text = "🗂 <b>Ещё файлы</b>"
    await call.message.edit_text(text, reply_markup=media_keyboard(items, album_id, next_token), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("photos_item:"))
async def show_media_item(call: CallbackQuery):
    item_id = call.data.split(":")[1]
    creds = auth_service.get_credentials(call.from_user.id)
    photos = GooglePhotosService(creds)
    item = photos.get_media_item(item_id)
    meta = item.get("mediaMetadata", {})
    filename = item.get("filename", "photo")
    created = meta.get("creationTime", "—")[:10]
    width = meta.get("width", "?")
    height = meta.get("height", "?")

    text = (
        f"🖼️ <b>{filename}</b>\n\n"
        f"Дата: {created}\n"
        f"Размер: {width}×{height}\n"
    )
    # Send thumbnail preview
    thumb_url = photos.get_download_url(item["baseUrl"], width=400, height=400)
    await call.message.answer_photo(
        URLInputFile(thumb_url, filename=filename),
        caption=text,
        reply_markup=item_action_keyboard(item_id),
        parse_mode="HTML",
    )
    await call.answer()


async def _send_photo_item(bot: Bot, chat_id: str | int, item: dict, creds):
    photos_svc = GooglePhotosService(creds)
    filename = item.get("filename", "photo.jpg")
    local_path = os.path.join(config.TEMP_DIR, filename)
    photos_svc.download_media(item["baseUrl"], local_path)

    mime = item.get("mimeType", "image/jpeg")
    caption = f"📸 {filename}"
    try:
        if "video" in mime:
            await bot.send_video(chat_id, FSInputFile(local_path, filename=filename), caption=caption)
        else:
            await bot.send_photo(chat_id, FSInputFile(local_path, filename=filename), caption=caption)
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)


@router.callback_query(F.data.startswith("photos_download:"))
async def download_photo(call: CallbackQuery, bot: Bot):
    item_id = call.data.split(":")[1]
    creds = auth_service.get_credentials(call.from_user.id)
    if not creds:
        await call.answer("Нет авторизации", show_alert=True)
        return
    await call.answer("⏳ Скачиваю...")
    photos_svc = GooglePhotosService(creds)
    item = photos_svc.get_media_item(item_id)
    await _send_photo_item(bot, call.from_user.id, item, creds)


@router.callback_query(F.data.startswith("photos_to_channel:"))
async def photo_to_channel(call: CallbackQuery, bot: Bot):
    if not config.TELEGRAM_CHANNEL_ID:
        await call.answer("❌ TELEGRAM_CHANNEL_ID не настроен", show_alert=True)
        return
    item_id = call.data.split(":")[1]
    creds = auth_service.get_credentials(call.from_user.id)
    if not creds:
        await call.answer("Нет авторизации", show_alert=True)
        return
    await call.answer("⏳ Публикую в канал...")
    photos_svc = GooglePhotosService(creds)
    item = photos_svc.get_media_item(item_id)
    await _send_photo_item(bot, config.TELEGRAM_CHANNEL_ID, item, creds)
    await call.message.answer("✅ Фото опубликовано в канале!")
