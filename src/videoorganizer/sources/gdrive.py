"""Sync media from a shared Google Drive folder.

Optional. Requires ``pip install "videoorganizer[drive]"`` and a Google OAuth
desktop-app credential with read-only Drive scope.

Drive is the only place that knows who actually uploaded a file, which is what
makes per-person attribution possible for a folder everyone dumps into.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from .. import media
from . import common

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FIELDS = (
    "nextPageToken, files("
    "id, name, mimeType, size, createdTime, modifiedTime, "
    "owners(displayName,emailAddress), lastModifyingUser(displayName,emailAddress)"
    ")"
)

FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveNotConfigured(Exception):
    """Raised when Drive sync is requested without the config or deps in place."""


def _require_deps():
    try:
        from google.auth.transport.requests import Request  # noqa: F401
        from google.oauth2.credentials import Credentials  # noqa: F401
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise DriveNotConfigured(
            'Google Drive support is not installed. Run: pip install "videoorganizer[drive]"'
        ) from exc


def _resolve(library, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (library.root / path)


def get_service(library):
    """Authorize and return a Drive v3 client, refreshing the cached token."""
    _require_deps()
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if not library.drive.folder_id:
        raise DriveNotConfigured(
            "No [drive] folder_id in your config. Add the folder id from the Drive URL."
        )

    token_file = _resolve(library, library.drive.token)
    creds_file = _resolve(library, library.drive.credentials)

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_file.exists():
                raise DriveNotConfigured(
                    f"OAuth client secrets not found at {creds_file}. "
                    "Create a Desktop App credential in Google Cloud Console and save it there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    return build("drive", "v3", credentials=creds)


def list_folder(service, folder_id: str, prefix: str = ""):
    """Yield every non-folder file under ``folder_id``, recursively."""
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields=FIELDS,
                pageSize=1000,
                pageToken=page_token,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
        )
        for item in response.get("files", []):
            item["_path"] = f"{prefix}/{item['name']}" if prefix else item["name"]
            if item["mimeType"] == FOLDER_MIME:
                yield from list_folder(service, item["id"], item["_path"])
            else:
                yield item
        page_token = response.get("nextPageToken")
        if not page_token:
            break


def fetch_metadata(library, service=None, cache: bool = True) -> list[dict]:
    """Fetch the Drive file listing, optionally caching it to disk."""
    service = service or get_service(library)
    files = list(list_folder(service, library.drive.folder_id))
    if cache and library.drive.metadata_cache:
        path = _resolve(library, library.drive.metadata_cache)
        path.write_text(json.dumps(files, indent=2), encoding="utf-8")
    return files


def load_cached_metadata(library) -> list[dict]:
    path = _resolve(library, library.drive.metadata_cache)
    if not path.exists():
        raise DriveNotConfigured(f"No cached metadata at {path}. Run a sync without --cached first.")
    return json.loads(path.read_text(encoding="utf-8"))


def uploader_of(library, drive_file: dict) -> str:
    """The display name of whoever last touched the file, mapped via ``[people]``."""
    raw = (drive_file.get("lastModifyingUser") or {}).get("displayName") or ""
    if not raw:
        raw = ((drive_file.get("owners") or [{}])[0]).get("displayName") or "Unknown"
    return library.friendly_name(raw)


def created_time(drive_file: dict) -> str | None:
    raw = drive_file.get("createdTime") or ""
    return raw.rstrip("Z").split(".")[0] if raw else None


def newest_by_name(library, files: list[dict]) -> dict[str, dict]:
    """Collapse the listing to one entry per filename, keeping the newest."""
    best: dict[str, dict] = {}
    for item in files:
        if item.get("mimeType") == FOLDER_MIME or not library.file_type(item["name"]):
            continue
        current = best.get(item["name"])
        if not current or item.get("modifiedTime", "") > current.get("modifiedTime", ""):
            best[item["name"]] = item
    return best


def download(service, file_id: str, dest: Path) -> Path:
    from googleapiclient.http import MediaIoBaseDownload

    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(buffer.getvalue())
    return dest


def sync(
    library,
    conn,
    dry_run: bool = False,
    cached: bool = False,
    on_progress=None,
) -> dict:
    """Download anything in the Drive folder that is not already in the library."""
    service = None if cached else get_service(library)
    files = load_cached_metadata(library) if cached else fetch_metadata(library, service)
    candidates = newest_by_name(library, files)

    known = {row[0] for row in conn.execute("SELECT filename FROM media")}
    new_files = {name: item for name, item in candidates.items() if name not in known}

    result = {
        "drive_total": len(candidates),
        "local_total": len(known),
        "new": [
            {
                "filename": name,
                "person": uploader_of(library, item),
                "created": (item.get("createdTime") or "")[:10],
            }
            for name, item in sorted(new_files.items())
        ],
        "imported": [],
        "failed": [],
        "dry_run": dry_run,
    }

    if dry_run or not new_files:
        return result

    if service is None:
        service = get_service(library)

    for index, (name, item) in enumerate(sorted(new_files.items()), start=1):
        person = uploader_of(library, item)
        captured_at = created_time(item)
        destination = common.destination(library, person, captured_at, name)

        try:
            download(service, item["id"], destination)
        except Exception as exc:
            result["failed"].append({"filename": name, "error": str(exc)})
            continue

        # Drive's createdTime is upload time, not capture time. Now that the
        # bytes are local, the file's own metadata is authoritative.
        real_time = media.get_capture_time(destination, library.video_exts, name)
        if real_time and real_time != captured_at:
            captured_at = real_time
            corrected = common.destination(library, person, captured_at, name)
            if corrected != destination:
                destination.replace(corrected)
                destination = corrected

        common.apply_timestamp(destination, captured_at)
        media_id = common.register(
            library,
            conn,
            destination,
            person,
            captured_at,
            source="gdrive",
            source_id=item["id"],
            source_owner=((item.get("owners") or [{}])[0]).get("displayName"),
            source_created=item.get("createdTime"),
        )
        result["imported"].append(
            {"id": media_id, "filename": name, "person": person, "captured_at": captured_at}
        )
        if on_progress:
            on_progress(index, len(new_files), name, person)

    return result


def update_people(library, conn) -> int:
    """Re-apply the ``[people]`` name map to rows already in the database."""
    updated = 0
    for row in conn.execute(
        "SELECT id, person, source_owner FROM media WHERE person IS NOT NULL"
    ).fetchall():
        friendly = library.friendly_name(row["person"])
        if friendly != row["person"]:
            conn.execute("UPDATE media SET person = ? WHERE id = ?", (friendly, row["id"]))
            updated += 1
    conn.commit()
    return updated
