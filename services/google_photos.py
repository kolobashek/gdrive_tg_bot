"""
Google Photos API service — albums and media items.
"""

import logging
import requests
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

PHOTOS_BASE = "https://photoslibrary.googleapis.com/v1"


class GooglePhotosService:
    def __init__(self, credentials: Credentials):
        self.credentials = credentials
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {credentials.token}"})

    def _refresh_token(self):
        from google.auth.transport.requests import Request
        self.credentials.refresh(Request())
        self._session.headers.update({"Authorization": f"Bearer {self.credentials.token}"})

    def _get(self, url: str, **params) -> dict:
        resp = self._session.get(url, params=params)
        if resp.status_code == 401:
            self._refresh_token()
            resp = self._session.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, url: str, json_data: dict) -> dict:
        resp = self._session.post(url, json=json_data)
        if resp.status_code == 401:
            self._refresh_token()
            resp = self._session.post(url, json=json_data)
        resp.raise_for_status()
        return resp.json()

    def list_albums(self, page_size: int = 20, page_token: str | None = None) -> dict:
        """List user's albums."""
        params: dict = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        return self._get(f"{PHOTOS_BASE}/albums", **params)

    def get_album(self, album_id: str) -> dict:
        return self._get(f"{PHOTOS_BASE}/albums/{album_id}")

    def list_media_in_album(
        self,
        album_id: str,
        page_size: int = 20,
        page_token: str | None = None,
    ) -> dict:
        """List media items in a specific album."""
        body: dict = {"albumId": album_id, "pageSize": page_size}
        if page_token:
            body["pageToken"] = page_token
        return self._post(f"{PHOTOS_BASE}/mediaItems:search", json_data=body)

    def list_media(self, page_size: int = 20, page_token: str | None = None) -> dict:
        """List all media items."""
        params: dict = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        return self._get(f"{PHOTOS_BASE}/mediaItems", **params)

    def get_media_item(self, media_item_id: str) -> dict:
        return self._get(f"{PHOTOS_BASE}/mediaItems/{media_item_id}")

    def get_download_url(self, base_url: str, width: int = 1920, height: int = 1080) -> str:
        """Construct a download URL for a media item."""
        return f"{base_url}=w{width}-h{height}"

    def download_media(self, base_url: str, dest_path: str) -> str:
        """Download a photo/video to disk."""
        url = self.get_download_url(base_url)
        resp = self._session.get(url, stream=True)
        resp.raise_for_status()
        import os
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return dest_path
