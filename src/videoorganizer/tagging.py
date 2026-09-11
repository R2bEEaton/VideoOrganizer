"""Semantic tagging via a local vision model.

Sends each item's thumbnail to Ollama and stores the returned tags and
one-sentence description. Everything runs locally; no media leaves the machine.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from . import db, thumbs


class TaggingError(Exception):
    """Raised when the tagging backend is unreachable or unusable."""


def build_prompt(config, file_type: str = "image", person: str | None = None) -> str:
    """Compose the vision prompt.

    ``config.subject`` is what makes tags useful — a model told it is looking at
    "a tropical vacation" produces "snorkeling" and "catamaran" where an
    unprimed one produces "water" and "boat".
    """
    kind = "video frame" if file_type == "video" else "photo"
    context = f"This is a {kind} from {config.subject}"
    if person and person.lower() != "unknown":
        context += f", contributed by {person}"
    context += "."

    return (
        f"{context}\n\n"
        "Return ONLY a JSON object — no markdown, no explanation — with exactly two fields:\n"
        f'  "tags": array of {config.min_tags}-{config.max_tags} lowercase semantic tags. '
        "Be specific and visual: name activities, objects, settings, and times of day.\n"
        '  "description": one natural English sentence describing what is happening.\n'
        'Example: {"tags": ["beach", "sunset", "group photo"], '
        '"description": "Five people pose on the beach at sunset."}'
    )


def extract_json_object(raw: str) -> dict | None:
    """Pull the first complete JSON object out of a model's reply.

    Vision models wrap output in markdown fences, prepend "Here is the JSON:",
    and append commentary. Scanning by brace depth finds the real object where a
    greedy regex would swallow trailing garbage.
    """
    if not raw:
        return None

    text = re.sub(r"^\s*```[a-zA-Z]*\n?", "", raw.strip())
    text = re.sub(r"\n?```\s*$", "", text)

    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : index + 1])
                except ValueError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def normalize_result(parsed: dict | None) -> tuple[list[str], str] | None:
    """Coerce a parsed reply into ``(tags, description)``."""
    if not parsed:
        return None

    raw_tags = parsed.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",")]

    tags, seen = [], set()
    for tag in raw_tags:
        if not isinstance(tag, (str, int, float)):
            continue
        cleaned = str(tag).strip().lower()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            tags.append(cleaned)

    description = parsed.get("description") or ""
    if not isinstance(description, str):
        description = str(description)
    description = description.strip()

    if not tags and not description:
        return None
    return tags, description


class OllamaBackend:
    """Talks to an Ollama server's ``/api/generate`` endpoint."""

    def __init__(self, config):
        self.config = config

    def available(self) -> bool:
        try:
            request = urllib.request.Request(f"{self.config.url}/api/tags")
            with urllib.request.urlopen(request, timeout=5) as response:
                json.loads(response.read())
            return True
        except Exception:
            return False

    def describe(self, image_path: Path, prompt: str) -> tuple[list[str], str] | None:
        payload = json.dumps(
            {
                "model": self.config.model,
                "prompt": prompt,
                "images": [base64.standard_b64encode(Path(image_path).read_bytes()).decode()],
                "stream": False,
                "options": {
                    "temperature": self.config.temperature,
                    "num_predict": self.config.num_predict,
                },
            }
        ).encode()

        request = urllib.request.Request(
            f"{self.config.url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
            body = json.loads(response.read())

        return normalize_result(extract_json_object(body.get("response", "")))


def get_backend(config):
    if config.backend == "ollama":
        return OllamaBackend(config)
    raise TaggingError(f"Unknown tagging backend: {config.backend!r}")


def tag_library(
    library,
    conn,
    limit: int = 0,
    retag: bool = False,
    on_progress=None,
) -> dict:
    """Tag untagged media. Returns counts of what happened.

    A file that fails is left with ``tagged = 0`` so the next run picks it up
    again rather than burying the failure.
    """
    backend = get_backend(library.tagging)
    if not backend.available():
        raise TaggingError(
            f"Tagging backend unreachable at {library.tagging.url}. "
            "Is Ollama running? Try: ollama serve"
        )

    query = (
        "SELECT id, filename, rel_path, file_type, person FROM media"
        if retag
        else "SELECT id, filename, rel_path, file_type, person FROM media WHERE tagged = 0"
    )
    query += " ORDER BY id"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query).fetchall()

    tagged = failed = missing = 0

    for index, row in enumerate(rows, start=1):
        try:
            src = library.resolve(row["rel_path"])
        except ValueError:
            missing += 1
            continue

        if not src.exists():
            missing += 1
            if on_progress:
                on_progress(index, len(rows), row["filename"], "missing", None)
            continue

        thumb = thumbs.ensure(library, row["id"], src)
        if not thumb:
            failed += 1
            if on_progress:
                on_progress(index, len(rows), row["filename"], "no-thumbnail", None)
            continue

        prompt = build_prompt(library.tagging, row["file_type"] or "image", row["person"])
        try:
            result = backend.describe(thumb, prompt)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            failed += 1
            if on_progress:
                on_progress(index, len(rows), row["filename"], f"error: {exc}", None)
            continue

        if not result:
            failed += 1
            if on_progress:
                on_progress(index, len(rows), row["filename"], "unparseable reply", None)
            continue

        tags, description = result
        db.set_tags(conn, row["id"], tags, description)
        tagged += 1
        if on_progress:
            on_progress(index, len(rows), row["filename"], "ok", tags)

    return {"considered": len(rows), "tagged": tagged, "failed": failed, "missing": missing}
