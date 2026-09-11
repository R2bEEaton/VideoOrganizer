"""Export an ordered queue of media into a folder, with a manifest.

The manifest is the handoff to :mod:`videoorganizer.recap`: it records what was
exported, in what order, and who each item belongs to.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import media as media_mod
from . import watermark
from .db import parse_tags

MANIFEST_NAME = "recap_manifest.json"


def resolve_export_dir(library, raw: str | Path, allow_outside: bool = True) -> Path:
    """Resolve and create an export folder.

    Relative paths land under the library's configured exports folder. Absolute
    paths are honored only when ``allow_outside`` is set — the web server passes
    ``False`` for non-local requests so a browser cannot write anywhere on disk.
    """
    if not raw:
        raise ValueError("An export folder is required.")

    path = Path(str(raw)).expanduser()
    if path.is_absolute():
        path = path.resolve()
        if not allow_outside and not path.is_relative_to(library.root.resolve()):
            raise ValueError("Absolute export paths are only allowed from localhost.")
    else:
        path = (library.exports_dir / path).resolve()

    if path.exists() and not path.is_dir():
        raise ValueError("Export destination must be a folder.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch_items(conn, ids: list[int]) -> dict[int, dict]:
    """Load the given media rows, with tags decoded and Live Photo pairing done."""
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM media WHERE id IN ({placeholders})", ids
    ).fetchall()

    items = {}
    for row in rows:
        item = dict(row)
        item["tags"] = parse_tags(item.get("tags"))
        item["live_photo_mov"] = media_mod.find_live_photo(conn, item)
        items[item["id"]] = item
    return items


def export_queue(
    library,
    conn,
    queue: list[dict],
    export_dir: Path,
    on_progress=None,
) -> dict:
    """Export an ordered queue.

    Each entry is ``{"id": int, "owner_name": str?, "use_live_photo": bool?}``.
    ``owner_name`` overrides the stored contributor for the watermark, which is
    how you fix an attribution without touching the database.
    """
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)

    ids = [entry["id"] for entry in queue if entry.get("id") is not None]
    items = fetch_items(conn, ids)

    written, failed = [], []

    for index, entry in enumerate(queue, start=1):
        item = items.get(entry.get("id"))
        if not item:
            failed.append({"id": entry.get("id"), "error": "Media not found in database."})
            continue

        owner_name = (entry.get("owner_name") or item.get("person") or "Unknown").strip() or "Unknown"
        use_live = bool(entry.get("use_live_photo")) and bool(item.get("live_photo_mov"))

        source_rel = item["live_photo_mov"] if use_live else item["rel_path"]
        try:
            source = library.resolve(source_rel)
        except ValueError as exc:
            failed.append({"id": item["id"], "filename": item["filename"], "error": str(exc)})
            continue

        if not source.exists():
            failed.append(
                {"id": item["id"], "filename": item["filename"], "error": "Source file missing."}
            )
            continue

        kind = "video" if (use_live or library.file_type(source) == "video") else "image"
        name = watermark.output_name(index, owner_name, item["filename"], kind)
        destination = export_dir / name

        try:
            rendered = watermark.export_item(source, destination, owner_name, kind, library.export)
        except Exception as exc:
            failed.append({"id": item["id"], "filename": item["filename"], "error": str(exc)})
            continue

        written.append(
            {
                "id": item["id"],
                "order": index,
                "filename": name,
                "path": str(destination),
                "owner_name": owner_name,
                "source_path": str(source),
                "export_kind": rendered,
                "used_live_photo": use_live,
            }
        )
        if on_progress:
            on_progress(index, len(queue), name, owner_name)

    manifest_path = export_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(
            {"export_dir": str(export_dir), "written": written, "failed": failed}, indent=2
        ),
        encoding="utf-8",
    )

    return {
        "export_dir": str(export_dir),
        "written": written,
        "failed": failed,
        "manifest": str(manifest_path),
    }


def load_manifest(export_dir: Path) -> dict:
    path = Path(export_dir) / MANIFEST_NAME
    if not path.exists():
        raise FileNotFoundError(
            f"No {MANIFEST_NAME} in {export_dir}. Run `vorg export` into this folder first."
        )
    return json.loads(path.read_text(encoding="utf-8"))
