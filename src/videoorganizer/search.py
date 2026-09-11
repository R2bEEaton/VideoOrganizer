"""Search over descriptions and tags.

FTS5 with prefix matching is the fast path; a LIKE scan is the fallback for
queries FTS rejects (punctuation, bare operators) or that simply miss.
"""

from __future__ import annotations

import re

from .db import parse_tags

SELECT_COLUMNS = (
    "id, filename, rel_path, person, captured_at, file_type, file_size, "
    "tags, description, tagged, source, source_owner"
)

#: Characters FTS5 treats as syntax. Stripped so a user's plain-English query
#: never turns into a malformed MATCH expression.
_FTS_UNSAFE = re.compile(r'[^\w\s\'-]', re.UNICODE)


def split_terms(query: str) -> list[str]:
    """Split user text into search terms, dropping FTS5 syntax characters."""
    return [t for t in _FTS_UNSAFE.sub(" ", query or "").split() if t]


def build_fts_query(query: str) -> str:
    """Turn user text into a safe FTS5 prefix expression.

    Terms are joined with ``OR``, not ``AND``. "sunset on the water" describes a
    photo; it is not a demand that all four words appear. BM25 ranking then
    floats the items matching the most terms to the top, which is the behavior
    someone typing a phrase actually wants.
    """
    return " OR ".join(f"{term}*" for term in split_terms(query))


def _filters(person=None, date=None, file_type=None, prefix=""):
    clauses, params = [], []
    if person:
        clauses.append(f"{prefix}person = ?")
        params.append(person)
    if date:
        clauses.append(f"{prefix}captured_at LIKE ?")
        params.append(f"{date}%")
    if file_type:
        clauses.append(f"{prefix}file_type = ?")
        params.append(file_type)
    return clauses, params


def search(
    conn,
    query: str = "",
    person: str | None = None,
    date: str | None = None,
    file_type: str | None = None,
    limit: int = 60,
    offset: int = 0,
) -> list[dict]:
    """Search the library, returning plain dicts with tags already decoded."""
    rows = []
    query = (query or "").strip()

    if query:
        fts_query = build_fts_query(query)
        if fts_query:
            clauses, params = _filters(person, date, file_type, prefix="m.")
            sql = (
                f"SELECT {', '.join('m.' + c.strip() for c in SELECT_COLUMNS.split(','))}, rank "
                "FROM media m JOIN media_fts f ON f.rowid = m.id "
                "WHERE media_fts MATCH ?"
            )
            args = [fts_query, *params]
            if clauses:
                sql += " AND " + " AND ".join(clauses)
            sql += " ORDER BY rank LIMIT ? OFFSET ?"
            args += [limit, offset]
            try:
                rows = conn.execute(sql, args).fetchall()
            except Exception:
                rows = []

        if not rows:
            # Fallback for anything FTS cannot serve. Each term is matched
            # independently, for the same reason the FTS query ORs its terms.
            clauses, params = _filters(person, date, file_type)
            terms = split_terms(query) or [query]
            term_clauses, term_args = [], []
            for term in terms:
                term_clauses.append(
                    "(description LIKE ? OR tags LIKE ? OR filename LIKE ?)"
                )
                term_args += [f"%{term}%"] * 3

            sql = f"SELECT {SELECT_COLUMNS} FROM media WHERE ({' OR '.join(term_clauses)})"
            args = [*term_args, *params]
            if clauses:
                sql += " AND " + " AND ".join(clauses)
            sql += " ORDER BY captured_at LIMIT ? OFFSET ?"
            args += [limit, offset]
            rows = conn.execute(sql, args).fetchall()
    else:
        clauses, params = _filters(person, date, file_type)
        sql = f"SELECT {SELECT_COLUMNS} FROM media WHERE 1 = 1"
        args = list(params)
        if clauses:
            sql += " AND " + " AND ".join(clauses)
        sql += " ORDER BY captured_at LIMIT ? OFFSET ?"
        args += [limit, offset]
        rows = conn.execute(sql, args).fetchall()

    results = []
    for row in rows:
        item = dict(row)
        item.pop("rank", None)
        item["tags"] = parse_tags(item.get("tags"))
        item["captured_date"] = (item.get("captured_at") or "")[:10]
        results.append(item)
    return results
