"""
Common handlers: /start, /help, /auth, /logout, access guard.
"""

import logging
from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import config
from services.google_auth import auth_service

router = Router()
logger = logging.getLogger(__name__)


class AuthStates(StatesGroup):
    waiting_for_code = State()


def is_allowed(user_id: int) -> bool:
    return not config.ALLOWED_USER_IDS or user_id in config.ALLOWED_USER_IDS


def main_menu_keyboard(authenticated: bool) -> InlineKeyboardMarkup:
    buttons = []
    if not authenticated:
        buttons.append([InlineKeyboardButton(text="🔑 Войти через Google", callback_data="auth_start")])
    else:
        buttons.extend([
            [
                InlineKeyboardButton(text="📁 Drive", callback_data="drive_root"),
                InlineKeyboardButton(text="🖼️ Фото", callback_data="photos_albums"),
            ],
            [
                InlineKeyboardButton(text="🔄 Синхронизация", callback_data="sync_menu"),
                InlineKeyboardButton(text="🚪 Выйти", callback_data="auth_logout"),
            ],
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(CommandStart())
async def cmd_start(message: Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    user_id = message.from_user.id
    auth = auth_service.is_authenticated(user_id)
    status = "✅ Вы авторизованы в Google." if auth else "❌ Нет авторизации Google."
    await message.answer(
        f"👋 Привет! Я бот для управления Google Drive и Фото.\n\n{status}",
        reply_markup=main_menu_keyboard(auth),
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Команды бота</b>\n\n"
        "/start — главное меню\n"
        "/auth — авторизация Google\n"
        "/logout — выйти из Google\n"
        "/drive — обзор Drive\n"
        "/search [запрос] — поиск файлов\n"
        "/photos — Google Фото\n"
        "/sync — меню синхронизации\n",
        parse_mode="HTML",
    )


# ─── Auth flow ───────────────────────────────────────────────────────────────

@router.message(Command("auth"))
@router.callback_query(F.data == "auth_start")
async def cmd_auth(event: Message | CallbackQuery, state: FSMContext):
    user_id = event.from_user.id
    if not is_allowed(user_id):
        return

    url = auth_service.get_auth_url(user_id)
    text = (
        "🔑 <b>Авторизация Google</b>\n\n"
        "1. Перейдите по ссылке:\n"
        f"<a href='{url}'>Открыть Google OAuth</a>\n\n"
        "2. Разрешите доступ — вас перенаправит на localhost (страница не откроется).\n"
        "3. Скопируйте полный URL из адресной строки браузера и отправьте его сюда."
    )
    await state.set_state(AuthStates.waiting_for_code)

    if isinstance(event, CallbackQuery):
        await event.message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
        await event.answer()
    else:
        await event.answer(text, parse_mode="HTML", disable_web_page_preview=True)


@router.message(AuthStates.waiting_for_code)
async def handle_auth_code(message: Message, state: FSMContext):
    code = message.text.strip()
    user_id = message.from_user.id
    success = auth_service.exchange_code(user_id, code)
    await state.clear()
    if success:
        await message.answer(
            "✅ Авторизация успешна! Теперь вы можете работать с Google Drive и Фото.",
            reply_markup=main_menu_keyboard(True),
        )
    else:
        await message.answer("❌ Неверный код или ошибка. Попробуйте /auth снова.")


@router.message(Command("logout"))
@router.callback_query(F.data == "auth_logout")
async def cmd_logout(event: Message | CallbackQuery):
    user_id = event.from_user.id
    auth_service.revoke(user_id)
    text = "👋 Вы вышли из Google аккаунта."
    if isinstance(event, CallbackQuery):
        await event.message.answer(text, reply_markup=main_menu_keyboard(False))
        await event.answer()
    else:
        await event.answer(text, reply_markup=main_menu_keyboard(False))
