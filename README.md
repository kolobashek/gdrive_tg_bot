# 🤖 Google Drive & Photos → Telegram Bot

Бот для управления Google Drive и Google Фото прямо из Telegram, с автоматической синхронизацией новых файлов в Telegram-канал.

## ✨ Возможности

| Функция | Описание |
|---|---|
| 📁 Обзор Drive | Навигация по папкам с пагинацией |
| 🔍 Поиск | Глобальный поиск файлов по имени |
| ⬆️ Загрузка | Отправь файл боту → он попадёт на Drive |
| ⬇️ Скачивание | Скачай любой файл с Drive прямо в Telegram |
| 🖼️ Google Фото | Просмотр альбомов и медиафайлов |
| 📤 Публикация | Отправь файл или фото в Telegram-канал вручную |
| 🔄 Авто-синхронизация | Новые файлы в Drive → автоматически в канал |

---

## 🚀 Установка

### 1. Клонировать репозиторий
```bash
git clone <repo-url>
cd google_drive_bot
```

### 2. Установить зависимости
```bash
pip install -r requirements.txt
```

### 3. Настроить Google Cloud Console

1. Перейти на https://console.cloud.google.com/
2. Создать новый проект (или выбрать существующий)
3. Включить API:
   - **Google Drive API**
   - **Google Photos Library API**
4. Перейти в **APIs & Services → Credentials**
5. Нажать **Create Credentials → OAuth client ID**
6. Тип приложения: **Desktop app**
7. Скачать JSON или скопировать **Client ID** и **Client Secret**
8. В разделе **OAuth consent screen** добавить тестового пользователя (свой Google аккаунт)

### 4. Создать Telegram-бота

1. Написать @BotFather в Telegram
2. `/newbot` → задать имя и username
3. Скопировать **токен**
4. Добавить бота в свой канал как **администратора** (с правом публикации)

### 5. Настроить .env

```bash
cp .env.example .env
```

Заполнить `.env`:
```env
TELEGRAM_TOKEN=1234567890:AABBCCDDEEFFaabbccdd...
TELEGRAM_CHANNEL_ID=@your_channel
ALLOWED_USER_IDS=ваш_telegram_id

GOOGLE_CLIENT_ID=1234-abc.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
GOOGLE_REDIRECT_URI=urn:ietf:wg:oauth:2.0:oob

SYNC_INTERVAL_SECONDS=300
SYNC_WATCH_FOLDER_ID=root
```

> **Как узнать свой Telegram ID?** Написать @userinfobot.

### 6. Запустить бота

```bash
python bot.py
```

---

## 📋 Команды бота

| Команда | Описание |
|---|---|
| `/start` | Главное меню |
| `/auth` | Авторизация Google |
| `/logout` | Выйти из Google |
| `/drive` | Открыть Google Drive |
| `/search [текст]` | Поиск файлов |
| `/photos` | Google Фото |
| `/sync` | Меню синхронизации |
| `/help` | Справка |

---

## 🔄 Как работает синхронизация

```
Google Drive (папка)
       │
       │  опрос каждые N минут
       ▼
   Бот проверяет новые файлы
       │
       │  новый файл обнаружен
       ▼
   Скачать файл временно
       │
       ▼
   Отправить в Telegram-канал
   (фото → send_photo, видео → send_video, остальное → send_document)
       │
       ▼
   Сохранить ID файла (не дублировать)
```

**Два режима синхронизации:**
- **Авто** — включается командой `/sync`, проверяет Drive каждые `SYNC_INTERVAL_SECONDS` секунд
- **Ручной** — кнопка «Синхронизировать сейчас» или кнопка «📤 В канал» у конкретного файла

---

## 📁 Структура проекта

```
google_drive_bot/
├── bot.py                  # Точка входа, запуск бота
├── config.py               # Конфигурация из .env
├── requirements.txt
├── .env.example
├── data/                   # Создаётся автоматически
│   ├── tokens.json         # OAuth токены пользователей
│   ├── sync_state.json     # Состояние синхронизации
│   └── tmp/                # Временные файлы при загрузке/скачивании
├── handlers/
│   ├── common.py           # /start, /help, /auth, /logout
│   ├── drive.py            # Drive: просмотр, поиск, загрузка, скачивание
│   ├── photos.py           # Google Фото: альбомы, медиа
│   └── sync.py             # Управление синхронизацией
└── services/
    ├── google_auth.py      # OAuth2 авторизация
    ├── google_drive.py     # Google Drive API
    ├── google_photos.py    # Google Photos API
    └── scheduler.py        # Фоновый опрос и публикация
```

---

## ⚠️ Ограничения

- Файлы > 50 MB не отправляются в Telegram (ограничение API) — бот присылает ссылку
- Google Docs/Sheets/Slides (native форматы) нельзя скачать напрямую — пропускаются при авто-синхронизации
- Google Photos API не позволяет загружать фото через третьи стороны (только чтение)
- Токены хранятся в `data/tokens.json` — не публиковать в git!

---

## 🔐 Безопасность

- Добавить `data/` и `.env` в `.gitignore`
- Использовать `ALLOWED_USER_IDS` чтобы ограничить доступ к боту
- В продакшне хранить токены в зашифрованном хранилище (Redis, Vault и т.д.)
