"""
Background sync scheduler — multiple channels per user.

State structure:
{
  "enabled_users": ["157168635"],
  "page_tokens":   {"157168635": "..."},   # per-user Drive changes token
  "channels": {
    "157168635": {
      "ch_abc123": {
        "id":        -1001234567890,
        "title":     "My Channel",
        "folder_id": "root",               # Drive folder to watch ("root" = all)
        "mime_filter": null,               # null | "image" | "video" | "audio" | "document"
        "send_mode": "compressed",         # "compressed" | "file"
        "max_files": null,                 # int | null = unlimited
        "posted_hashes": []               # MD5s (or file IDs) already sent to this channel
      },
      ...
    }
  }
}

Rate limits:
  - 429/503: transient  → exponential backoff
  - 403 rateLimitExceeded: daily quota  → retry at next UTC midnight
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from googleapiclient.errors import HttpError
from aiogram import Bot
from aiogram.types import FSInputFile

from config import config
from services.google_auth import auth_service
from services.google_drive import GoogleDriveService
from services.file_utils import generate_caption, split_into_zips, max_upload_bytes

logger = logging.getLogger(__name__)

SEND_MODE_COMPRESSED = "compressed"
SEND_MODE_FILE       = "file"

MIME_FILTERS = {
    "image":    ("mimeType contains 'image/'",),
    "video":    ("mimeType contains 'video/'",),
    "audio":    ("mimeType contains 'audio/'",),
    "document": (
        "mimeType contains 'application/pdf'",
        "mimeType contains 'application/vnd.ms'",
        "mimeType contains 'application/vnd.openxmlformats'",
        "mimeType contains 'application/msword'",
        "mimeType contains 'text/'",
    ),
}


def _file_matches_filter(file: dict, mime_filter: str | None) -> bool:
    """Return True if the file passes the channel's mime filter."""
    if not mime_filter:
        return True
    mime = file.get("mimeType", "")
    if mime_filter == "image":
        return mime.startswith("image/")
    if mime_filter == "video":
        return mime.startswith("video/")
    if mime_filter == "audio":
        return mime.startswith("audio/")
    if mime_filter == "document":
        return (
            "pdf" in mime or "msword" in mime or "ms-excel" in mime
            or "ms-powerpoint" in mime or "openxmlformats" in mime
            or "vnd.ms" in mime or mime.startswith("text/")
        )
    return True


def _file_matches_folder(file: dict, folder_id: str) -> bool:
    """Return True if file is in the given folder (or folder_id == 'root')."""
    if not folder_id or folder_id == "root":
        return True
    return folder_id in file.get("parents", [])


class ChannelConfig:
    """Wraps a single channel config dict with typed accessors."""

    def __init__(self, data: dict):
        self._d = data

    @property
    def channel_id(self) -> int | str:
        return self._d["id"]

    @property
    def title(self) -> str:
        return self._d.get("title", str(self._d["id"]))

    @property
    def folder_id(self) -> str:
        return self._d.get("folder_id", "root")

    @folder_id.setter
    def folder_id(self, v: str):
        self._d["folder_id"] = v

    @property
    def mime_filter(self) -> str | None:
        return self._d.get("mime_filter")

    @mime_filter.setter
    def mime_filter(self, v: str | None):
        self._d["mime_filter"] = v

    @property
    def send_mode(self) -> str:
        return self._d.get("send_mode", SEND_MODE_COMPRESSED)

    @send_mode.setter
    def send_mode(self, v: str):
        self._d["send_mode"] = v

    @property
    def max_files(self) -> int | None:
        return self._d.get("max_files")

    @max_files.setter
    def max_files(self, v: int | None):
        self._d["max_files"] = v

    def _file_hash(self, file: dict) -> str:
        return file.get("md5Checksum") or file["id"]

    def is_duplicate(self, file: dict) -> bool:
        return self._file_hash(file) in self._d.get("posted_hashes", [])

    def mark_posted(self, file: dict):
        hashes = self._d.setdefault("posted_hashes", [])
        h = self._file_hash(file)
        if h not in hashes:
            hashes.append(h)

    def matches(self, file: dict) -> bool:
        return (
            _file_matches_folder(file, self.folder_id)
            and _file_matches_filter(file, self.mime_filter)
        )

    def to_dict(self) -> dict:
        return self._d


class SyncScheduler:
    def __init__(self, bot: Bot):
        self.bot = bot
        self._state: dict = self._load_state()
        self._quota_retry_after: datetime | None = None

    # ─── Persistence ──────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if os.path.exists(config.SYNC_STATE_PATH):
            try:
                with open(config.SYNC_STATE_PATH, "r") as f:
                    state = json.load(f)
                state = self._migrate(state)
                return state
            except Exception as e:
                logger.error(f"Failed to load sync state: {e}")
        return {"enabled_users": [], "page_tokens": {}, "channels": {}}

    def _migrate(self, state: dict) -> dict:
        """Migrate legacy single-channel state to multi-channel format."""
        if "channels" not in state:
            state["channels"] = {}

        # Migrate old single channel_ids + send_modes + max_files + posted_hashes
        for uid_str, ch_data in state.pop("channel_ids", {}).items():
            if uid_str not in state["channels"]:
                state["channels"][uid_str] = {}
            if not state["channels"][uid_str]:
                key = str(uuid.uuid4())[:8]
                hashes = (
                    state.pop("posted_hashes", None)
                    or state.pop("posted_file_ids", None)
                    or []
                )
                state["channels"][uid_str][key] = {
                    "id": ch_data["id"],
                    "title": ch_data.get("title", ""),
                    "folder_id": config.SYNC_WATCH_FOLDER_ID,
                    "mime_filter": None,
                    "send_mode": state.pop("send_modes", {}).get(uid_str, SEND_MODE_COMPRESSED),
                    "max_files": state.pop("max_files", {}).get(uid_str),
                    "posted_hashes": hashes,
                }
                logger.info(f"Migrated legacy channel for user {uid_str}")

        state.pop("send_modes", None)
        state.pop("max_files", None)
        state.pop("posted_hashes", None)
        state.pop("posted_file_ids", None)
        return state

    def _save_state(self):
        with open(config.SYNC_STATE_PATH, "w") as f:
            json.dump(self._state, f, indent=2)

    # ─── User-level helpers ───────────────────────────────────────────────────

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

    # ─── Channel CRUD ─────────────────────────────────────────────────────────

    def _user_channels(self, user_id: int) -> dict:
        return self._state["channels"].setdefault(str(user_id), {})

    def get_channels(self, user_id: int) -> dict[str, ChannelConfig]:
        return {k: ChannelConfig(v) for k, v in self._user_channels(user_id).items()}

    def get_channel(self, user_id: int, ch_key: str) -> ChannelConfig | None:
        data = self._user_channels(user_id).get(ch_key)
        return ChannelConfig(data) if data else None

    def add_channel(self, user_id: int, channel_id: int, title: str = "") -> str:
        """Add a new channel config. Returns the new key."""
        key = str(uuid.uuid4())[:8]
        self._user_channels(user_id)[key] = {
            "id": channel_id,
            "title": title,
            "folder_id": "root",
            "mime_filter": None,
            "send_mode": SEND_MODE_COMPRESSED,
            "max_files": None,
            "posted_hashes": [],
        }
        self._save_state()
        return key

    def remove_channel(self, user_id: int, ch_key: str):
        self._user_channels(user_id).pop(ch_key, None)
        self._save_state()

    def update_channel(self, user_id: int, ch_key: str, **kwargs):
        ch = self._user_channels(user_id).get(ch_key)
        if ch:
            ch.update(kwargs)
            self._save_state()

    def has_any_channel(self, user_id: int) -> bool:
        return bool(self._user_channels(user_id))

    # ─── Legacy compat (used by drive/photos handlers) ────────────────────────

    def get_channel_id(self, user_id: int) -> int | str | None:
        channels = self._user_channels(user_id)
        if channels:
            return next(iter(channels.values()))["id"]
        if config.TELEGRAM_CHANNEL_ID:
            return config.TELEGRAM_CHANNEL_ID
        return None

    def get_send_mode(self, user_id: int) -> str:
        channels = self._user_channels(user_id)
        if channels:
            return next(iter(channels.values())).get("send_mode", SEND_MODE_COMPRESSED)
        return SEND_MODE_COMPRESSED

    # ─── Main loop ────────────────────────────────────────────────────────────

    async def run(self):
        logger.info("Sync scheduler started")
        while True:
            await asyncio.sleep(config.SYNC_INTERVAL_SECONDS)
            if self._quota_retry_after:
                if datetime.now(timezone.utc) < self._quota_retry_after:
                    remaining = int((self._quota_retry_after - datetime.now(timezone.utc)).total_seconds() / 60)
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

    async def _poll_user(self, user_id: int, max_files_override: int | None = None):
        creds = auth_service.get_credentials(user_id)
        if not creds:
            logger.warning(f"No credentials for user {user_id}")
            return

        channels = self.get_channels(user_id)
        if not channels:
            logger.warning(f"No channels for user {user_id}")
            return

        uid_str = str(user_id)
        since_token = self._state["page_tokens"].get(uid_str)
        drive = GoogleDriveService(creds)

        new_files, new_token = await self._api_call_with_backoff(
            drive.list_new_files_since,
            since_token=since_token,
        )
        self._state["page_tokens"][uid_str] = new_token

        if not new_files:
            self._save_state()
            return

        # Distribute each new file to matching channels
        for ch_key, ch in channels.items():
            limit = max_files_override if max_files_override is not None else ch.max_files
            posted = 0
            for file in new_files:
                if limit is not None and posted >= limit:
                    break
                if not ch.matches(file):
                    continue
                if ch.is_duplicate(file):
                    logger.info(f"[{ch.title}] Skip duplicate: {file.get('name')}")
                    continue
                await self._post_file(drive, file, ch)
                ch.mark_posted(file)
                posted += 1

        self._save_state()

    # ─── API backoff ──────────────────────────────────────────────────────────

    async def _api_call_with_backoff(self, fn, *args, **kwargs):
        delay = 1
        for attempt in range(6):
            try:
                return fn(*args, **kwargs)
            except HttpError as e:
                status = e.resp.status
                reason = e.error_details[0].get("reason", "") if e.error_details else ""
                if status in (429, 503):
                    wait = delay * (2 ** attempt)
                    logger.warning(f"Rate limit {status}, retry in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                if status == 403 and "rateLimitExceeded" in reason:
                    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
                        hour=0, minute=5, second=0, microsecond=0
                    )
                    self._quota_retry_after = tomorrow
                    logger.error(f"Daily quota exhausted. Retry at {tomorrow.isoformat()}")
                    raise
                raise
        raise RuntimeError("Max retries exceeded")

    # ─── Post file ────────────────────────────────────────────────────────────

    async def _post_file(self, drive: GoogleDriveService, file: dict, ch: ChannelConfig):
        """Post a file to the channel according to its ChannelConfig."""
        name = file.get("name", "file")
        mime = file.get("mimeType", "")
        size = int(file.get("size", 0))
        limit = max_upload_bytes()
        caption = generate_caption(file)

        if mime.startswith("application/vnd.google-apps"):
            logger.info(f"Skip Google-native: {name}")
            return

        if size > limit:
            await self.bot.send_message(
                ch.channel_id,
                f"{caption}\n\n⚠️ {size//(1024*1024)} MB > лимит {limit//(1024*1024)} MB\n"
                f"🔗 {file.get('webViewLink', '')}",
                parse_mode="HTML",
            )
            return

        local_path = os.path.join(config.TEMP_DIR, name)
        try:
            drive.download_file(file["id"], local_path)
            input_file = FSInputFile(local_path, filename=name)
            is_image = mime.startswith("image/")
            is_video = mime.startswith("video/")

            if ch.send_mode == SEND_MODE_COMPRESSED and is_image:
                await self.bot.send_photo(ch.channel_id, input_file, caption=caption, parse_mode="HTML")
            elif ch.send_mode == SEND_MODE_COMPRESSED and is_video:
                await self.bot.send_video(ch.channel_id, input_file, caption=caption, parse_mode="HTML")
            else:
                await self.bot.send_document(ch.channel_id, input_file, caption=caption, parse_mode="HTML")
            logger.info(f"[{ch.title}] Posted [{ch.send_mode}]: {name}")
        finally:
            if os.path.exists(local_path):
                os.remove(local_path)

    async def post_as_archive(self, bot: Bot, drive: GoogleDriveService, file: dict,
                               channel_id: int | str, folder_name: str = ""):
        name = file.get("name", "file")
        local_path = os.path.join(config.TEMP_DIR, name)
        limit = max_upload_bytes()
        caption_base = generate_caption(file, folder_name)
        zips: list[str] = []
        try:
            drive.download_file(file["id"], local_path)
            zips = split_into_zips(local_path, int(limit * 0.93), config.TEMP_DIR)
            for i, zip_path in enumerate(zips, 1):
                await bot.send_document(
                    channel_id,
                    FSInputFile(zip_path, filename=os.path.basename(zip_path)),
                    caption=f"{caption_base}\n🗜 Часть {i}/{len(zips)}",
                    parse_mode="HTML",
                )
        finally:
            if os.path.exists(local_path):
                os.remove(local_path)
            for z in zips:
                if os.path.exists(z):
                    os.remove(z)

    async def manual_post_file(self, bot: Bot, file_id: str, user_id: int) -> bool:
        creds = auth_service.get_credentials(user_id)
        if not creds:
            return False
        channel_id = self.get_channel_id(user_id)
        if not channel_id:
            return False
        drive = GoogleDriveService(creds)
        meta = drive.get_file_metadata(file_id)
        channels = self.get_channels(user_id)
        if channels:
            ch = next(iter(channels.values()))
            await self._post_file(drive, meta, ch)
        return True


scheduler: SyncScheduler | None = None


def get_scheduler(bot: Bot) -> SyncScheduler:
    global scheduler
    if scheduler is None:
        scheduler = SyncScheduler(bot)
    return scheduler
