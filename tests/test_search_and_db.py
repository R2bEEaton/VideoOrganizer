import json

import pytest

from videoorganizer import db, search, watermark


# ── FTS query building ────────────────────────────────────────────────────


def test_terms_become_or_ed_prefix_matches():
    """A typed phrase is a description, not a demand that every word appear."""
    assert search.build_fts_query("sun set") == "sun* OR set*"


def test_fts_syntax_is_stripped_from_user_text():
    """A query like 'beach (sunset)' must not become a malformed MATCH."""
    assert '"' not in search.build_fts_query('beach "sunset" (x)')
    assert search.build_fts_query("beach (sunset)") == "beach* OR sunset*"


def test_empty_query_yields_empty_expression():
    assert search.build_fts_query("   ") == ""


# ── Schema ────────────────────────────────────────────────────────────────


def test_connect_is_idempotent(library):
    db.connect(library.database).close()
    conn = db.connect(library.database)
    assert "media" in {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()


def test_migration_adds_missing_columns(library):
    """An older database gains the newer columns without losing rows."""
    import sqlite3

    library.database.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(library.database)
    raw.execute(
        "CREATE TABLE media (id INTEGER PRIMARY KEY, filename TEXT NOT NULL, "
        "rel_path TEXT NOT NULL UNIQUE, person TEXT, captured_at TEXT, file_type TEXT, "
        "file_size INTEGER, duration_s REAL, tags TEXT, description TEXT, tagged INTEGER)"
    )
    raw.execute("INSERT INTO media (filename, rel_path) VALUES ('a.jpg', 'organized/a.jpg')")
    raw.commit()
    raw.close()

    conn = db.connect(library.database)
    assert {"source", "source_id", "imported_at"} <= db.columns(conn)
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1
    conn.close()


def test_parse_tags_tolerates_bad_data():
    assert db.parse_tags('["a","b"]') == ["a", "b"]
    assert db.parse_tags(None) == []
    assert db.parse_tags("not json") == []
    assert db.parse_tags('{"a": 1}') == []
    assert db.parse_tags(["already", "a", "list"]) == ["already", "a", "list"]


def test_insert_is_idempotent_on_path(conn):
    first = db.insert_media(
        conn, filename="a.jpg", rel_path="organized/a.jpg", person="Alex", file_type="image"
    )
    second = db.insert_media(
        conn, filename="a.jpg", rel_path="organized/a.jpg", person="Alex", file_type="image"
    )
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1


# ── Search behavior ───────────────────────────────────────────────────────


@pytest.fixture
def populated(conn):
    rows = [
        ("beach.jpg", "Alex", "2026-03-09T10:00:00", "image", ["beach", "sunset"], "A beach at sunset."),
        ("jungle.mp4", "Blair", "2026-03-10T10:00:00", "video", ["jungle", "hiking"], "Hiking in the jungle."),
        ("boat.jpg", "Alex", "2026-03-10T11:00:00", "image", ["boat", "ocean"], "A boat on the ocean."),
    ]
    for filename, person, captured, file_type, tags, description in rows:
        media_id = db.insert_media(
            conn,
            filename=filename,
            rel_path=f"organized/{person}/{captured[:10]}/{filename}",
            person=person,
            captured_at=captured,
            file_type=file_type,
        )
        db.set_tags(conn, media_id, tags, description)
    return conn


def test_search_finds_by_tag(populated):
    assert [r["filename"] for r in search.search(populated, "beach")] == ["beach.jpg"]


def test_search_finds_by_description_prefix(populated):
    assert [r["filename"] for r in search.search(populated, "hik")] == ["jungle.mp4"]


def test_search_filters_combine(populated):
    results = search.search(populated, "", person="Alex", date="2026-03-10")
    assert [r["filename"] for r in results] == ["boat.jpg"]


def test_search_filters_by_type(populated):
    assert [r["filename"] for r in search.search(populated, "", file_type="video")] == ["jungle.mp4"]


def test_empty_query_returns_everything_in_date_order(populated):
    assert [r["filename"] for r in search.search(populated, "")] == [
        "beach.jpg",
        "jungle.mp4",
        "boat.jpg",
    ]


def test_search_decodes_tags(populated):
    assert search.search(populated, "beach")[0]["tags"] == ["beach", "sunset"]


def test_search_pagination(populated):
    assert len(search.search(populated, "", limit=2)) == 2
    assert len(search.search(populated, "", limit=2, offset=2)) == 1


def test_like_fallback_catches_what_fts_misses(populated):
    """Searching a filename works even though filenames are not in the FTS index."""
    assert [r["filename"] for r in search.search(populated, "boat.jpg")] == ["boat.jpg"]


def test_a_natural_phrase_finds_partial_matches(populated):
    """Not every word of 'sunset on the water' is a tag. It should still hit."""
    results = search.search(populated, "sunset on the water")
    assert "beach.jpg" in [r["filename"] for r in results]


def test_best_match_ranks_first(populated):
    """The item matching the most terms should lead the results."""
    results = search.search(populated, "jungle hiking")
    assert results[0]["filename"] == "jungle.mp4"


def test_an_unmatched_query_still_returns_nothing(populated):
    assert search.search(populated, "spelunking") == []


def test_stats_counts(populated):
    overview = db.stats(populated)
    assert overview["total"] == 3
    assert overview["tagged"] == 3
    assert {e["person"] for e in overview["by_person"]} == {"Alex", "Blair"}


# ── Filename safety ───────────────────────────────────────────────────────


def test_sanitize_strips_path_and_reserved_characters():
    assert watermark.sanitize_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


def test_sanitize_collapses_whitespace():
    assert watermark.sanitize_filename("  Alex   Smith  ") == "Alex_Smith"


def test_sanitize_never_returns_empty():
    assert watermark.sanitize_filename("") == "item"
    assert watermark.sanitize_filename("...") == "item"


def test_output_name_sorts_in_queue_order():
    names = [watermark.output_name(i, "Alex", "IMG_1.HEIC", "image") for i in (1, 2, 10)]
    assert names == sorted(names)
    assert names[0] == "001_Alex_IMG_1.jpg"


def test_output_name_uses_mp4_for_video():
    assert watermark.output_name(3, "Alex", "clip.MOV", "video") == "003_Alex_clip.mp4"


def test_drawtext_escaping_protects_ffmpeg_syntax():
    escaped = watermark.escape_drawtext("O'Brien: 50%, [x]")
    for char in ("\\'", "\\:", "\\%", "\\[", "\\]", "\\,"):
        assert char in escaped
