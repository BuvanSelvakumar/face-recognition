"""Where wedding photos come from: a Google Drive folder, or a folder on local disk."""
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import config

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"


@dataclass
class SourceFile:
    source_id: str
    name: str
    mime: str | None


class SourceError(Exception):
    pass


# ---------------------------------------------------------------- Google Drive

def parse_drive_folder_id(link: str) -> str:
    link = link.strip()
    for pattern in (r"/folders/([\w-]+)", r"[?&]id=([\w-]+)"):
        m = re.search(pattern, link)
        if m:
            return m.group(1)
    if re.fullmatch(r"[\w-]{10,}", link):
        return link
    raise SourceError("That doesn't look like a Google Drive folder link.")


def service_account_email() -> str | None:
    try:
        return json.loads(Path(config.GOOGLE_SERVICE_ACCOUNT_FILE).read_text())["client_email"]
    except Exception:
        return None


def _drive():
    # A fresh client per call: the underlying HTTP library is not thread-safe.
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    key_file = Path(config.GOOGLE_SERVICE_ACCOUNT_FILE)
    if not key_file.exists():
        raise SourceError(
            f"Google service-account key not found at {key_file}. See README → Google Drive setup."
        )
    creds = service_account.Credentials.from_service_account_file(
        str(key_file), scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _not_shared_error() -> SourceError:
    email = service_account_email() or "the service account"
    return SourceError(f"Can't open that folder. Share it (Viewer) with {email} and try again.")


def drive_check_folder(folder_id: str) -> str:
    """Returns the folder name, or raises if the service account can't see it."""
    from googleapiclient.errors import HttpError

    try:
        meta = _drive().files().get(
            fileId=folder_id, fields="id,name,mimeType", supportsAllDrives=True
        ).execute()
    except HttpError as e:
        if e.resp.status in (403, 404):
            raise _not_shared_error() from e
        raise SourceError(f"Google Drive error: {e}") from e
    if meta.get("mimeType") != DRIVE_FOLDER_MIME:
        raise SourceError("That link is a file, not a folder.")
    return meta["name"]


def drive_list_images(folder_id: str) -> list[SourceFile]:
    """All images in the folder, including sub-folders."""
    svc = _drive()
    out: list[SourceFile] = []
    queue: list[tuple[str, str]] = [(folder_id, "")]
    while queue:
        fid, prefix = queue.pop(0)
        page_token = None
        while True:
            resp = svc.files().list(
                q=f"'{fid}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for f in resp.get("files", []):
                if f["mimeType"] == DRIVE_FOLDER_MIME:
                    queue.append((f["id"], f"{prefix}{f['name']}/"))
                elif f["mimeType"].startswith("image/") or Path(f["name"]).suffix.lower() in IMAGE_EXTS:
                    out.append(SourceFile(f["id"], prefix + f["name"], f["mimeType"]))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    out.sort(key=lambda f: f.name)
    return out


def drive_download(file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    request = _drive().files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=16 * 1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk(num_retries=3)
    return buf.getvalue()


# ---------------------------------------------------------------- Local folder

def local_check_folder(path: str) -> str:
    if not config.ALLOW_LOCAL_FOLDERS:
        raise SourceError("Local folders are disabled on this server (ALLOW_LOCAL_FOLDERS=false).")
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise SourceError(f"Folder not found: {p}")
    return p.name


def local_list_images(path: str) -> list[SourceFile]:
    root = Path(path).expanduser().resolve()
    files = [
        SourceFile(str(p.relative_to(root)), str(p.relative_to(root)), None)
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith(".")
    ]
    files.sort(key=lambda f: f.name)
    return files


def local_download(root_path: str, rel: str) -> bytes:
    root = Path(root_path).expanduser().resolve()
    target = (root / rel).resolve()
    if root not in target.parents:
        raise SourceError("Invalid path")
    return target.read_bytes()


# ---------------------------------------------------------------- Dispatch

def check_folder(source_type: str, ref: str) -> str:
    return drive_check_folder(ref) if source_type == "drive" else local_check_folder(ref)


def list_images(source_type: str, ref: str) -> list[SourceFile]:
    return drive_list_images(ref) if source_type == "drive" else local_list_images(ref)


def download(source_type: str, ref: str, source_id: str) -> bytes:
    return drive_download(source_id) if source_type == "drive" else local_download(ref, source_id)
