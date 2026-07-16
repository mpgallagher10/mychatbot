"""
Image downsampling + EXIF extraction.

Downsampling every photo to a ~1568px long edge at JPEG q80 cuts vision token
cost 5-10x with no meaningful loss for damage/condition detection, and keeps
each image under the API's per-image budget. EXIF capture time is extracted for
temporal ordering (contractors walk room by room, so time clusters ≈ rooms).
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from PIL import ExifTags, Image, ImageOps

# EXIF tag id for DateTimeOriginal (0x9003).
_EXIF_DATETIME_ORIGINAL = next(
    (k for k, v in ExifTags.TAGS.items() if v == "DateTimeOriginal"), 0x9003
)


@dataclass
class ProcessedImage:
    jpeg_bytes: bytes
    width: int
    height: int
    content_hash: str  # sha256 of the ORIGINAL bytes
    bytes_original: int
    bytes_downsampled: int
    exif_taken_at: Optional[datetime]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_exif_datetime(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    # EXIF format: "YYYY:MM:DD HH:MM:SS"
    try:
        return datetime.strptime(raw.strip(), "%Y:%m:%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def extract_exif_taken_at(image: Image.Image) -> Optional[datetime]:
    try:
        exif = image.getexif()
    except Exception:  # pragma: no cover - defensive
        return None
    if not exif:
        return None
    return _parse_exif_datetime(exif.get(_EXIF_DATETIME_ORIGINAL))


def process_image(
    original_bytes: bytes,
    *,
    long_edge_px: int = 1568,
    jpeg_quality: int = 80,
) -> ProcessedImage:
    """Downsample to a long edge of `long_edge_px` and re-encode as JPEG."""
    content_hash = sha256_hex(original_bytes)

    with Image.open(io.BytesIO(original_bytes)) as img:
        exif_taken_at = extract_exif_taken_at(img)
        # Honor EXIF orientation, then drop the tag by re-encoding.
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")

        # Only ever downscale, never upscale.
        img.thumbnail((long_edge_px, long_edge_px), Image.Resampling.LANCZOS)
        width, height = img.size

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=jpeg_quality, optimize=True)
        jpeg_bytes = out.getvalue()

    return ProcessedImage(
        jpeg_bytes=jpeg_bytes,
        width=width,
        height=height,
        content_hash=content_hash,
        bytes_original=len(original_bytes),
        bytes_downsampled=len(jpeg_bytes),
        exif_taken_at=exif_taken_at,
    )
