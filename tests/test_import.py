"""End-to-end import tests against real files on disk."""

import zipfile
from datetime import datetime, timedelta

from videoorganizer import db
from videoorganizer.sources import common, folder, zips


def make_jpeg(path, color=(120, 160, 200)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color).save(path, "JPEG")
    return path


# ── Person inference ──────────────────────────────────────────────────────


def test_person_comes_from_the_second_folder_level(library):
    library.people["somelogin123"] = "Alex"
    assert zips.person_from_entry("Trip 2026/somelogin123/IMG_1.HEIC", library) == "Alex"


def test_unmapped_folder_names_pass_through(library):
    assert zips.person_from_entry("Trip 2026/Jordan's Pics/IMG_1.HEIC", library) == "Jordan's Pics"


def test_files_at_the_archive_root_are_unattributed(library):
    assert zips.person_from_entry("Trip 2026/IMG_1.HEIC", library) == "Unknown"


def test_no_strip_prefix_reads_the_first_level(library):
    assert zips.person_from_entry("Alex/IMG_1.HEIC", library, strip_prefix=False) == "Alex"


# ── Zip import ────────────────────────────────────────────────────────────


def test_zip_import_files_by_person_and_date(library, conn, tmp_path):
    source = make_jpeg(tmp_path / "src" / "IMG_20260309_143721.jpg")
    archive = tmp_path / "trip.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(source, "Trip 2026/Alex/IMG_20260309_143721.jpg")

    result = zips.import_zip(library, conn, archive)

    assert len(result["imported"]) == 1
    row = conn.execute("SELECT * FROM media").fetchone()
    assert row["person"] == "Alex"
    assert row["captured_at"].startswith("2026-03-09")
    assert "2026-03-09" in row["rel_path"]
    assert library.resolve(row["rel_path"]).exists()


def test_zip_import_is_idempotent(library, conn, tmp_path):
    source = make_jpeg(tmp_path / "src" / "IMG_20260309_143721.jpg")
    archive = tmp_path / "trip.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(source, "Trip 2026/Alex/IMG_20260309_143721.jpg")

    zips.import_zip(library, conn, archive)
    second = zips.import_zip(library, conn, archive)

    assert second["imported"] == []
    assert len(second["skipped"]) == 1
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1


def test_dry_run_writes_nothing(library, conn, tmp_path):
    source = make_jpeg(tmp_path / "src" / "IMG_20260309_143721.jpg")
    archive = tmp_path / "trip.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(source, "Trip 2026/Alex/IMG_20260309_143721.jpg")

    result = zips.import_zip(library, conn, archive, dry_run=True)

    assert len(result["imported"]) == 1
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


def test_non_media_entries_are_ignored(library, conn, tmp_path):
    archive = tmp_path / "trip.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Trip 2026/Alex/notes.txt", "hello")
        zf.writestr("Trip 2026/Alex/metadata.json", "{}")

    assert zips.import_zip(library, conn, archive)["imported"] == []


def test_undated_files_land_in_unknown_date(library, conn, tmp_path):
    source = make_jpeg(tmp_path / "src" / "nodate.jpg")
    archive = tmp_path / "trip.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("Trip 2026/Alex/nodate.jpg", date_time=(1980, 1, 1, 0, 0, 0))
        zf.writestr(info, source.read_bytes())

    zips.import_zip(library, conn, archive)
    assert "unknown-date" in conn.execute("SELECT rel_path FROM media").fetchone()[0]


def test_dead_clock_timestamps_can_be_offset(library, conn, tmp_path):
    """A camera stuck at 2009 can be shifted onto the real timeline."""
    source = make_jpeg(tmp_path / "src" / "nodate.jpg")
    archive = tmp_path / "trip.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("Trip 2026/Alex/nodate.jpg", date_time=(2009, 1, 1, 0, 0, 0))
        zf.writestr(info, source.read_bytes())

    offset = datetime(2026, 3, 9) - datetime(2009, 1, 1)
    zips.import_zip(library, conn, archive, time_offset=offset)

    assert conn.execute("SELECT captured_at FROM media").fetchone()[0].startswith("2026-03-09")


def test_same_filename_from_two_people_both_import(library, conn, tmp_path):
    """Two iPhones both produce IMG_4335. Neither should overwrite the other."""
    source = make_jpeg(tmp_path / "src" / "IMG_4335.jpg")
    archive = tmp_path / "trip.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(source, "Trip 2026/Alex/IMG_4335.jpg")
        zf.write(source, "Trip 2026/Blair/IMG_4335.jpg")

    zips.import_zip(library, conn, archive)

    rows = conn.execute("SELECT person, rel_path FROM media ORDER BY person").fetchall()
    assert len(rows) == 2
    assert rows[0]["rel_path"] != rows[1]["rel_path"]


def test_no_staging_folders_are_left_behind(library, conn, tmp_path):
    source = make_jpeg(tmp_path / "src" / "IMG_20260309_143721.jpg")
    archive = tmp_path / "trip.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(source, "Trip 2026/Alex/IMG_20260309_143721.jpg")

    zips.import_zip(library, conn, archive)
    assert list(library.organized_dir.glob("*/_staging")) == []


# ── Folder import ─────────────────────────────────────────────────────────


def test_folder_import_copies_by_default(library, conn, tmp_path):
    source = make_jpeg(tmp_path / "src" / "IMG_20260309_143721.jpg")

    result = folder.import_folder(library, conn, tmp_path / "src", person="Alex")

    assert len(result["imported"]) == 1
    assert source.exists()
    assert conn.execute("SELECT person FROM media").fetchone()[0] == "Alex"


def test_folder_import_can_move(library, conn, tmp_path):
    source = make_jpeg(tmp_path / "src" / "IMG_20260309_143721.jpg")
    folder.import_folder(library, conn, tmp_path / "src", person="Alex", move=True)
    assert not source.exists()


def test_folder_import_reads_person_from_subfolder(library, conn, tmp_path):
    library.people["somelogin123"] = "Alex"
    make_jpeg(tmp_path / "src" / "somelogin123" / "IMG_20260309_143721.jpg")

    folder.import_folder(library, conn, tmp_path / "src", person_from_subfolder=True)

    assert conn.execute("SELECT person FROM media").fetchone()[0] == "Alex"


def test_folder_import_skips_non_media(library, conn, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "notes.txt").write_text("hello")
    assert folder.import_folder(library, conn, tmp_path / "src", person="Alex")["imported"] == []


def test_thumbnail_is_generated_on_import(library, conn, tmp_path):
    make_jpeg(tmp_path / "src" / "IMG_20260309_143721.jpg")
    folder.import_folder(library, conn, tmp_path / "src", person="Alex")

    media_id = conn.execute("SELECT id FROM media").fetchone()[0]
    assert library.thumb_path(media_id).exists()


# ── Duplicate detection ───────────────────────────────────────────────────


def test_duplicate_check_is_scoped_to_the_person(conn):
    db.insert_media(
        conn,
        filename="IMG_1.jpg",
        rel_path="organized/Alex/2026-03-09/IMG_1.jpg",
        person="Alex",
        captured_at="2026-03-09T10:00:00",
        file_type="image",
    )
    assert common.already_imported(conn, "Alex", "IMG_1.jpg", "2026-03-09T10:00:00")
    assert not common.already_imported(conn, "Blair", "IMG_1.jpg", "2026-03-09T10:00:00")
