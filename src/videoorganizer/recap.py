"""Assemble a recap video from an exported folder.

The pipeline: classify each exported item into a section by its tags, assign it
to a chapter, spend each chapter's time budget on a representative spread of
items, render every pick to a uniform segment, and concatenate.

Sections and chapters come entirely from config — nothing here knows what your
trip was.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import export as export_mod
from . import media as media_mod
from . import watermark

OTHER_SECTION = "other"
ANALYSIS_NAME = "recap_analysis.json"
SUMMARY_NAME = "recap_video_summary.json"
OUTPUT_NAME = "recap_video.mp4"

#: A clip longer than this gets trimmed — a recap is a highlight reel.
MAX_CLIP_SECONDS = 3.5
#: Back-to-back clips from the same person get trimmed harder still.
REPEAT_CLIP_SECONDS = 3.0
#: How long a still holds, when it is distinct from the one before it.
STILL_SECONDS = 1.75
#: ...and when it is part of a burst or a visually similar run.
SIMILAR_STILL_SECONDS = 0.95
#: Shared-tag count above which two stills are considered a similar run.
SIMILAR_TAG_THRESHOLD = 2


@dataclass
class RecapItem:
    order: int
    id: int
    filename: str
    path: Path
    owner_name: str
    export_kind: str
    captured_at: str = ""
    person: str = ""
    tags: list[str] = field(default_factory=list)
    description: str = ""
    section: str = OTHER_SECTION
    chapter: str = ""
    duration_s: float | None = None


def classify_section(tags, description, sections) -> str:
    """First section whose keywords appear in the item's text. Order matters."""
    blob = " ".join([description or "", " ".join(tags or [])]).lower()
    for section in sections:
        if any(keyword in blob for keyword in section.keywords):
            return section.name
    return OTHER_SECTION


def chapter_for(captured_at: str, section: str, config) -> str:
    """Assign an item to a chapter.

    A chapter's ``dates`` range wins over its ``sections`` list, because "the day
    we went to the coast" is a stronger signal than "this photo has a boat in
    it". Anything unmatched falls to ``default_chapter``.
    """
    chapters = config.chapters
    if not chapters:
        return ""

    date = (captured_at or "")[:10]

    if date:
        for chapter in chapters:
            if not chapter.dates:
                continue
            start = chapter.dates[0]
            end = chapter.dates[-1] if len(chapter.dates) > 1 else start
            if start <= date <= end:
                return chapter.id

    if section and section != OTHER_SECTION:
        for chapter in chapters:
            if section in chapter.sections:
                return chapter.id

    if config.default_chapter:
        for chapter in chapters:
            if chapter.id == config.default_chapter:
                return chapter.id

    undated = [c for c in chapters if not c.dates and not c.sections]
    return undated[0].id if undated else chapters[-1].id


def build_items(library, conn, manifest: dict) -> list[RecapItem]:
    """Turn manifest entries into classified recap items."""
    written = manifest.get("written") or []
    ids = [entry["id"] for entry in written if entry.get("id") is not None]

    rows = {}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        for row in conn.execute(
            f"SELECT id, person, captured_at, file_type, tags, description, filename "
            f"FROM media WHERE id IN ({placeholders})",
            ids,
        ):
            rows[row["id"]] = dict(row)

    from .db import parse_tags

    items = []
    for order, entry in enumerate(written, start=1):
        row = rows.get(entry.get("id"), {})
        tags = parse_tags(row.get("tags"))
        description = row.get("description") or ""
        section = classify_section(tags, description, library.recap.sections)
        path = Path(entry["path"])
        items.append(
            RecapItem(
                order=order,
                id=entry.get("id"),
                filename=entry["filename"],
                path=path,
                owner_name=entry.get("owner_name", ""),
                export_kind=entry.get("export_kind", "image"),
                captured_at=row.get("captured_at") or "",
                person=row.get("person") or entry.get("owner_name", ""),
                tags=tags,
                description=description,
                section=section,
                chapter=chapter_for(row.get("captured_at") or "", section, library.recap),
                duration_s=(
                    media_mod.ffprobe_duration(path)
                    if entry.get("export_kind") == "video"
                    else None
                ),
            )
        )
    return items


def write_analysis(export_dir: Path, items: list[RecapItem]) -> Path:
    """Record the classification so a rerun is reproducible and reviewable."""
    path = Path(export_dir) / ANALYSIS_NAME
    path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "order": item.order,
                        "id": item.id,
                        "filename": item.filename,
                        "captured_at": item.captured_at,
                        "chapter": item.chapter,
                        "section": item.section,
                        "tags": item.tags,
                        "description": item.description,
                    }
                    for item in items
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def evenly_spaced(items: list, count: int) -> list:
    """Sample ``count`` items spread across the list, preserving order."""
    if count <= 0:
        return []
    if count >= len(items):
        return list(items)
    step = len(items) / count
    chosen, seen = [], set()
    for index in range(count):
        item = items[min(len(items) - 1, round(index * step))]
        if item.order in seen:
            continue
        chosen.append(item)
        seen.add(item.order)
    return chosen


def item_duration(item: RecapItem, previous: RecapItem | None = None) -> float:
    """How long an item holds on screen.

    Near-duplicates get less time: a burst of twelve nearly identical shots
    should read as one moment, not twelve.
    """
    if item.export_kind == "video":
        duration = min(item.duration_s or MAX_CLIP_SECONDS, MAX_CLIP_SECONDS)
        if previous and previous.export_kind == "video" and previous.person == item.person:
            duration = min(duration, REPEAT_CLIP_SECONDS)
        return duration

    if previous and previous.export_kind == "image":
        shared = len(set(previous.tags or []) & set(item.tags or []))
        same_run = (
            previous.person == item.person
            and previous.chapter == item.chapter
            and previous.section == item.section
        )
        if same_run or shared >= SIMILAR_TAG_THRESHOLD or abs(item.order - previous.order) == 1:
            return SIMILAR_STILL_SECONDS
    return STILL_SECONDS


def select_items(items: list[RecapItem], config) -> list[RecapItem]:
    """Pick what makes the cut, chapter by chapter, inside each time budget.

    Every chapter keeps its first and last item as bookends plus one item per
    distinct section, then fills the remaining budget with an even sample
    weighted toward video.
    """
    selected = []
    chapters = config.chapters or []

    for chapter in chapters:
        pool = [item for item in items if item.chapter == chapter.id]
        if not pool:
            continue

        target = max(5, round(chapter.budget / 1.45))
        videos = [item for item in pool if item.export_kind == "video"]

        anchors = [pool[0]]
        if pool[-1].order != pool[0].order:
            anchors.append(pool[-1])
        seen_sections = set()
        for item in pool:
            if item.section != OTHER_SECTION and item.section not in seen_sections:
                anchors.append(item)
                seen_sections.add(item.section)

        video_count = min(len(videos), max(1, round(target * 0.32))) if videos else 0
        picks = evenly_spaced(videos, video_count)
        picks += evenly_spaced(pool, max(0, target - len(picks)))

        merged = {item.order: item for item in anchors + picks}
        ordered = [merged[key] for key in sorted(merged)]

        running, previous, trimmed = 0.0, None, []
        for item in ordered:
            duration = item_duration(item, previous)
            if trimmed and running + duration > chapter.budget + 2.0:
                continue
            trimmed.append(item)
            running += duration
            previous = item
        selected.extend(trimmed)

    seen, result = set(), []
    for item in sorted(selected, key=lambda i: i.order):
        if item.order not in seen:
            result.append(item)
            seen.add(item.order)
    return result


def make_title_card(path: Path, title: str, subtitle: str, config, export_config) -> Path:
    """Render a title card still."""
    from PIL import Image, ImageDraw

    width, height = export_config.width, export_config.height
    canvas = Image.new("RGB", (width, height), (10, 16, 34))
    draw = ImageDraw.Draw(canvas)

    title_font = watermark.load_font(int(height * 0.07), bold=True)
    body_font = watermark.load_font(int(height * 0.031), bold=True)

    draw.rectangle(
        (width * 0.094, height * 0.218, width * 0.906, height * 0.782),
        outline=(60, 80, 130),
        width=2,
    )
    draw.rectangle(
        (width * 0.125, height * 0.278, width * 0.24, height * 0.289), fill=(255, 151, 102)
    )

    title_box = draw.textbbox((0, 0), title, font=title_font)
    draw.text(
        ((width - (title_box[2] - title_box[0])) / 2 - title_box[0], height * 0.37 - title_box[1]),
        title,
        font=title_font,
        fill=(237, 241, 252),
    )

    if subtitle:
        sub_box = draw.textbbox((0, 0), subtitle, font=body_font)
        draw.text(
            ((width - (sub_box[2] - sub_box[0])) / 2 - sub_box[0], height * 0.49 - sub_box[1]),
            subtitle,
            font=body_font,
            fill=(148, 163, 200),
        )

    canvas.save(path, "JPEG", quality=94)
    return path


def run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffmpeg command failed")


def _encode_args(config, export_config) -> list[str]:
    return [
        "-c:v", "libx264",
        "-preset", export_config.video_preset,
        "-crf", str(export_config.video_crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-ar", str(config.audio_rate),
        "-ac", "2",
        "-movflags", "+faststart",
    ]


def render_still_segment(src: Path, dest: Path, duration: float, config, export_config) -> Path:
    """A still becomes a clip of silence — every segment needs an audio track or
    concat drops out of sync."""
    run_ffmpeg(
        [
            "ffmpeg", "-y",
            "-loop", "1", "-t", f"{duration:.3f}", "-i", str(src),
            "-f", "lavfi", "-t", f"{duration:.3f}",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate={config.audio_rate}",
            "-shortest", "-r", str(config.frame_rate),
            *_encode_args(config, export_config),
            str(dest),
        ]
    )
    return dest


def render_clip_segment(src: Path, dest: Path, duration: float, config, export_config) -> Path:
    run_ffmpeg(
        [
            "ffmpeg", "-y",
            "-t", f"{duration:.3f}", "-i", str(src),
            "-r", str(config.frame_rate),
            *_encode_args(config, export_config),
            str(dest),
        ]
    )
    return dest


def build_timeline(library, items: list[RecapItem], work_dir: Path):
    """Render every segment, inserting a title card at each chapter boundary."""
    config = library.recap
    segments = []

    opening = work_dir / "title_000.jpg"
    make_title_card(opening, config.title, config.subtitle, config, library.export)
    segments.append(
        render_still_segment(
            opening,
            work_dir / "title_000.mp4",
            config.title_card_duration,
            config,
            library.export,
        )
    )

    titles = {c.id: (c.title, c.subtitle) for c in config.chapters}
    selected = select_items(items, config)

    current_chapter, previous = None, None
    for item in selected:
        if item.chapter != current_chapter:
            current_chapter = item.chapter
            if current_chapter in titles:
                title, subtitle = titles[current_chapter]
                card = work_dir / f"title_{item.order:03d}.jpg"
                make_title_card(card, title, subtitle, config, library.export)
                segments.append(
                    render_still_segment(
                        card,
                        work_dir / f"title_{item.order:03d}.mp4",
                        config.title_card_duration,
                        config,
                        library.export,
                    )
                )

        destination = work_dir / f"segment_{item.order:03d}.mp4"
        duration = item_duration(item, previous)
        if item.export_kind == "video":
            render_clip_segment(item.path, destination, duration, config, library.export)
        else:
            render_still_segment(item.path, destination, duration, config, library.export)
        segments.append(destination)
        previous = item

    return segments, selected


def concat_segments(segments: list[Path], output: Path, work_dir: Path, config, export_config) -> Path:
    listing = work_dir / "segments.txt"
    listing.write_text(
        "\n".join(f"file '{segment.as_posix()}'" for segment in segments), encoding="utf-8"
    )
    run_ffmpeg(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
            *_encode_args(config, export_config),
            str(output),
        ]
    )
    return output


def build(library, conn, export_dir: Path, keep_work: bool = False) -> dict:
    """Build the recap video for an already-exported folder."""
    export_dir = Path(export_dir).resolve()
    manifest = export_mod.load_manifest(export_dir)

    if not library.recap.chapters:
        raise ValueError(
            "No [[recap.chapters]] defined in your config. "
            "Add at least one chapter before building a recap."
        )

    work_dir = export_dir / "_recap_build"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    try:
        items = build_items(library, conn, manifest)
        write_analysis(export_dir, items)
        segments, selected = build_timeline(library, items, work_dir)
        output = concat_segments(
            segments, export_dir / OUTPUT_NAME, work_dir, library.recap, library.export
        )

        summary = {
            "output": str(output),
            "segment_count": len(segments),
            "asset_count": len(items),
            "selected_count": len(selected),
            "selected": [item.filename for item in selected],
            "chapters": [
                {
                    "id": chapter.id,
                    "title": chapter.title,
                    "subtitle": chapter.subtitle,
                    "budget": chapter.budget,
                    "selected": sum(1 for i in selected if i.chapter == chapter.id),
                }
                for chapter in library.recap.chapters
            ],
        }
        (export_dir / SUMMARY_NAME).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary
    finally:
        if not keep_work and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
