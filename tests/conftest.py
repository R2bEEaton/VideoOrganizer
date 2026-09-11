import pytest

from videoorganizer.config import EXAMPLE_CONFIG, CONFIG_NAME, load_config
from videoorganizer import db


@pytest.fixture
def library(tmp_path):
    """A real library rooted in a temp folder, built from the shipped example config."""
    (tmp_path / CONFIG_NAME).write_text(EXAMPLE_CONFIG, encoding="utf-8")
    return load_config(tmp_path / CONFIG_NAME)


@pytest.fixture
def conn(library):
    connection = db.connect(library.database)
    yield connection
    connection.close()
