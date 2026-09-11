"""VideoOrganizer — organize, tag, browse, and cut recaps from a pile of media.

Typical use is through the ``vorg`` command line, but the pieces are importable:

    from videoorganizer import load_config, db, search

    library = load_config()
    with db.open_db(library.database) as conn:
        hits = search.search(conn, "sunset on the water")
"""

from .config import ConfigError, Library, find_config, load_config

__version__ = "0.1.0"

__all__ = ["ConfigError", "Library", "find_config", "load_config", "__version__"]
