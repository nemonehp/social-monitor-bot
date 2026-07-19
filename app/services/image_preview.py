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
