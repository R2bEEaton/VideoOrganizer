"""Export media to a uniform frame with an attribution watermark.

Everything lands at one resolution with a blurred fill behind it, so a mix of
portrait phone video, landscape photos, and square crops cuts together without
letterbox jitter. The contributor's name is burned into the corner.
"""

from __future__ import annotations

import platform
import re
import subprocess
from pathlib import Path

#: Searched in order for a real TrueType face; PIL's bitmap default is the
#: last resort and looks it.
FONT_CANDIDATES = {
    "Windows": [
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
    ],
    "Darwin": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ],
    "Linux": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ],
}

BOLD_FONT_CANDIDATES = {
    "Windows": ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "Darwin": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf"],
    "Linux": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
}

MARGIN = 34
BOX_PADDING = 18


def find_font_file(bold: bool = False) -> Path | None:
    """First existing system font, preferring the platform's own faces."""
    table = BOLD_FONT_CANDIDATES if bold else FONT_CANDIDATES
    system = platform.system()
    candidates = table.get(system, []) + [c for k, v in table.items() if k != system for c in v]
    if bold:
        candidates += FONT_CANDIDATES.get(system, [])
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def load_font(size: int, bold: bool = False):
    from PIL import ImageFont

    path = find_font_file(bold=bold)
    if path:
        try:
            return ImageFont.truetype(str(path), size)
        except Exception:
            pass
    return ImageFont.load_default()


def _load_rgba(src: Path):
    from .thumbs import load_image

    return load_image(src).convert("RGBA")


def _blurred_background(image, config):
    from PIL import Image, ImageFilter, ImageOps

    background = ImageOps.fit(
        image,
        (config.width, config.height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )
    background = background.filter(ImageFilter.GaussianBlur(radius=config.blur_sigma))
    dimmer = Image.new("RGBA", background.size, (0, 0, 0, 58))
    return Image.alpha_composite(background, dimmer)


def _fitted_foreground(image, config):
    from PIL import Image

    foreground = image.copy()
    scale = min(config.width / foreground.width, config.height / foreground.height, 1.0)
    size = (
        max(1, round(foreground.width * scale)),
        max(1, round(foreground.height * scale)),
    )
    if size != foreground.size:
        foreground = foreground.resize(size, Image.Resampling.LANCZOS)
    return foreground


def export_image(src: Path, dest: Path, owner_name: str, config) -> Path:
    """Render a still onto the export canvas with a watermark."""
    from PIL import Image, ImageDraw

    image = _load_rgba(Path(src))
    canvas = _blurred_background(image, config)
    foreground = _fitted_foreground(image, config)
    canvas.alpha_composite(
        foreground,
        ((config.width - foreground.width) // 2, (config.height - foreground.height) // 2),
    )

    if config.watermark and owner_name:
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font = load_font(max(24, int(config.height * 0.04)))
        left, top, right, bottom = draw.textbbox((0, 0), owner_name, font=font)
        text_w, text_h = right - left, bottom - top

        box = (
            config.width - text_w - BOX_PADDING * 2 - MARGIN,
            config.height - text_h - BOX_PADDING * 2 - MARGIN,
            config.width - MARGIN,
            config.height - MARGIN,
        )
        draw.rectangle(box, fill=(0, 0, 0, 115))
        draw.text(
            (box[0] + BOX_PADDING - left, box[1] + BOX_PADDING - top),
            owner_name,
            font=font,
            fill=(255, 255, 255, 235),
        )
        canvas = Image.alpha_composite(canvas, overlay)

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(dest, "JPEG", quality=config.jpeg_quality, optimize=True)
    return dest


def escape_drawtext(value: str) -> str:
    """Escape text for ffmpeg's drawtext filter, which has its own quoting."""
    escaped = (value or "").replace("\\", "\\\\")
    for char in (":", "'", ",", "[", "]", "%", ";"):
        escaped = escaped.replace(char, f"\\{char}")
    return escaped


def build_drawtext(owner_name: str) -> str:
    parts = []
    font_path = find_font_file()
    if font_path:
        parts.append("fontfile='" + font_path.as_posix().replace(":", "\\:") + "'")
    parts += [
        f"text='{escape_drawtext(owner_name)}'",
        "fontcolor=white",
        "fontsize=h*0.04",
        "box=1",
        "boxcolor=black@0.45",
        f"boxborderw={BOX_PADDING}",
        f"x=w-tw-{MARGIN}",
        f"y=h-th-{MARGIN}",
    ]
    return "drawtext=" + ":".join(parts)


def build_video_filter(owner_name: str, config) -> str:
    """Blurred-fill + centered source + optional watermark, as one filtergraph."""
    width, height = config.width, config.height
    graph = (
        f"[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma={config.blur_sigma}[bg];"
        f"[fgsrc]scale=w='min({width}\\,iw)':h='min({height}\\,ih)':"
        f"force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    if config.watermark and owner_name:
        graph += f",{build_drawtext(owner_name)}"
    return graph + "[v]"


def export_video(src: Path, dest: Path, owner_name: str, config) -> Path:
    """Re-encode a clip onto the export canvas with a watermark."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-filter_complex", build_video_filter(owner_name, config),
        "-map", "[v]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", config.video_preset, "-crf", str(config.video_crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", config.audio_bitrate,
        "-movflags", "+faststart",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffmpeg export failed")
    return dest


def sanitize_filename(value: str) -> str:
    """Reduce arbitrary text to something safe on every filesystem."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", (value or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._ ")
    return cleaned or "item"


def output_name(index: int, owner_name: str, filename: str, kind: str) -> str:
    """Numbered output filename that preserves queue order when sorted."""
    ext = ".mp4" if kind == "video" else ".jpg"
    return f"{index:03d}_{sanitize_filename(owner_name)}_{sanitize_filename(Path(filename).stem)}{ext}"


def export_item(src: Path, dest: Path, owner_name: str, kind: str, config) -> str:
    """Export one item, dispatching on media kind. Returns the kind rendered."""
    if kind == "video":
        export_video(src, dest, owner_name, config)
        return "video"
    export_image(src, dest, owner_name, config)
    return "image"
