"""Configuration loading.

Everything site-specific lives in a ``videoorganizer.toml`` at the root of a
library folder. Nothing in this package hardcodes a path, a person, a Drive
folder, or a trip.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

CONFIG_NAME = "videoorganizer.toml"

DEFAULT_VIDEO_EXTS = [".mov", ".mp4", ".avi", ".mkv", ".m4v", ".mts"]
DEFAULT_IMAGE_EXTS = [".jpg", ".jpeg", ".heic", ".heif", ".png", ".dng", ".gif", ".webp"]


class ConfigError(Exception):
    """Raised when a config file is missing or malformed."""


@dataclass
class TaggingConfig:
    """How media gets described by a vision model."""

    backend: str = "ollama"
    url: str = "http://localhost:11434"
    model: str = "llama3.2-vision:latest"
    timeout: int = 180
    temperature: float = 0.2
    num_predict: int = 300
    #: Describes what the library is *of*. Injected into the prompt so the model
    #: has context, e.g. "a tropical vacation" or "a backyard woodworking shop".
    subject: str = "a personal photo and video collection"
    min_tags: int = 5
    max_tags: int = 15


@dataclass
class ExportConfig:
    """Watermarked export render settings."""

    width: int = 1920
    height: int = 1080
    blur_sigma: int = 28
    jpeg_quality: int = 92
    video_crf: int = 18
    video_preset: str = "medium"
    audio_bitrate: str = "192k"
    watermark: bool = True


@dataclass
class RecapSection:
    """A keyword bucket used to classify an item by what it shows."""

    name: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class RecapChapter:
    """A titled run of the finished recap video.

    ``dates`` optionally pins the chapter to a date range (inclusive ISO
    ``YYYY-MM-DD`` strings). ``sections`` optionally pins it to section names.
    ``budget`` is the target number of seconds of screen time.
    """

    id: str
    title: str
    subtitle: str = ""
    budget: float = 18.0
    dates: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)


@dataclass
class RecapConfig:
    title: str = "Recap"
    subtitle: str = ""
    frame_rate: int = 30
    audio_rate: int = 48000
    title_card_duration: float = 2.0
    default_chapter: str = ""
    sections: list[RecapSection] = field(default_factory=list)
    chapters: list[RecapChapter] = field(default_factory=list)


@dataclass
class DriveConfig:
    folder_id: str = ""
    credentials: str = "client_secret.json"
    token: str = "token.json"
    metadata_cache: str = "drive_metadata.json"

    @property
    def enabled(self) -> bool:
        return bool(self.folder_id)


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 5500
    title: str = "Media Library"
    kicker: str = ""


@dataclass
class Library:
    """A media library rooted at a single folder."""

    root: Path
    database: Path
    thumbs_dir: Path
    organized_dir: Path
    exports_dir: Path
    video_exts: set[str]
    image_exts: set[str]
    people: dict[str, str]
    tagging: TaggingConfig
    export: ExportConfig
    recap: RecapConfig
    drive: DriveConfig
    web: WebConfig

    @property
    def media_exts(self) -> set[str]:
        return self.video_exts | self.image_exts

    def file_type(self, path: Path | str) -> str | None:
        """Return ``'video'``, ``'image'``, or ``None`` for a non-media file."""
        ext = Path(path).suffix.lower()
        if ext in self.video_exts:
            return "video"
        if ext in self.image_exts:
            return "image"
        return None

    def friendly_name(self, raw: str | None) -> str:
        """Map an uploader/account name to the display name from ``[people]``."""
        if not raw:
            return "Unknown"
        return self.people.get(raw, raw)

    def resolve(self, rel_path: str | Path) -> Path:
        """Resolve a library-relative path, refusing anything outside the root."""
        path = (self.root / str(rel_path).replace("\\", "/")).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"Path escapes library root: {rel_path}")
        return path

    def relative(self, path: Path) -> str:
        """Render an absolute path as the library-relative form stored in the DB."""
        return str(Path(path).resolve().relative_to(self.root.resolve()))

    def thumb_path(self, media_id: int) -> Path:
        """Thumbnails are keyed by database id, never by filename stem.

        Two people's phones routinely produce the same ``IMG_4335`` stem, and a
        Live Photo is a ``.HEIC`` and a ``.MOV`` sharing one stem. Only the id is
        unique.
        """
        return self.thumbs_dir / f"{media_id}.jpg"


def find_config(start: Path | None = None) -> Path | None:
    """Search ``start`` and its parents for a config file."""
    env = os.environ.get("VIDEOORGANIZER_CONFIG")
    if env:
        candidate = Path(env).expanduser()
        return candidate if candidate.exists() else None

    current = (start or Path.cwd()).resolve()
    for folder in [current, *current.parents]:
        candidate = folder / CONFIG_NAME
        if candidate.exists():
            return candidate
    return None


def _sub(data: dict, key: str) -> dict:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _exts(values, fallback: list[str]) -> set[str]:
    items = values if values else fallback
    return {e if e.startswith(".") else f".{e}" for e in (s.lower() for s in items)}


def load_config(path: Path | None = None, start: Path | None = None) -> Library:
    """Load a :class:`Library` from a config file.

    Raises :class:`ConfigError` if no config can be found.
    """
    config_path = Path(path) if path else find_config(start)
    if not config_path or not config_path.exists():
        raise ConfigError(
            f"No {CONFIG_NAME} found. Run `vorg init` in your media folder to create one."
        )

    with open(config_path, "rb") as handle:
        data = tomllib.load(handle)

    return build_library(data, config_path.parent)


def build_library(data: dict, base: Path) -> Library:
    """Build a :class:`Library` from parsed TOML and the folder holding it."""
    lib = _sub(data, "library")
    root = Path(lib.get("root", ".")).expanduser()
    if not root.is_absolute():
        root = (base / root).resolve()

    def under_root(key: str, default: str) -> Path:
        value = Path(lib.get(key, default)).expanduser()
        return value if value.is_absolute() else (root / value)

    tagging_raw = _sub(data, "tagging")
    export_raw = _sub(data, "export")
    recap_raw = _sub(data, "recap")
    drive_raw = _sub(data, "drive")
    web_raw = _sub(data, "web")

    sections = [
        RecapSection(name=s["name"], keywords=[k.lower() for k in s.get("keywords", [])])
        for s in recap_raw.get("sections", [])
        if s.get("name")
    ]
    chapters = [
        RecapChapter(
            id=c["id"],
            title=c.get("title", c["id"]),
            subtitle=c.get("subtitle", ""),
            budget=float(c.get("budget", 18.0)),
            dates=list(c.get("dates", [])),
            sections=list(c.get("sections", [])),
        )
        for c in recap_raw.get("chapters", [])
        if c.get("id")
    ]

    people = {str(k): str(v) for k, v in _sub(data, "people").items()}

    return Library(
        root=root,
        database=under_root("database", "media.db"),
        thumbs_dir=under_root("thumbs", ".thumbs"),
        organized_dir=under_root("organized", "organized"),
        exports_dir=under_root("exports", "exports"),
        video_exts=_exts(lib.get("video_extensions"), DEFAULT_VIDEO_EXTS),
        image_exts=_exts(lib.get("image_extensions"), DEFAULT_IMAGE_EXTS),
        people=people,
        tagging=TaggingConfig(
            **{k: v for k, v in tagging_raw.items() if k in TaggingConfig.__dataclass_fields__}
        ),
        export=ExportConfig(
            **{k: v for k, v in export_raw.items() if k in ExportConfig.__dataclass_fields__}
        ),
        recap=RecapConfig(
            title=recap_raw.get("title", "Recap"),
            subtitle=recap_raw.get("subtitle", ""),
            frame_rate=int(recap_raw.get("frame_rate", 30)),
            audio_rate=int(recap_raw.get("audio_rate", 48000)),
            title_card_duration=float(recap_raw.get("title_card_duration", 2.0)),
            default_chapter=recap_raw.get("default_chapter", ""),
            sections=sections,
            chapters=chapters,
        ),
        drive=DriveConfig(
            **{k: v for k, v in drive_raw.items() if k in DriveConfig.__dataclass_fields__}
        ),
        web=WebConfig(**{k: v for k, v in web_raw.items() if k in WebConfig.__dataclass_fields__}),
    )


EXAMPLE_CONFIG = '''# VideoOrganizer library configuration.
# Every path is relative to this file unless it is absolute.

[library]
root = "."
database = "media.db"
thumbs = ".thumbs"
organized = "organized"
exports = "exports"
# Override the recognized extensions if your cameras produce something unusual.
# video_extensions = [".mov", ".mp4"]
# image_extensions = [".jpg", ".heic"]

# Map the account/display name a file arrives with to the name you want shown.
# Anyone not listed here keeps their original name.
[people]
# "somelogin123" = "Alex"
# "Jordan Rivera" = "Jordan"

[tagging]
backend = "ollama"
url = "http://localhost:11434"
model = "llama3.2-vision:latest"
# Tell the vision model what it is looking at. This single line does more for
# tag quality than any other setting.
subject = "a personal photo and video collection"

[export]
width = 1920
height = 1080
watermark = true

[web]
host = "127.0.0.1"
port = 5500
title = "Media Library"
kicker = ""

# Optional: sync from a shared Google Drive folder.
# Requires `pip install "videoorganizer[drive]"`.
[drive]
folder_id = ""
credentials = "client_secret.json"
token = "token.json"

# Optional: automatic recap video assembly.
[recap]
title = "Our Trip"
subtitle = "Recap"
# Items matching no chapter land here. Must name one of the chapter ids below.
# This key has to stay up here in [recap]: anything written after the first
# [[recap.chapters]] block belongs to that chapter, not to [recap].
default_chapter = "main"

# Sections classify each item by what the tags say it shows. First match wins.
[[recap.sections]]
name = "travel"
keywords = ["airplane", "airport", "runway", "road", "car", "luggage"]

[[recap.sections]]
name = "water"
keywords = ["beach", "ocean", "snorkeling", "boat", "swimming", "pool"]

[[recap.sections]]
name = "food"
keywords = ["dinner", "restaurant", "meal", "cooking", "table"]

[[recap.sections]]
name = "people"
keywords = ["group photo", "portrait", "selfie", "smiling", "friends"]

# Chapters are the titled runs of the finished video, in order.
# Pin one to a date range with `dates`, or to sections with `sections`.
# `budget` is roughly how many seconds of screen time it gets.
[[recap.chapters]]
id = "arrival"
title = "Getting There"
subtitle = "The journey out"
budget = 25.0
sections = ["travel"]

[[recap.chapters]]
id = "main"
title = "The Trip"
subtitle = ""
budget = 90.0

[[recap.chapters]]
id = "finale"
title = "Last Day"
subtitle = "Heading home"
budget = 20.0
'''
