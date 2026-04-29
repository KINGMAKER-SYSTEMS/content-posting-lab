"""
Google Drive API client using a service account.

Credentials are loaded from either:
  - GOOGLE_SERVICE_ACCOUNT_JSON env var (base64-encoded JSON — for Railway)
  - GOOGLE_SERVICE_ACCOUNT_FILE env var (file path — for local dev)
"""

import base64
import json
import os
from io import BytesIO
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/drive"]

_service = None


def _get_credentials() -> Credentials | None:
    """Load service account credentials from env."""
    # Railway: base64-encoded JSON in env var
    b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if b64:
        try:
            raw = base64.b64decode(b64)
            info = json.loads(raw)
            return Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception:
            return None

    # Local: file path
    fpath = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    if fpath and os.path.exists(fpath):
        try:
            return Credentials.from_service_account_file(fpath, scopes=SCOPES)
        except Exception:
            return None

    return None


def get_service():
    """Get or create the Drive API service singleton."""
    global _service
    if _service is not None:
        return _service
    creds = _get_credentials()
    if creds is None:
        return None
    _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def is_configured() -> bool:
    """Check if Drive API credentials are available."""
    return _get_credentials() is not None


def list_files(folder_id: str, page_size: int = 100) -> list[dict]:
    """List files in a Drive folder.

    Returns list of {id, name, mimeType, size, createdTime, modifiedTime}.
    """
    svc = get_service()
    if svc is None:
        return []

    results = []
    page_token = None

    while True:
        resp = (
            svc.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, size, createdTime, modifiedTime)",
                pageSize=page_size,
                pageToken=page_token,
                orderBy="createdTime desc",
            )
            .execute()
        )
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return results


def get_folder_info(folder_id: str) -> dict | None:
    """Get folder metadata."""
    svc = get_service()
    if svc is None:
        return None

    try:
        return (
            svc.files()
            .get(fileId=folder_id, fields="id, name, mimeType")
            .execute()
        )
    except Exception:
        return None


def create_folder(name: str, parent_id: str | None = None) -> dict | None:
    """Create a new folder in Drive.

    Returns {id, name, webViewLink} or None on failure.
    If parent_id is None, creates in My Drive root.
    """
    svc = get_service()
    if svc is None:
        return None

    body: dict = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        body["parents"] = [parent_id]

    try:
        created = (
            svc.files()
            .create(body=body, fields="id, name, webViewLink")
            .execute()
        )
        return created
    except Exception:
        return None


def upload_file(folder_id: str, file_path: str, mime_type: str = "video/mp4") -> dict | None:
    """Upload a local file to a Drive folder.

    Returns the created file metadata {id, name, webViewLink}.
    """
    svc = get_service()
    if svc is None:
        return None

    name = Path(file_path).name
    file_metadata = {
        "name": name,
        "parents": [folder_id],
    }
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

    created = (
        svc.files()
        .create(
            body=file_metadata,
            media_body=media,
            fields="id, name, webViewLink",
        )
        .execute()
    )
    return created


def delete_file(file_id: str) -> bool:
    """Delete a file from Drive."""
    svc = get_service()
    if svc is None:
        return False

    try:
        svc.files().delete(fileId=file_id).execute()
        return True
    except Exception:
        return False


def download_file(file_id: str, dest_path: str) -> bool:
    """Download a file from Drive to local path."""
    svc = get_service()
    if svc is None:
        return False

    try:
        request = svc.files().get_media(fileId=file_id)
        with open(dest_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return True
    except Exception:
        return False


def count_files(folder_id: str) -> int:
    """Count files in a folder (lightweight — only fetches IDs)."""
    svc = get_service()
    if svc is None:
        return 0

    count = 0
    page_token = None

    while True:
        resp = (
            svc.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        count += len(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return count


def get_inventory(folder_ids: dict[str, str]) -> dict[str, int]:
    """Get file counts for multiple folders.

    Args:
        folder_ids: mapping of integration_id -> drive_folder_id

    Returns:
        mapping of integration_id -> file count
    """
    result = {}
    for integration_id, folder_id in folder_ids.items():
        result[integration_id] = count_files(folder_id)
    return result
