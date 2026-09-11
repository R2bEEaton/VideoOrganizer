"""SQLite schema, migrations, and the FTS5 index."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id           INTEGER PRIMARY KEY,
    filename     TEXT NOT NULL,
    rel_path     TEXT NOT NULL UNIQUE,
    person       TEXT,
    captured_at  TEXT,
    file_type    TEXT,
    file_size    INTEGER,
    duration_s   REAL,
    tags         TEXT,
    description  TEXT,
    tagged       INTEGER DEFAULT 0,
    source       TEXT,
    source_id    TEXT,
    source_owner TEXT,
    source_created TEXT,
    imported_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_media_person ON media(person);
CREATE INDEX IF NOT EXISTS idx_media_captured ON media(captured_at);
CREATE INDEX IF NOT EXISTS idx_media_filename ON media(filename);

CREATE VIRTUAL TABLE IF NOT EXISTS media_fts
    USING fts5(id UNINDEXED, description, tags, content='media', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS media_ai AFTER INSERT ON media BEGIN
    INSERT INTO media_fts(rowid, id, description, tags)
    VALUES (new.id, new.id, new.description, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS media_ad AFTER DELETE ON media BEGIN
    INSERT INTO media_fts(media_fts, rowid, id, description, tags)
    VALUES ('delete', old.id, old.id, old.description, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS media_au AFTER UPDATE OF description, tags ON media BEGIN
    INSERT INTO media_fts(media_fts, rowid, id, description, tags)
    VALUES ('delete', old.id, old.id, old.description, old.tags);
    INSERT INTO media_fts(rowid, id, description, tags)
    VALUES (new.id, new.id, new.description, new.tags);
END;
"""

#: Columns added after the first release. Applied additively so an existing
#: database is upgraded in place without losing data.
MIGRATIONS: list[tuple[str, str]] = [
    ("source", "TEXT"),
    ("source_id", "TEXT"),
    ("source_owner", "TEXT"),
    ("source_created", "TEXT"),
    ("imported_at", "TEXT"),
]


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) a library database with the schema applied."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    return conn


@contextmanager
def open_db(db_path: Path):
    """Context-managed :func:`connect`."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(media)")}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add any missing columns. Returns the names of columns added."""
    existing = columns(conn)
    added = []
    for name, sql_type in MIGRATIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE media ADD COLUMN {name} {sql_type}")
            added.append(name)
    if added:
        conn.commit()
    return added


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the full-text index from the ``media`` table."""
    conn.execute("INSERT INTO media_fts(media_fts) VALUES('rebuild')")
    conn.commit()


def parse_tags(value) -> list[str]:
    """Decode the JSON tag array, tolerating nulls and malformed rows."""
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        return []
    return decoded if isinstance(decoded, list) else []


def insert_media(conn: sqlite3.Connection, **fields) -> int | None:
    """Insert one media row, ignoring duplicates on ``rel_path``.

    Returns the row id, or ``None`` if the path was already present.
    """
    keys = [k for k in fields if fields[k] is not None or k in {"captured_at"}]
    placeholders = ",".join("?" for _ in keys)
    conn.execute(
        f"INSERT OR IGNORE INTO media ({','.join(keys)}) VALUES ({placeholders})",
        [fields[k] for k in keys],
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM media WHERE rel_path = ?", (fields["rel_path"],)
    ).fetchone()
    return row[0] if row else None


def set_tags(conn: sqlite3.Connection, media_id: int, tags: list[str], description: str) -> None:
    conn.execute(
        "UPDATE media SET tags = ?, description = ?, tagged = 1 WHERE id = ?",
        (json.dumps(tags), description, media_id),
    )
    conn.commit()


def stats(conn: sqlite3.Connection) -> dict:
    """Library overview used by both the CLI and the web UI."""
    total = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
    tagged = conn.execute(
        "SELECT COUNT(*) FROM media WHERE description IS NOT NULL AND description != ''"
    ).fetchone()[0]
    by_person = [
        {"person": row[0] or "Unknown", "count": row[1]}
        for row in conn.execute(
            "SELECT person, COUNT(*) FROM media GROUP BY person ORDER BY 2 DESC"
        )
    ]
    by_date = [
        {"date": row[0], "count": row[1]}
        for row in conn.execute(
            "SELECT substr(captured_at, 1, 10) AS d, COUNT(*) FROM media "
            "WHERE captured_at IS NOT NULL GROUP BY d ORDER BY d"
        )
    ]
    by_type = [
        {"type": row[0] or "unknown", "count": row[1]}
        for row in conn.execute("SELECT file_type, COUNT(*) FROM media GROUP BY file_type")
    ]
    undated = conn.execute("SELECT COUNT(*) FROM media WHERE captured_at IS NULL").fetchone()[0]
    return {
        "total": total,
        "tagged": tagged,
        "untagged": total - tagged,
        "undated": undated,
        "by_person": by_person,
        "by_date": by_date,
        "by_type": by_type,
    }
