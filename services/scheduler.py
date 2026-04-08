"""
Background sync scheduler.
Polls Google Drive for new files and posts them to Telegram channel.

Rate limits (Drive API v3):
  - 1 000 req / 100s per user  →  polling every 5 min = 288 req/day, well within limits
  - Daily project quota: ~1B req/day, irrelevant for personal use
  - 429 / 503: transient rate-limit  →  exponential backoff, up to 5 retries
  - 403 rateLimitExceeded: daily quota exhausted  →  schedule retry next UTC midnight
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from googleapiclient.errors import HttpError
from aiogram import Bot
from aiogram.types import FSInputFile

from config import config
from services.google_auth import auth_service
from services.google_drive import GoogleDriveService
from services.file_utils import generate_caption, split_into_zips, max_upload_bytes

logger = logging.getLogger(__name__)

SEND_MODE_COMPRESSED = "compressed"   # send_photo / send_video (Telegram re-encodes)
SEND_MODE_FILE       = "file"         # send_document (original quality)


class SyncScheduler:
    def __init__(self, bot: Bot):
        self.bot = bot
        self._state: dict = self._load_state()
        # If daily quota was exhausted, sleep until this timestamp
        self._quota_retry_after: datetime | None = None

    # ─── Persistence ──────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if os.path.exists(config.SYNC_STATE_PATH):
            try:
                with open(config.SYNC_STATE_PATH, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "enabled_users": [],    # user_ids whose Drive is being watched
            "page_tokens": {},      # user_id -> Drive changes page token
            "posted_file_ids": [],  # Drive file IDs already posted
            "channel_ids": {},      # user_id -> {id, title}
            "send_modes": {},       # user_id -> "compressed" | "file"
            "max_files": {},        # user_id -> int | null (None = unlimited)
        }

    def _save_state(self):
        with open(config.SYNC_STATE_PATH, "w") as f:
            json.dump(self._state, f, indent=2)

    # ─── User settings ────────────────────────────────────────────────────────

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

    def set_channel(self, user_id: int, channel_id: int, title: str = ""):
        self._state.setdefault("channel_ids", {})[str(user_id)] = {
            "id": channel_id,
            "title": title,
        }
        self._save_state()

    def get_channel(self, user_id: int) -> dict | None:
        entry = self._state.get("channel_ids", {}).get(str(user_id))
        if entry:
            return entry
        if config.TELEGRAM_CHANNEL_ID:
            return {"id": config.TELEGRAM_CHANNEL_ID, "title": ""}
        return None

    def get_channel_id(self, user_id: int) -> int | str | None:
        ch = self.get_channel(user_id)
        return ch["id"] if ch else None

    def set_send_mode(self, user_id: int, mode: str):
        """mode: 'compressed' | 'file'"""
        self._state.setdefault("send_modes", {})[str(user_id)] = mode
        self._save_state()

    def get_send_mode(self, user_id: int) -> str:
        return self._state.get("send_modes", {}).get(str(user_id), SEND_MODE_COMPRESSED)

    def set_max_files(self, user_id: int, max_files: int | None):
        """None = unlimited. Set a small number for testing."""
        self._state.setdefault("max_files", {})[str(user_id)] = max_files
        self._save_state()

    def get_max_files(self, user_id: int) -> int | None:
        return self._state.get("max_files", {}).get(str(user_id))

    # ─── Main loop ────────────────────────────────────────────────────────────

    async def run(self):
        """Main polling loop."""
        logger.info("Sync scheduler started")
        while True:
            await asyncio.sleep(config.SYNC_INTERVAL_SECONDS)

            # If daily quota was exhausted, wait until reset
            if self._quota_retry_after:
                now = datetime.now(timezone.utc)
                if now < self._quota_retry_after:
                    remaining = int((self._quota_retry_after - now).total_seconds() / 60)
                    logger.info(f"Daily quota exhausted, retrying in {remaining} min")
                    continue
                self._quota_retry_after = None

            await self._poll_all()

    async def _poll_all(self):
        for uid_str in list(self._state["enabled_users"]):
            try:
                await self._poll_user(int(uid_str))
            except Exception as e:
                logger.error(f"Sync error for user {uid_str}: {e}")

    # ─── Per-user poll ────────────────────────────────────────────────────────

    async def _poll_user(self, user_id: int, max_files: int | None = None):
        creds = auth_service.get_credentials(user_id)
        if not creds:
            logger.warning(f"No credentials for user {user_id}, skipping sync")
            return

        channel_id = self.get_channel_id(user_id)
        if not channel_id:
            logger.warning(f"No channel set for user {user_id}, skipping sync")
            return

        if max_files is None:
            max_files = self.get_max_files(user_id)  # None = unlimited

        drive = GoogleDriveService(creds)
        uid_str = str(user_id)
        since_token = self._state["page_tokens"].get(uid_str)

        new_files, new_token = await self._api_call_with_backoff(
            drive.list_new_files_since,
            folder_id=config.SYNC_WATCH_FOLDER_ID,
            since_token=since_token,
        )
        self._state["page_tokens"][uid_str] = new_token

        send_mode = self.get_send_mode(user_id)
        posted = 0
        for file in new_files:
            if max_files is not None and posted >= max_files:
                logger.info(f"Reached max_files={max_files} for user {user_id}, stopping")
                break
            fid = file.get("id")
            if fid in self._state["posted_file_ids"]:
                continue
            await self._post_file_to_channel(drive, file, channel_id, send_mode)
            self._state["posted_file_ids"].append(fid)
            posted += 1

        self._save_state()
        return posted

    # ─── API with backoff ─────────────────────────────────────────────────────

    async def _api_call_with_backoff(self, fn, *args, **kwargs):
        """Call a sync Drive API function with exponential backoff on 429/503."""
        delay = 1
        for attempt in range(6):
            try:
                return fn(*args, **kwargs)
            except HttpError as e:
                status = e.resp.status
                reason = e.error_details[0].get("reason", "") if e.error_details else ""

                if status in (429, 503):
                    # Transient rate limit — backoff and retry
                    wait = delay * (2 ** attempt)
                    logger.warning(f"Rate limit ({status}), retrying in {wait}s (attempt {attempt+1})")
                    await asyncio.sleep(wait)
                    continue

                if status == 403 and "rateLimitExceeded" in reason:
                    # Daily quota exhausted — schedule retry at next UTC midnight
                    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
                        hour=0, minute=5, second=0, microsecond=0
                    )
                    self._quota_retry_after = tomorrow
                    logger.error(
                        f"Daily quota exhausted. Next retry scheduled at {tomorrow.isoformat()}"
                    )
                    raise

                raise
        raise RuntimeError("Max retries exceeded for Drive API call")

    # ─── Post to channel ──────────────────────────────────────────────────────

    async def _post_file_to_channel(
        self,
        drive: GoogleDriveService,
        file: dict,
        channel_id: int | str,
        send_mode: str = SEND_MODE_COMPRESSED,
        folder_name: str = "",
    ):
        file_id = file["id"]
        name = file.get("name", "file")
        mime = file.get("mimeType", "")
        size = int(file.get("size", 0))
        limit = max_upload_bytes()

        if mime.startswith("application/vnd.google-apps"):
            logger.info(f"Skipping Google native format: {name}")
            return

        caption = generate_caption(file, folder_name)

        # File exceeds upload limit → post link + split notice
        if size > limit:
            size_mb = size // (1024 * 1024)
            limit_mb = limit // (1024 * 1024)
            await self.bot.send_message(
                channel_id,
                f"{caption}\n\n"
                f"⚠️ Файл {size_mb} MB превышает лимит {limit_mb} MB.\n"
                f"🔗 {file.get('webViewLink', '')}",
                parse_mode="HTML",
            )
            return

        local_path = os.path.join(config.TEMP_DIR, name)
        try:
            drive.download_file(file_id, local_path)
            input_file = FSInputFile(local_path, filename=name)
            is_image = mime.startswith("image/")
            is_video = mime.startswith("video/")

            if send_mode == SEND_MODE_COMPRESSED and is_image:
                await self.bot.send_photo(channel_id, input_file, caption=caption, parse_mode="HTML")
            elif send_mode == SEND_MODE_COMPRESSED and is_video:
                await self.bot.send_video(channel_id, input_file, caption=caption, parse_mode="HTML")
            else:
                await self.bot.send_document(channel_id, input_file, caption=caption, parse_mode="HTML")

            logger.info(f"Posted to channel [{send_mode}]: {name}")
        finally:
            if os.path.exists(local_path):
                os.remove(local_path)

    async def post_as_archive(
        self,
        bot: Bot,
        drive: GoogleDriveService,
        file: dict,
        channel_id: int | str,
        folder_name: str = "",
    ):
        """Download, split into zips, post each part."""
        file_id = file["id"]
        name = file.get("name", "file")
        local_path = os.path.join(config.TEMP_DIR, name)
        limit = max_upload_bytes()
        caption_base = generate_caption(file, folder_name)

        zips: list[str] = []
        try:
            drive.download_file(file_id, local_path)
            # Leave ~5% headroom for zip overhead
            part_size = int(limit * 0.93)
            zips = split_into_zips(local_path, part_size, config.TEMP_DIR)
            total = len(zips)
            for i, zip_path in enumerate(zips, 1):
                zip_name = os.path.basename(zip_path)
                part_caption = f"{caption_base}\n🗜 Часть {i}/{total}"
                await bot.send_document(
                    channel_id,
                    FSInputFile(zip_path, filename=zip_name),
                    caption=part_caption,
                    parse_mode="HTML",
                )
            logger.info(f"Posted archive ({total} parts): {name}")
        finally:
            if os.path.exists(local_path):
                os.remove(local_path)
            for z in zips:
                if os.path.exists(z):
                    os.remove(z)

    async def manual_post_file(self, bot: Bot, file_id: str, user_id: int):
        creds = auth_service.get_credentials(user_id)
        if not creds:
            return False
        channel_id = self.get_channel_id(user_id)
        if not channel_id:
            return False
        drive = GoogleDriveService(creds)
        meta = drive.get_file_metadata(file_id)
        send_mode = self.get_send_mode(user_id)
        await self._post_file_to_channel(drive, meta, channel_id, send_mode)
        return True


# Global instance
scheduler: SyncScheduler | None = None


def get_scheduler(bot: Bot) -> SyncScheduler:
    global scheduler
    if scheduler is None:
        scheduler = SyncScheduler(bot)
    return scheduler
