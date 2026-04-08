"""
Google OAuth2 service — handles authentication and token storage.
"""

import json
import os
import logging
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request

from config import config

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/photoslibrary.readonly",
]

CLIENT_CONFIG = {
    "installed": {
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uris": [config.GOOGLE_REDIRECT_URI],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


class GoogleAuthService:
    def __init__(self):
        self._tokens: dict[int, dict] = {}
        self._flows: dict[int, Flow] = {}
        self._load_tokens()

    def _load_tokens(self):
        if os.path.exists(config.TOKEN_STORAGE_PATH):
            try:
                with open(config.TOKEN_STORAGE_PATH, "r") as f:
                    raw = json.load(f)
                    self._tokens = {int(k): v for k, v in raw.items()}
            except Exception as e:
                logger.error(f"Failed to load tokens: {e}")

    def _save_tokens(self):
        with open(config.TOKEN_STORAGE_PATH, "w") as f:
            json.dump(self._tokens, f, indent=2)

    def get_auth_url(self, user_id: int) -> str:
        """Generate OAuth URL for a user."""
        flow = Flow.from_client_config(
            CLIENT_CONFIG,
            scopes=SCOPES,
            redirect_uri=config.GOOGLE_REDIRECT_URI,
        )
        flow.redirect_uri = config.GOOGLE_REDIRECT_URI
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        logger.info(f"Auth URL scopes requested: {SCOPES}")
        self._flows[user_id] = flow
        return auth_url

    def exchange_code(self, user_id: int, code: str) -> bool:
        """Exchange auth code for tokens. Accepts either a bare code or a full redirect URL."""
        flow = self._flows.get(user_id)
        if not flow:
            logger.error(f"No flow found for user {user_id}")
            return False
        # Support pasting the full redirect URL (http://localhost?code=...&...)
        if code.startswith("http"):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(code)
            params = parse_qs(parsed.query)
            granted_scope = params.get("scope", ["(not returned)"])[0]
            logger.info(f"Redirect URL granted scope: {granted_scope}")
            codes = params.get("code")
            if not codes:
                logger.error(f"No 'code' param found in URL. Full URL: {code[:200]}")
                return False
            code = codes[0]
        try:
            flow.fetch_token(code=code)
            creds = flow.credentials
            logger.info(f"Token scopes after exchange: {creds.scopes}")
            self._tokens[user_id] = {
                "token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": list(creds.scopes) if creds.scopes else SCOPES,
            }
            self._save_tokens()
            del self._flows[user_id]
            return True
        except Exception as e:
            logger.error(f"Token exchange failed for user {user_id}: {e}")
            return False

    def get_credentials(self, user_id: int) -> Credentials | None:
        """Get valid credentials for a user, refreshing if needed."""
        token_data = self._tokens.get(user_id)
        if not token_data:
            return None
        creds = Credentials(
            token=token_data["token"],
            refresh_token=token_data["refresh_token"],
            token_uri=token_data["token_uri"],
            client_id=token_data["client_id"],
            client_secret=token_data["client_secret"],
            scopes=token_data["scopes"],
        )
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                token_data["token"] = creds.token
                self._tokens[user_id] = token_data
                self._save_tokens()
            except Exception as e:
                logger.error(f"Token refresh failed: {e}")
                return None
        return creds

    def is_authenticated(self, user_id: int) -> bool:
        return user_id in self._tokens

    def revoke(self, user_id: int):
        self._tokens.pop(user_id, None)
        self._save_tokens()


auth_service = GoogleAuthService()
