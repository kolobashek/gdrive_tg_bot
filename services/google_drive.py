"""
Google Drive API service — list, search, upload, download files.
"""

import io
import logging
import os
from typing import Optional
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

MIME_FOLDER = "application/vnd.google-apps.folder"

FILE_FIELDS = "id, name, mimeType, size, modifiedTime, webViewLink, thumbnailLink, parents"


class GoogleDriveService:
    def __init__(self, credentials: Credentials):
        self.service = build("drive", "v3", credentials=credentials)

    def list_files(
        self,
        folder_id: str = "root",
        page_size: int = 20,
        page_token: str | None = None,
        query: str | None = None,
    ) -> dict:
        """List files in a folder."""
        q_parts = [f"'{folder_id}' in parents", "trashed = false"]
        if query:
            q_parts.append(f"name contains '{query}'")
        q = " and ".join(q_parts)

        params = dict(
            q=q,
            pageSize=page_size,
            fields=f"nextPageToken, files({FILE_FIELDS})",
            orderBy="modifiedTime desc",
        )
        if page_token:
            params["pageToken"] = page_token

        result = self.service.files().list(**params).execute()
        return result

    def search_files(self, query: str, page_size: int = 15) -> list[dict]:
        """Global search across Drive."""
        q = f"name contains '{query}' and trashed = false"
        result = self.service.files().list(
            q=q,
            pageSize=page_size,
            fields=f"files({FILE_FIELDS})",
            orderBy="modifiedTime desc",
        ).execute()
        return result.get("files", [])

    def get_file_metadata(self, file_id: str) -> dict:
        return self.service.files().get(
            fileId=file_id,
            fields=FILE_FIELDS,
        ).execute()

    def upload_file(
        self,
        local_path: str,
        filename: str,
        folder_id: str = "root",
        mime_type: str | None = None,
    ) -> dict:
        """Upload a file to Drive."""
        file_metadata = {"name": filename, "parents": [folder_id]}
        media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
        uploaded = self.service.files().create(
            body=file_metadata,
            media_body=media,
            fields=FILE_FIELDS,
        ).execute()
        return uploaded

    def download_file(self, file_id: str, dest_path: str) -> str:
        """Download a file from Drive. Returns local path."""
        request = self.service.files().get_media(fileId=file_id)
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with io.FileIO(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return dest_path

    def create_folder(self, name: str, parent_id: str = "root") -> dict:
        meta = {
            "name": name,
            "mimeType": MIME_FOLDER,
            "parents": [parent_id],
        }
        return self.service.files().create(body=meta, fields=FILE_FIELDS).execute()

    def delete_file(self, file_id: str):
        self.service.files().delete(fileId=file_id).execute()

    def list_new_files_since(self, folder_id: str, since_token: str | None) -> tuple[list[dict], str]:
        """
        Returns (new_files, new_page_token).
        Uses Drive changes API with page token for efficient polling.
        """
        if not since_token:
            resp = self.service.changes().getStartPageToken().execute()
            return [], resp.get("startPageToken", "1")

        all_changes = []
        token = since_token
        while True:
            resp = self.service.changes().list(
                pageToken=token,
                fields="nextPageToken, newStartPageToken, changes(fileId, file, removed)",
                spaces="drive",
            ).execute()

            for change in resp.get("changes", []):
                if change.get("removed"):
                    continue
                f = change.get("file", {})
                if f and folder_id in f.get("parents", []):
                    all_changes.append(f)

            if "newStartPageToken" in resp:
                token = resp["newStartPageToken"]
                break
            token = resp.get("nextPageToken", token)

        return all_changes, token
