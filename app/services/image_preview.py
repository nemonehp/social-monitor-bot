from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SOURCE_PIXELS = 40_000_000


@dataclass(frozen=True, slots=True)
class PreparedPreview:
    data: bytes
    width: int
    height: int
    mime_type: str = "image/jpeg"


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def prepare_preview(
    data: bytes,
    *,
    max_edge: int,
    max_bytes: int,
    min_edge: int = 1,
) -> PreparedPreview | None:
    """Decode, orient and encode an image as a bounded high-quality JPEG preview.

    The collector may receive JPEG, PNG, WEBP, a cached Telegram thumbnail or the
    first frame of an animated image. Normalizing it here prevents files with a
    misleading ``.jpg`` suffix and lets us download the largest available source
    thumbnail before applying our own size limits.
    """
    if not data or max_edge < 1 or max_bytes < 1 or min_edge < 1:
        return None
    try:
        with Image.open(BytesIO(data)) as opened:
            opened.seek(0)
            if opened.width * opened.height > MAX_SOURCE_PIXELS:
                return None
            image = ImageOps.exif_transpose(opened)
            image.load()
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError):
        return None

    image = _flatten_to_rgb(image)
    if max(image.size) < min_edge:
        return None
    if max(image.size) > max_edge:
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

    # Usually quality 92 at 1600 px is well below the configured limit. Lower
    # quality only as much as necessary; if needed, reduce dimensions gradually.
    qualities = (92, 88, 84, 80, 76, 72, 68)
    for resize_round in range(5):
        for quality in qualities:
            output = BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
                subsampling=0 if quality >= 88 else 1,
            )
            encoded = output.getvalue()
            if len(encoded) <= max_bytes:
                return PreparedPreview(
                    data=encoded,
                    width=image.width,
                    height=image.height,
                )
        if resize_round == 4 or max(image.size) <= 640:
            break
        next_size = (
            max(1, int(image.width * 0.85)),
            max(1, int(image.height * 0.85)),
        )
        image = image.resize(next_size, Image.Resampling.LANCZOS)
    return None


def decorate_video_preview(
    preview: PreparedPreview,
    *,
    duration: int | None = None,
    index: int | None = None,
    total: int | None = None,
) -> PreparedPreview:
    """Add a familiar play affordance without pretending the JPEG is playable."""
    from PIL import ImageDraw, ImageFont

    try:
        with Image.open(BytesIO(preview.data)) as opened:
            image = opened.convert("RGB")
    except (OSError, UnidentifiedImageError):
        return preview
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    radius = max(24, min(width, height) // 9)
    cx, cy = width // 2, height // 2
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=(0, 0, 0, 145),
        outline=(255, 255, 255, 215),
        width=max(2, radius // 14),
    )
    triangle = [
        (cx - radius // 3, cy - radius // 2),
        (cx - radius // 3, cy + radius // 2),
        (cx + radius // 2, cy),
    ]
    draw.polygon(triangle, fill=(255, 255, 255, 245))

    labels: list[str] = []
    if duration and duration > 0:
        minutes, seconds = divmod(duration, 60)
        labels.append(f"{minutes}:{seconds:02d}")
    if index and total and total > 1:
        labels.append(f"{index}/{total}")
    label = " · ".join(labels)
    if label:
        font = ImageFont.load_default(size=max(12, min(width, height) // 25))
        box = draw.textbbox((0, 0), label, font=font)
        text_w = box[2] - box[0]
        text_h = box[3] - box[1]
        pad = max(6, text_h // 3)
        x = width - text_w - pad * 2 - max(8, width // 50)
        y = height - text_h - pad * 2 - max(8, height // 50)
        draw.rounded_rectangle(
            (x, y, x + text_w + pad * 2, y + text_h + pad * 2),
            radius=pad,
            fill=(0, 0, 0, 170),
        )
        draw.text((x + pad, y + pad), label, font=font, fill=(255, 255, 255, 255))

    output = BytesIO()
    image.save(output, format="JPEG", quality=90, optimize=True, progressive=True)
    data = output.getvalue()
    return PreparedPreview(data=data, width=width, height=height)
