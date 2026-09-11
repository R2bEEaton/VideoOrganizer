from datetime import datetime

import pytest

from videoorganizer import media


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("VID_20260309_143721.mp4", datetime(2026, 3, 9, 14, 37, 21)),
        ("IMG_20260309_164151.jpg", datetime(2026, 3, 9, 16, 41, 51)),
        ("PXL_20260309_143721123.jpg", datetime(2026, 3, 9, 14, 37, 21)),
        ("Photo Mar 08 2026, 2 13 32 PM.heic", datetime(2026, 3, 8, 14, 13, 32)),
        ("Photo Mar 08 2026, 2 13 32 AM.heic", datetime(2026, 3, 8, 2, 13, 32)),
        ("2026-03-09 14.37.21.jpg", datetime(2026, 3, 9, 14, 37, 21)),
        ("IMG-20260309-WA0001.jpg", datetime(2026, 3, 9, 12, 0, 0)),
    ],
)
def test_extract_date_from_filename(filename, expected):
    assert media.extract_date_from_filename(filename) == expected


@pytest.mark.parametrize(
    "filename",
    ["IMG_4335.HEIC", "random.mp4", "", "screenshot.png", "VID_99999999_999999.mp4"],
)
def test_extract_date_returns_none_for_unrecognized(filename):
    assert media.extract_date_from_filename(filename) is None


def test_noon_is_used_for_date_only_names():
    """A date-only filename has no time; midday avoids sliding into an adjacent day."""
    assert media.extract_date_from_filename("IMG-20260309-WA0001.jpg").hour == 12


def test_implausible_dates_are_rejected():
    assert not media.is_plausible(datetime(1970, 1, 1))
    assert not media.is_plausible(None)
    assert media.is_plausible(datetime(2026, 3, 9))


def test_epoch_filename_is_not_treated_as_a_capture_time():
    """Cameras with a dead clock emit 1970 dates. They are not real."""
    assert media.extract_date_from_filename("IMG_19700101_000000.jpg") is None


def test_date_folder():
    assert media.date_folder("2026-03-09T14:37:21") == "2026-03-09"
    assert media.date_folder(None) == "unknown-date"
    assert media.date_folder("") == "unknown-date"


def test_dedupe_path_suffixes_collisions(tmp_path):
    assert media.dedupe_path(tmp_path, "a.jpg") == tmp_path / "a.jpg"

    (tmp_path / "a.jpg").write_bytes(b"x")
    assert media.dedupe_path(tmp_path, "a.jpg") == tmp_path / "a (1).jpg"

    (tmp_path / "a (1).jpg").write_bytes(b"x")
    assert media.dedupe_path(tmp_path, "a.jpg") == tmp_path / "a (2).jpg"


def _insert(conn, **kwargs):
    from videoorganizer import db as db_mod

    defaults = {
        "filename": "IMG_1.HEIC",
        "rel_path": "organized/A/2026-03-09/IMG_1.HEIC",
        "person": "Alex",
        "captured_at": "2026-03-09T10:00:00",
        "file_type": "image",
    }
    defaults.update(kwargs)
    return db_mod.insert_media(conn, **defaults)


def test_live_photo_pairs_same_person_same_day(conn):
    _insert(conn)
    _insert(
        conn,
        filename="IMG_1.MOV",
        rel_path="organized/A/2026-03-09/IMG_1.MOV",
        file_type="video",
    )
    row = conn.execute("SELECT * FROM media WHERE filename = 'IMG_1.HEIC'").fetchone()
    assert media.find_live_photo(conn, dict(row)) == "organized/A/2026-03-09/IMG_1.MOV"


def test_live_photo_does_not_pair_across_people(conn):
    """Different phones hand out the same IMG_ numbers constantly."""
    _insert(conn)
    _insert(
        conn,
        filename="IMG_1.MOV",
        rel_path="organized/B/2026-03-09/IMG_1.MOV",
        person="Blair",
        file_type="video",
    )
    row = conn.execute("SELECT * FROM media WHERE filename = 'IMG_1.HEIC'").fetchone()
    assert media.find_live_photo(conn, dict(row)) is None


def test_live_photo_does_not_pair_across_distant_dates(conn):
    _insert(conn)
    _insert(
        conn,
        filename="IMG_1.MOV",
        rel_path="organized/A/2026-05-20/IMG_1.MOV",
        captured_at="2026-05-20T10:00:00",
        file_type="video",
    )
    row = conn.execute("SELECT * FROM media WHERE filename = 'IMG_1.HEIC'").fetchone()
    assert media.find_live_photo(conn, dict(row)) is None


def test_live_photo_ignores_non_heic(conn):
    _insert(conn, filename="IMG_1.JPG", rel_path="organized/A/2026-03-09/IMG_1.JPG")
    row = conn.execute("SELECT * FROM media WHERE filename = 'IMG_1.JPG'").fetchone()
    assert media.find_live_photo(conn, dict(row)) is None
