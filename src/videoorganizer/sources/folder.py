"""Import media from a folder on disk."""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import media
from . import common


def import_folder(
    library,
    conn,
    source_dir: Path,
    person: str | None = None,
    recursive: bool = True,
    move: bool = False,
    person_from_subfolder: bool = False,
    dry_run: bool = False,
    on_progress=None,
) -> dict:
    """Import every media file under ``source_dir``.

    ``person_from_subfolder`` reads the contributor from each file's top-level
    folder name, which is how an "everyone dump your photos here" share drive is
    usually laid out.
    """
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise NotADirectoryError(f"Not a folder: {source_dir}")

    pattern = "**/*" if recursive else "*"
    files = sorted(
        path
        for path in source_dir.glob(pattern)
        if path.is_file() and library.file_type(path)
    )

    imported, skipped, failed = [], [], []

    for index, path in enumerate(files, start=1):
        if person:
            owner = person
        elif person_from_subfolder:
            relative = path.relative_to(source_dir)
            raw = relative.parts[0] if len(relative.parts) > 1 else "Unknown"
            owner = library.friendly_name(raw)
        else:
            owner = "Unknown"

        captured_at = media.get_capture_time(path, library.video_exts)

        if common.already_imported(conn, owner, path.name, captured_at):
            skipped.append({"filename": path.name, "person": owner})
            continue

        if dry_run:
            imported.append(
                {"filename": path.name, "person": owner, "captured_at": captured_at}
            )
            continue

        destination = common.destination(library, owner, captured_at, path.name)
        try:
            if move:
                shutil.move(str(path), str(destination))
            else:
                shutil.copy2(str(path), str(destination))
        except Exception as exc:
            failed.append({"filename": path.name, "error": str(exc)})
            continue

        common.apply_timestamp(destination, captured_at)
        media_id = common.register(
            library, conn, destination, owner, captured_at, source="folder", source_owner=owner
        )
        imported.append(
            {"id": media_id, "filename": path.name, "person": owner, "captured_at": captured_at}
        )
        if on_progress:
            on_progress(index, len(files), path.name, owner)

    return {
        "source": str(source_dir),
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "dry_run": dry_run,
    }
