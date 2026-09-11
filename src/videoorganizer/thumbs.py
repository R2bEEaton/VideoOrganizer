"""Thumbnail generation.

Thumbnails are always keyed by database id. The original scripts this package
replaces had two competing conventions — one id-keyed, one stem-keyed — and the
stem-keyed one silently collided whenever two phones produced the same
``IMG_4335`` name.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

THUMB_SIZE = (512, 512)
HEIF_EXTS = {".heic", ".heif"}

#: Seek offsets tried in order for video. A clip that opens on a black frame or
#: a fade-in yields a useless all-dark thumbnail at 2s, so fall back toward 0.
VIDEO_SEEK_OFFSETS = ["2", "1", "0"]

#: An ffmpeg-produced JPEG smaller than this is almost certainly a blank frame.
MIN_USEFUL_BYTES = 3000


def load_image(path: Path):
    """Open any supported still as an orientation-corrected PIL image."""
    path = Path(path)
    if path.suffix.lower() in HEIF_EXTS:
        from pillow_heif import register_heif_opener

        register_heif_opener()

    from PIL import Image, ImageOps

    img = Image.open(path)
    return ImageOps.exif_transpose(img)


def _thumb_from_video(src: Path, dest: Path, timeout: int = 30) -> bool:
    for offset in VIDEO_SEEK_OFFSETS:
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", offset, "-i", str(src),
                    "-vframes", "1", "-vf", f"scale={THUMB_SIZE[0]}:-1",
                    "-q:v", "5", str(dest),
                ],
                capture_output=True,
                timeout=timeout,
            )
        except (subprocess.SubprocessError, OSError):
            return False
        if dest.exists() and dest.stat().st_size >= MIN_USEFUL_BYTES:
            return True
    return dest.exists()


def _thumb_from_image(src: Path, dest: Path) -> bool:
    # ffmpeg reads empty sub-images out of HEIC containers, so stills always go
    # through PIL (with pillow-heif registered for HEIC/HEIF).
    img = load_image(src)
    img.thumbnail(THUMB_SIZE)
    img.convert("RGB").save(dest, "JPEG", quality=80)
    return dest.exists()


def generate(src: Path, dest: Path, video_exts: set[str], overwrite: bool = False) -> Path | None:
    """Write a JPEG thumbnail for ``src`` to ``dest``. Returns ``dest`` or ``None``."""
    src, dest = Path(src), Path(dest)
    if dest.exists() and not overwrite:
        return dest
    if not src.exists():
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        ok = (
            _thumb_from_video(src, dest, )
            if src.suffix.lower() in video_exts
            else _thumb_from_image(src, dest)
        )
    except Exception:
        return None
    return dest if ok and dest.exists() else None


def ensure(library, media_id: int, src: Path, overwrite: bool = False) -> Path | None:
    """Ensure the id-keyed thumbnail for ``media_id`` exists."""
    return generate(src, library.thumb_path(media_id), library.video_exts, overwrite=overwrite)


def backfill(library, conn, overwrite: bool = False, limit: int = 0) -> dict:
    """Generate any missing thumbnails across the library."""
    rows = conn.execute("SELECT id, rel_path FROM media ORDER BY id").fetchall()
    made, skipped, failed = 0, 0, 0

    for row in rows:
        if limit and made >= limit:
            break
        dest = library.thumb_path(row["id"])
        if dest.exists() and not overwrite:
            skipped += 1
            continue
        try:
            src = library.resolve(row["rel_path"])
        except ValueError:
            failed += 1
            continue
        if generate(src, dest, library.video_exts, overwrite=overwrite):
            made += 1
        else:
            failed += 1

    return {"made": made, "skipped": skipped, "failed": failed, "total": len(rows)}
