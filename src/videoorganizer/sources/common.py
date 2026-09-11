"""Shared filing logic for every import source.

All sources converge here: a file plus who contributed it plus when it was taken
becomes a row in the database and a file under
``organized/<person>/<YYYY-MM-DD>/``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .. import db, media, thumbs


def destination(library, person: str, captured_at: str | None, filename: str) -> Path:
    """Where a file belongs, with collisions resolved."""
    folder = library.organized_dir / person / media.date_folder(captured_at)
    folder.mkdir(parents=True, exist_ok=True)
    return media.dedupe_path(folder, filename)


def already_imported(conn, person: str, filename: str, captured_at: str | None) -> bool:
    """True if this person already has this filename at this timestamp.

    Matching on filename alone would wrongly reject two people's ``IMG_4335``.
    """
    row = conn.execute(
        "SELECT 1 FROM media WHERE person IS ? AND filename = ? AND captured_at IS ? LIMIT 1",
        (person, filename, captured_at),
    ).fetchone()
    return row is not None


def register(
    library,
    conn,
    path: Path,
    person: str,
    captured_at: str | None,
    source: str,
    source_id: str | None = None,
    source_owner: str | None = None,
    source_created: str | None = None,
    make_thumb: bool = True,
) -> int | None:
    """Insert a filed media file into the database and thumbnail it."""
    path = Path(path)
    media_id = db.insert_media(
        conn,
        filename=path.name,
        rel_path=library.relative(path),
        person=person,
        captured_at=captured_at,
        file_type=library.file_type(path),
        file_size=path.stat().st_size,
        source=source,
        source_id=source_id,
        source_owner=source_owner,
        source_created=source_created,
        imported_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    if media_id and make_thumb:
        thumbs.ensure(library, media_id, path)
    return media_id


def apply_timestamp(path: Path, captured_at: str | None) -> None:
    """Set the file's mtime to its capture time so file managers sort right."""
    if not captured_at:
        return
    try:
        stamp = datetime.fromisoformat(captured_at).timestamp()
        import os

        os.utime(path, (stamp, stamp))
    except (ValueError, OSError):
        pass
