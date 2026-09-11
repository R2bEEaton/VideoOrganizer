"""The ``vorg`` command line."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from . import __version__, db, search, tagging, thumbs
from .config import CONFIG_NAME, EXAMPLE_CONFIG, ConfigError, load_config


def _utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _library(args):
    return load_config(Path(args.config) if args.config else None)


def _progress(index, total, name, detail=None):
    suffix = f"  [{detail}]" if detail else ""
    print(f"  [{index}/{total}] {name}{suffix}")


def _report(result: dict, label: str) -> None:
    print(f"\n{label}")
    print(f"  imported: {len(result.get('imported', []))}")
    print(f"  skipped:  {len(result.get('skipped', []))}")
    failed = result.get("failed", [])
    print(f"  failed:   {len(failed)}")
    for item in failed[:10]:
        print(f"    ! {item.get('filename', item.get('id'))}: {item.get('error')}")
    if result.get("dry_run"):
        print("  (dry run — nothing was written)")


# ── init ──────────────────────────────────────────────────────────────────


def cmd_init(args) -> int:
    target = Path(args.directory or ".").resolve()
    target.mkdir(parents=True, exist_ok=True)
    config_path = target / CONFIG_NAME

    if config_path.exists() and not args.force:
        print(f"{config_path} already exists. Use --force to overwrite.")
        return 1

    config_path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    library = load_config(config_path)
    with db.open_db(library.database):
        pass
    library.thumbs_dir.mkdir(parents=True, exist_ok=True)
    library.organized_dir.mkdir(parents=True, exist_ok=True)

    print(f"Created {config_path}")
    print(f"Created {library.database}")
    print("\nNext:")
    print(f"  1. Edit {CONFIG_NAME} — set [tagging] subject and the [people] name map.")
    print("  2. vorg import zip <archive.zip>   (or: vorg import folder <dir>)")
    print("  3. vorg tag")
    print("  4. vorg web")
    return 0


# ── import ────────────────────────────────────────────────────────────────


def cmd_import_zip(args) -> int:
    from .sources import zips

    library = _library(args)
    offset = None
    if args.offset_days or args.offset_hours:
        offset = timedelta(days=args.offset_days, hours=args.offset_hours)

    with db.open_db(library.database) as conn:
        result = zips.import_zips(
            library,
            conn,
            args.archives,
            person=args.person,
            strip_prefix=not args.no_strip_prefix,
            time_offset=offset,
            dry_run=args.dry_run,
            on_progress=_progress if args.verbose else None,
        )
    _report(result, f"Imported from {len(args.archives)} archive(s)")
    return 0


def cmd_import_folder(args) -> int:
    from .sources import folder

    library = _library(args)
    with db.open_db(library.database) as conn:
        result = folder.import_folder(
            library,
            conn,
            Path(args.directory),
            person=args.person,
            recursive=not args.no_recursive,
            move=args.move,
            person_from_subfolder=args.person_from_subfolder,
            dry_run=args.dry_run,
            on_progress=_progress if args.verbose else None,
        )
    _report(result, f"Imported from {args.directory}")
    return 0


def cmd_import_drive(args) -> int:
    from .sources import gdrive

    library = _library(args)
    try:
        with db.open_db(library.database) as conn:
            result = gdrive.sync(
                library,
                conn,
                dry_run=args.dry_run,
                cached=args.cached,
                on_progress=_progress if args.verbose else None,
            )
    except gdrive.DriveNotConfigured as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\nDrive: {result['drive_total']} media files")
    print(f"Local: {result['local_total']} files")
    print(f"New:   {len(result['new'])}")
    for item in result["new"][:50]:
        print(f"  {item['filename']:45s} {item['person']:15s} {item['created']}")
    if args.dry_run:
        print("\n(dry run — nothing downloaded)")
    else:
        print(f"\nImported {len(result['imported'])}, failed {len(result['failed'])}")
    return 0


# ── maintenance ───────────────────────────────────────────────────────────


def cmd_thumbs(args) -> int:
    library = _library(args)
    with db.open_db(library.database) as conn:
        result = thumbs.backfill(library, conn, overwrite=args.overwrite, limit=args.limit)
    print(
        f"Thumbnails: {result['made']} made, {result['skipped']} already present, "
        f"{result['failed']} failed (of {result['total']})"
    )
    return 0


def cmd_fix_dates(args) -> int:
    """Re-read capture times and refile anything that lands on a new date."""
    import shutil

    from . import media as media_mod
    from .sources import common

    library = _library(args)
    fixed = moved = 0

    with db.open_db(library.database) as conn:
        query = "SELECT id, filename, rel_path, person, captured_at FROM media"
        if not args.all:
            query += " WHERE captured_at IS NULL"

        rows = conn.execute(query).fetchall()
        print(f"Checking {len(rows)} file(s)...")

        for row in rows:
            try:
                path = library.resolve(row["rel_path"])
            except ValueError:
                continue
            if not path.exists():
                continue

            captured_at = media_mod.get_capture_time(path, library.video_exts, row["filename"])
            if not captured_at or captured_at == row["captured_at"]:
                continue

            destination = common.destination(
                library, row["person"] or "Unknown", captured_at, path.name
            )
            if destination.parent != path.parent:
                shutil.move(str(path), str(destination))
                conn.execute(
                    "UPDATE media SET captured_at = ?, rel_path = ? WHERE id = ?",
                    (captured_at, library.relative(destination), row["id"]),
                )
                moved += 1
            else:
                conn.execute(
                    "UPDATE media SET captured_at = ? WHERE id = ?", (captured_at, row["id"])
                )
            fixed += 1
        conn.commit()

    print(f"Updated {fixed} date(s); refiled {moved} file(s).")
    return 0


def cmd_people(args) -> int:
    from .sources import gdrive

    library = _library(args)
    with db.open_db(library.database) as conn:
        if args.apply:
            updated = gdrive.update_people(library, conn)
            print(f"Renamed {updated} row(s) using the [people] map.")
        for entry in db.stats(conn)["by_person"]:
            print(f"  {entry['person']:24s} {entry['count']}")
    return 0


# ── query ─────────────────────────────────────────────────────────────────


def cmd_tag(args) -> int:
    library = _library(args)
    with db.open_db(library.database) as conn:
        try:
            result = tagging.tag_library(
                library,
                conn,
                limit=args.limit,
                retag=args.retag,
                on_progress=(
                    (lambda i, t, n, s, tags: _progress(i, t, n, s))
                    if not args.quiet
                    else None
                ),
            )
        except tagging.TaggingError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(
        f"\nTagged {result['tagged']} of {result['considered']} "
        f"({result['failed']} failed, {result['missing']} missing). "
        "Failures stay untagged and are retried next run."
    )
    return 0


def cmd_search(args) -> int:
    library = _library(args)
    with db.open_db(library.database) as conn:
        results = search.search(
            conn,
            query=" ".join(args.query),
            person=args.person,
            date=args.date,
            file_type=args.type,
            limit=args.top,
        )

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    if not results:
        print("No results.")
        return 0

    print(f"\n{len(results)} result(s):\n")
    for item in results:
        print(f"  [{(item['file_type'] or '?').upper()}] {item['filename']}")
        print(f"    person: {item['person'] or 'unknown'}   date: {item['captured_date'] or '?'}")
        if item["tags"]:
            print(f"    tags:   {', '.join(item['tags'])}")
        if item["description"]:
            print(f"    desc:   {item['description']}")
        print(f"    path:   {library.root / item['rel_path']}")
        print()
    return 0


def cmd_stats(args) -> int:
    library = _library(args)
    with db.open_db(library.database) as conn:
        overview = db.stats(conn)

    if args.json:
        print(json.dumps(overview, indent=2))
        return 0

    print(f"\n{library.web.title}  —  {library.root}")
    print("─" * 46)
    print(f"Total files: {overview['total']}")
    print(f"Tagged:      {overview['tagged']}  (untagged: {overview['untagged']})")
    print(f"Undated:     {overview['undated']}")
    print("\nBy type:")
    for entry in overview["by_type"]:
        print(f"  {entry['type']:12s} {entry['count']}")
    print("\nBy person:")
    for entry in overview["by_person"]:
        print(f"  {entry['person']:24s} {entry['count']}")
    if overview["by_date"]:
        print("\nBy date:")
        for entry in overview["by_date"]:
            print(f"  {entry['date']}  {entry['count']}")
    return 0


# ── output ────────────────────────────────────────────────────────────────


def cmd_export(args) -> int:
    from . import export as export_mod

    library = _library(args)

    if args.queue:
        queue = json.loads(Path(args.queue).read_text(encoding="utf-8"))
        if isinstance(queue, dict):
            queue = queue.get("items") or queue.get("written") or []
    elif args.ids:
        queue = [{"id": int(i)} for i in args.ids]
    else:
        print("error: pass --ids or --queue.", file=sys.stderr)
        return 1

    with db.open_db(library.database) as conn:
        export_dir = export_mod.resolve_export_dir(library, args.to)
        result = export_mod.export_queue(
            library, conn, queue, export_dir, on_progress=_progress if args.verbose else None
        )

    print(f"\nExported {len(result['written'])} item(s) to {result['export_dir']}")
    for item in result["failed"]:
        print(f"  ! {item.get('filename', item.get('id'))}: {item['error']}")
    print(f"Manifest: {result['manifest']}")
    return 0 if result["written"] else 1


def cmd_recap(args) -> int:
    from . import recap as recap_mod

    library = _library(args)
    try:
        with db.open_db(library.database) as conn:
            summary = recap_mod.build(
                library, conn, Path(args.export_dir), keep_work=args.keep_work
            )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\nBuilt {summary['output']}")
    print(f"  {summary['selected_count']} of {summary['asset_count']} items used")
    for chapter in summary["chapters"]:
        print(f"  {chapter['title']:28s} {chapter['selected']} item(s)")
    return 0


def cmd_web(args) -> int:
    try:
        from .web import run
    except ImportError:
        print(
            'error: the web UI needs Flask. Run: pip install "videoorganizer[web]"',
            file=sys.stderr,
        )
        return 1

    library = _library(args)
    run(library, host=args.host, port=args.port, debug=args.debug)
    return 0


# ── parser ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vorg",
        description="Organize, tag, browse, and cut recaps from a pile of photos and video.",
    )
    parser.add_argument("--version", action="version", version=f"videoorganizer {__version__}")
    parser.add_argument("--config", help=f"Path to {CONFIG_NAME} (default: search upward from cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Create a config file and database in a folder")
    p.add_argument("directory", nargs="?", default=".")
    p.add_argument("--force", action="store_true", help="Overwrite an existing config")
    p.set_defaults(func=cmd_init)

    imp = sub.add_parser("import", help="Import media from a source")
    imp_sub = imp.add_subparsers(dest="source", required=True)

    p = imp_sub.add_parser("zip", help="Import from zip archive(s)")
    p.add_argument("archives", nargs="+")
    p.add_argument("--person", help="Attribute everything to one person")
    p.add_argument(
        "--no-strip-prefix",
        action="store_true",
        help="Treat the first folder inside the zip as a person, not a wrapper",
    )
    p.add_argument(
        "--offset-days", type=int, default=0, help="Correct dead-clock timestamps by N days"
    )
    p.add_argument("--offset-hours", type=int, default=0, help="...and by N hours")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_import_zip)

    p = imp_sub.add_parser("folder", help="Import from a folder on disk")
    p.add_argument("directory")
    p.add_argument("--person", help="Attribute everything to one person")
    p.add_argument(
        "--person-from-subfolder",
        action="store_true",
        help="Read the contributor from each file's top-level folder name",
    )
    p.add_argument("--no-recursive", action="store_true")
    p.add_argument("--move", action="store_true", help="Move instead of copy")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_import_folder)

    p = imp_sub.add_parser("drive", help="Sync from a shared Google Drive folder")
    p.add_argument("--dry-run", action="store_true", help="List new files without downloading")
    p.add_argument("--cached", action="store_true", help="Use the cached listing, skip the API")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_import_drive)

    p = sub.add_parser("thumbs", help="Generate any missing thumbnails")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_thumbs)

    p = sub.add_parser("fix-dates", help="Re-read capture times and refile by date")
    p.add_argument("--all", action="store_true", help="Recheck every file, not just undated ones")
    p.set_defaults(func=cmd_fix_dates)

    p = sub.add_parser("people", help="Show contributors; re-apply the [people] name map")
    p.add_argument("--apply", action="store_true", help="Rename rows using the map")
    p.set_defaults(func=cmd_people)

    p = sub.add_parser("tag", help="Describe and tag media with a local vision model")
    p.add_argument("--limit", type=int, default=0, help="Stop after N files")
    p.add_argument("--retag", action="store_true", help="Re-tag files that already have tags")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_tag)

    p = sub.add_parser("search", help="Search descriptions and tags")
    p.add_argument("query", nargs="*")
    p.add_argument("--person")
    p.add_argument("--date", help="Date prefix, e.g. 2026-03-09")
    p.add_argument("--type", choices=["image", "video"])
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("stats", help="Library overview")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("export", help="Export an ordered queue with watermarks")
    p.add_argument("--ids", nargs="+", help="Media ids, in the order you want them")
    p.add_argument("--queue", help="JSON file of queue entries (as the web UI produces)")
    p.add_argument("--to", required=True, help="Destination folder")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("recap", help="Build a recap video from an exported folder")
    p.add_argument("export_dir")
    p.add_argument("--keep-work", action="store_true", help="Keep intermediate segments")
    p.set_defaults(func=cmd_recap)

    p = sub.add_parser("web", help="Start the local browser UI")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_web)

    return parser


def main(argv=None) -> int:
    _utf8_stdout()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
