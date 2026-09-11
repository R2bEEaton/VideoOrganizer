"""Import media out of zip archives.

Handles the shape a bulk cloud-storage download arrives in: one or more zips
whose inner folders are named after whoever contributed them.
"""

from __future__ import annotations

import shutil
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from .. import media
from . import common


def person_from_entry(inner_path: str, library, strip_prefix: bool = True) -> str:
    """Infer the contributor from the folder structure inside the archive.

    A Drive download nests everything under the shared folder's own name, so the
    first path component is dropped: ``Trip 2026/Alex's Photos/IMG_1.HEIC``
    means Alex. Files sitting at the root have no attribution.
    """
    parts = [p for p in inner_path.replace("\\", "/").split("/") if p]
    if strip_prefix and len(parts) >= 3:
        subfolder = parts[1]
    elif not strip_prefix and len(parts) >= 2:
        subfolder = parts[0]
    else:
        return "Unknown"

    for key, name in library.people.items():
        if key.lower() in subfolder.lower():
            return name
    return subfolder


#: Zip entries at or before this are the format's own floor value (1980-01-01)
#: or a camera with no clock — not a real capture time.
ARCHIVE_EPOCH_CUTOFF = datetime(2010, 1, 1)


def entry_datetime(entry: zipfile.ZipInfo, offset: timedelta | None) -> datetime | None:
    """The archive's own timestamp for an entry, with optional correction.

    Some cameras write a dead-clock date — 2009-01-01, counting up from there —
    and the zip format's own floor is 1980-01-01. ``offset`` shifts those onto
    the real timeline, preserving their relative order. Without an offset they
    are discarded rather than believed, so the file is filed under
    ``unknown-date`` instead of 1980.
    """
    try:
        stamp = datetime(*entry.date_time)
    except (ValueError, TypeError):
        return None

    if stamp < ARCHIVE_EPOCH_CUTOFF:
        if not offset:
            return None
        stamp = stamp + offset

    return stamp if media.is_plausible(stamp) else None


def import_zip(
    library,
    conn,
    archive: Path,
    person: str | None = None,
    strip_prefix: bool = True,
    time_offset: timedelta | None = None,
    dry_run: bool = False,
    on_progress=None,
) -> dict:
    """Import every media file from one archive.

    ``person`` forces a single contributor for the whole archive (use when the
    zip came straight off someone's phone); otherwise it is inferred per entry.
    """
    archive = Path(archive)
    if not archive.exists():
        raise FileNotFoundError(f"Archive not found: {archive}")

    imported, skipped, failed = [], [], []

    with zipfile.ZipFile(archive) as zf:
        entries = [
            entry
            for entry in zf.infolist()
            if not entry.is_dir() and library.file_type(entry.filename)
        ]

        for index, entry in enumerate(entries, start=1):
            filename = Path(entry.filename).name
            owner = person or person_from_entry(entry.filename, library, strip_prefix)
            zip_time = entry_datetime(entry, time_offset)

            if dry_run:
                captured_at = zip_time.isoformat() if zip_time else None
                if already_or_note(conn, owner, filename, captured_at, skipped):
                    continue
                imported.append({"filename": filename, "person": owner, "captured_at": captured_at})
                continue

            # Extract first: the real capture time lives in the file's own
            # metadata, and the archive timestamp is only a fallback.
            staging = library.organized_dir / owner / "_staging"
            staging.mkdir(parents=True, exist_ok=True)
            staged = media.dedupe_path(staging, filename)
            try:
                with zf.open(entry) as src, open(staged, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except Exception as exc:
                failed.append({"filename": filename, "error": str(exc)})
                continue

            captured_at = media.get_capture_time(staged, library.video_exts, filename)
            if not captured_at and zip_time:
                captured_at = zip_time.replace(microsecond=0).isoformat()

            if already_or_note(conn, owner, filename, captured_at, skipped):
                staged.unlink(missing_ok=True)
                continue

            final = common.destination(library, owner, captured_at, filename)
            shutil.move(str(staged), str(final))
            common.apply_timestamp(final, captured_at)

            media_id = common.register(
                library, conn, final, owner, captured_at, source="zip", source_owner=owner
            )
            imported.append(
                {
                    "id": media_id,
                    "filename": filename,
                    "person": owner,
                    "captured_at": captured_at,
                }
            )
            if on_progress:
                on_progress(index, len(entries), filename, owner)

        cleanup_staging(library)

    return {
        "archive": str(archive),
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "dry_run": dry_run,
    }


def already_or_note(conn, person, filename, captured_at, skipped) -> bool:
    if common.already_imported(conn, person, filename, captured_at):
        skipped.append({"filename": filename, "person": person})
        return True
    return False


def cleanup_staging(library) -> None:
    for staging in library.organized_dir.glob("*/_staging"):
        try:
            next(staging.iterdir())
        except StopIteration:
            staging.rmdir()
        except OSError:
            pass


def import_zips(library, conn, archives, **kwargs) -> dict:
    """Import a batch of archives, aggregating the per-archive results."""
    results = [import_zip(library, conn, Path(a), **kwargs) for a in archives]
    return {
        "archives": [r["archive"] for r in results],
        "imported": [item for r in results for item in r["imported"]],
        "skipped": [item for r in results for item in r["skipped"]],
        "failed": [item for r in results for item in r["failed"]],
        "dry_run": kwargs.get("dry_run", False),
    }
