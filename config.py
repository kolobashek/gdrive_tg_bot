"""
Configuration — loads from environment variables / .env file
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Telegram
    TELEGRAM_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))
    TELEGRAM_CHANNEL_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHANNEL_ID", ""))
    ALLOWED_USER_IDS: list[int] = field(default_factory=lambda: [
        int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
    ])

    # Google OAuth
    GOOGLE_CLIENT_ID: str = field(default_factory=lambda: os.getenv("GOOGLE_CLIENT_ID", ""))
    GOOGLE_CLIENT_SECRET: str = field(default_factory=lambda: os.getenv("GOOGLE_CLIENT_SECRET", ""))
    GOOGLE_REDIRECT_URI: str = field(default_factory=lambda: os.getenv("GOOGLE_REDIRECT_URI", "urn:ietf:wg:oauth:2.0:oob"))

    # Sync settings
    SYNC_INTERVAL_SECONDS: int = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))   # 5 min default
    SYNC_WATCH_FOLDER_ID: str = field(default_factory=lambda: os.getenv("SYNC_WATCH_FOLDER_ID", "root"))

    # Paths
    TOKEN_STORAGE_PATH: str = "data/tokens.json"
    SYNC_STATE_PATH: str = "data/sync_state.json"
    TEMP_DIR: str = "data/tmp"


config = Config()

# Create directories
for directory in ["data", "data/tmp"]:
    os.makedirs(directory, exist_ok=True)
