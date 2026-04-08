"""
Background sync scheduler.
Polls Google Drive for new files and posts them to Telegram channel.
"""

import asyncio
import json
import logging
import os
import mimetypes
from aiogram import Bot
from aiogram.types import FSInputFile

from config import config
from services.google_auth import auth_service
from services.google_drive import GoogleDriveService

logger = logging.getLogger(__name__)


class SyncScheduler:
    def __init__(self, bot: Bot):
        self.bot = bot
        self._state: dict = self._load_state()

    def _load_state(self) -> dict:
        if os.path.exists(config.SYNC_STATE_PATH):
            try:
                with open(config.SYNC_STATE_PATH, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "enabled_users": [],       # user_ids whose Drive is being watched
            "page_tokens": {},         # user_id -> Drive changes page token
            "posted_file_ids": [],     # Drive file IDs already posted
        }

    def _save_state(self):
        with open(config.SYNC_STATE_PATH, "w") as f:
            json.dump(self._state, f, indent=2)

    def enable_sync(self, user_id: int):
        uid = str(user_id)
        if uid not in self._state["enabled_users"]:
            self._state["enabled_users"].append(uid)
            self._save_state()

    def disable_sync(self, user_id: int):
        uid = str(user_id)
        if uid in self._state["enabled_users"]:
            self._state["enabled_users"].remove(uid)
            self._save_state()

    def is_enabled(self, user_id: int) -> bool:
        return str(user_id) in self._state["enabled_users"]

    async def run(self):
        """Main polling loop."""
        logger.info("Sync scheduler started")
        while True:
            await asyncio.sleep(config.SYNC_INTERVAL_SECONDS)
            await self._poll_all()

    async def _poll_all(self):
        for uid_str in list(self._state["enabled_users"]):
            user_id = int(uid_str)
            try:
                await self._poll_user(user_id)
            except Exception as e:
                logger.error(f"Sync error for user {user_id}: {e}")

    async def _poll_user(self, user_id: int):
        creds = auth_service.get_credentials(user_id)
        if not creds:
            logger.warning(f"No credentials for user {user_id}, skipping sync")
            return

        drive = GoogleDriveService(creds)
        uid_str = str(user_id)
        since_token = self._state["page_tokens"].get(uid_str)

        new_files, new_token = drive.list_new_files_since(
            folder_id=config.SYNC_WATCH_FOLDER_ID,
            since_token=since_token,
        )
        self._state["page_tokens"][uid_str] = new_token

        for file in new_files:
            fid = file.get("id")
            if fid in self._state["posted_file_ids"]:
                continue
            await self._post_file_to_channel(drive, file)
            self._state["posted_file_ids"].append(fid)

        self._save_state()

    async def _post_file_to_channel(self, drive: GoogleDriveService, file: dict):
        """Download file and post to Telegram channel."""
        if not config.TELEGRAM_CHANNEL_ID:
            logger.warning("TELEGRAM_CHANNEL_ID not set, skipping post")
            return

        file_id = file["id"]
        name = file.get("name", "file")
        mime = file.get("mimeType", "")
        size = int(file.get("size", 0))

        # Skip Google Docs native formats (can't download directly)
        if mime.startswith("application/vnd.google-apps"):
            logger.info(f"Skipping Google native format: {name}")
            return

        # Skip files >50MB (Telegram limit)
        if size > 50 * 1024 * 1024:
            caption = (
                f"📁 <b>Новый файл на Drive</b>\n"
                f"<code>{name}</code>\n"
                f"Размер: {size // (1024*1024)} MB (слишком большой для загрузки)\n"
                f"🔗 {file.get('webViewLink', '')}"
            )
            await self.bot.send_message(
                config.TELEGRAM_CHANNEL_ID,
                caption,
                parse_mode="HTML",
            )
            return

        local_path = os.path.join(config.TEMP_DIR, name)
        try:
            drive.download_file(file_id, local_path)
            caption = f"📥 <b>{name}</b>"
            input_file = FSInputFile(local_path, filename=name)

            if mime.startswith("image/"):
                await self.bot.send_photo(
                    config.TELEGRAM_CHANNEL_ID,
                    input_file,
                    caption=caption,
                    parse_mode="HTML",
                )
            elif mime.startswith("video/"):
                await self.bot.send_video(
                    config.TELEGRAM_CHANNEL_ID,
                    input_file,
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                await self.bot.send_document(
                    config.TELEGRAM_CHANNEL_ID,
                    input_file,
                    caption=caption,
                    parse_mode="HTML",
                )
            logger.info(f"Posted file to channel: {name}")
        finally:
            if os.path.exists(local_path):
                os.remove(local_path)

    async def manual_post_file(self, bot: Bot, file_id: str, user_id: int):
        """Manually post a specific file to channel on user command."""
        creds = auth_service.get_credentials(user_id)
        if not creds:
            return False
        drive = GoogleDriveService(creds)
        meta = drive.get_file_metadata(file_id)
        await self._post_file_to_channel(drive, meta)
        return True


# Global instance — imported by handlers
scheduler: SyncScheduler | None = None


def get_scheduler(bot: Bot) -> SyncScheduler:
    global scheduler
    if scheduler is None:
        scheduler = SyncScheduler(bot)
    return scheduler
