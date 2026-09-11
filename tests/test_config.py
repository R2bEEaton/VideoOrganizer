from pathlib import Path

import pytest

from videoorganizer import config


def test_example_config_loads(library):
    assert library.database.name == "media.db"
    assert library.thumbs_dir.name == ".thumbs"
    assert ".mov" in library.video_exts
    assert ".heic" in library.image_exts


def test_paths_resolve_relative_to_the_config_file(tmp_path):
    (tmp_path / config.CONFIG_NAME).write_text(config.EXAMPLE_CONFIG, encoding="utf-8")
    library = config.load_config(tmp_path / config.CONFIG_NAME)
    assert library.root == tmp_path.resolve()
    assert library.database.parent == tmp_path.resolve()


def test_missing_config_raises():
    with pytest.raises(config.ConfigError):
        config.load_config(Path("/nonexistent/videoorganizer.toml"))


def test_defaults_apply_to_an_empty_config(tmp_path):
    (tmp_path / config.CONFIG_NAME).write_text("[library]\n", encoding="utf-8")
    library = config.load_config(tmp_path / config.CONFIG_NAME)
    assert library.tagging.model == "llama3.2-vision:latest"
    assert library.export.width == 1920
    assert library.people == {}
    assert not library.drive.enabled


def test_extensions_are_normalized(tmp_path):
    (tmp_path / config.CONFIG_NAME).write_text(
        '[library]\nvideo_extensions = ["MOV", ".Mp4"]\n', encoding="utf-8"
    )
    library = config.load_config(tmp_path / config.CONFIG_NAME)
    assert library.video_exts == {".mov", ".mp4"}


def test_file_type_dispatch(library):
    assert library.file_type("a.MOV") == "video"
    assert library.file_type("a.HEIC") == "image"
    assert library.file_type("notes.txt") is None


def test_friendly_name_maps_and_passes_through(tmp_path):
    (tmp_path / config.CONFIG_NAME).write_text(
        '[library]\n[people]\n"somelogin123" = "Alex"\n', encoding="utf-8"
    )
    library = config.load_config(tmp_path / config.CONFIG_NAME)
    assert library.friendly_name("somelogin123") == "Alex"
    assert library.friendly_name("Unlisted Person") == "Unlisted Person"
    assert library.friendly_name(None) == "Unknown"


def test_thumbnails_are_keyed_by_id_not_filename(library):
    """Two files can share a stem; only the database id is unique."""
    assert library.thumb_path(42).name == "42.jpg"


def test_resolve_refuses_paths_outside_the_library(library):
    with pytest.raises(ValueError):
        library.resolve("../../etc/passwd")


def test_resolve_accepts_windows_style_separators(library):
    (library.root / "organized" / "A").mkdir(parents=True, exist_ok=True)
    resolved = library.resolve(r"organized\A\file.jpg")
    assert resolved.name == "file.jpg"


def test_recap_sections_and_chapters_load(library):
    assert [s.name for s in library.recap.sections]
    assert [c.id for c in library.recap.chapters] == ["arrival", "main", "finale"]
    assert library.recap.default_chapter == "main"
