"""Reading capture times out of media files, and pairing Live Photos.

The capture-time chain tries, in order: format-specific EXIF, ffprobe container
metadata, then the filename itself. Phones and cameras disagree about where they
put the timestamp, and plenty of files have none at all.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

#: Timestamps at or before this are camera defaults (dead clock battery,
#: epoch-zero GoPro files), not real capture times.
IMPLAUSIBLE_BEFORE = datetime(1990, 1, 1)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

#: EXIF tag ids: DateTimeOriginal, DateTimeDigitized, DateTime.
_EXIF_DATE_TAGS = (36867, 36868, 306)

FILENAME_PATTERNS = [
    # Android: VID_20260309_143721, IMG_20260309_164151, PXL_20260309_143721123
    r"(?:VID|IMG|PXL)[_-](\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})",
    # Generic burst/screenshot: 2026-03-09 14.37.21 or 2026_03_09_14_37_21
    r"(\d{4})[-_.](\d{2})[-_.](\d{2})[ _T](\d{2})[-_.](\d{2})[-_.](\d{2})",
    # WhatsApp / date-only: IMG-20260309-WA0001
    r"(?:IMG|VID)[_-](\d{4})(\d{2})(\d{2})[_-]WA",
]

#: iOS share-sheet naming: "Photo Mar 08 2026, 2 13 32 PM"
_IOS_PATTERN = r"(?:Photo|Video) (\w{3})\w* (\d{1,2}) (\d{4}), (\d{1,2}) (\d{1,2}) (\d{1,2}) ([AP]M)"


def is_plausible(dt: datetime | None) -> bool:
    """Reject placeholder timestamps a camera invents when it has no clock."""
    return bool(dt) and dt > IMPLAUSIBLE_BEFORE


def extract_date_from_filename(filename: str) -> datetime | None:
    """Pull a datetime out of common camera filename conventions."""
    name = Path(filename).stem

    match = re.search(_IOS_PATTERN, name)
    if match:
        mon, day, year, hour, minute, second, meridiem = match.groups()
        month = _MONTHS.get(mon[:3].lower())
        if month:
            hour_24 = int(hour) % 12 + (12 if meridiem.upper() == "PM" else 0)
            try:
                return datetime(int(year), month, int(day), hour_24, int(minute), int(second))
            except ValueError:
                pass

    for pattern in FILENAME_PATTERNS:
        match = re.search(pattern, name, re.IGNORECASE)
        if not match:
            continue
        parts = [int(g) for g in match.groups()]
        if len(parts) == 3:  # date only
            parts += [12, 0, 0]
        try:
            candidate = datetime(*parts)
        except ValueError:
            continue
        if is_plausible(candidate):
            return candidate
    return None


def _read_exif_date(path: Path, register_heif: bool = False) -> datetime | None:
    try:
        if register_heif:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        from PIL import Image

        with Image.open(path) as img:
            exif = img.getexif()
        for tag in _EXIF_DATE_TAGS:
            value = exif.get(tag)
            if not value or not isinstance(value, str):
                continue
            try:
                candidate = datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S")
            except ValueError:
                continue
            if is_plausible(candidate):
                return candidate
    except Exception:
        pass
    return None


def read_exif_date(path: Path) -> datetime | None:
    """EXIF date for a PIL-readable still (JPEG, PNG, DNG, TIFF)."""
    return _read_exif_date(path)


def read_heic_date(path: Path) -> datetime | None:
    """EXIF date for HEIC/HEIF, which needs the pillow-heif opener registered."""
    return _read_exif_date(path, register_heif=True)


def read_ffprobe_date(path: Path, timeout: int = 15) -> datetime | None:
    """Container ``creation_time``, checked in format tags then stream tags.

    Works for video and, as a last resort, for image containers that PIL cannot
    read metadata from.
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_entries", "format_tags=creation_time:stream_tags=creation_time",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return None

    raw = ((data.get("format") or {}).get("tags") or {}).get("creation_time", "")
    if not raw:
        for stream in data.get("streams") or []:
            raw = ((stream.get("tags") or {}).get("creation_time")) or ""
            if raw:
                break
    if not raw:
        return None

    try:
        candidate = datetime.fromisoformat(raw.rstrip("Z").split(".")[0])
    except ValueError:
        return None
    return candidate if is_plausible(candidate) else None


def ffprobe_duration(path: Path, timeout: int = 30) -> float | None:
    """Duration in seconds, or ``None`` if ffprobe cannot read the file."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_entries", "format=duration", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return float(json.loads(proc.stdout)["format"]["duration"])
    except Exception:
        return None


def get_capture_time(path: Path, video_exts: set[str], filename: str | None = None) -> str | None:
    """Best-effort ISO8601 capture time for a file.

    Order: format-specific EXIF, then the filename, then a generic ffprobe read.
    Returns ``None`` when nothing plausible is found — the caller files those
    under ``unknown-date`` rather than inventing a timestamp.
    """
    path = Path(path)
    ext = path.suffix.lower()
    dt = None

    if ext in video_exts:
        dt = read_ffprobe_date(path)
    elif ext in {".heic", ".heif"}:
        dt = read_heic_date(path)
    else:
        dt = read_exif_date(path)

    if dt is None:
        dt = extract_date_from_filename(filename or path.name)
    if dt is None:
        dt = read_ffprobe_date(path)

    return dt.isoformat() if is_plausible(dt) else None


def date_folder(captured_at: str | None) -> str:
    """The ``YYYY-MM-DD`` folder an item belongs in."""
    return captured_at[:10] if captured_at else "unknown-date"


def dedupe_path(dest_dir: Path, filename: str) -> Path:
    """A non-colliding path in ``dest_dir``, suffixing ``(1)``, ``(2)``, ..."""
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    index = 1
    while True:
        candidate = dest_dir / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def find_live_photo(conn, media_row, image_exts: set[str] | None = None) -> str | None:
    """Find the ``.MOV`` half of a Live Photo, if one exists.

    An iPhone Live Photo is a HEIC still plus a short MOV sharing the filename
    stem. Matching on the stem alone is not enough: different phones hand out the
    same sequential ``IMG_4335`` numbers constantly, so a same-stem match from
    another person on another day is a false pairing. Requiring the same person
    and a capture time within a day of each other is what makes this safe.
    """
    filename = media_row["filename"] if not isinstance(media_row, dict) else media_row.get("filename")
    if not filename or Path(filename).suffix.lower() not in {".heic", ".heif"}:
        return None

    get = media_row.get if isinstance(media_row, dict) else media_row.__getitem__
    stem = Path(filename).stem
    try:
        person = get("person") or ""
        captured = (get("captured_at") or "")[:10]
    except (KeyError, IndexError):
        return None

    row = conn.execute(
        """
        SELECT rel_path FROM media
        WHERE (filename = ? OR filename = ?)
          AND person IS ?
          AND ABS(JULIANDAY(substr(captured_at, 1, 10)) - JULIANDAY(?)) <= 1
        LIMIT 1
        """,
        (f"{stem}.MOV", f"{stem}.mov", person, captured),
    ).fetchone()
    return row[0].replace("\\", "/") if row else None
