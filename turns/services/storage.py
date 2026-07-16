"""
Local/volume storage for downsampled photos.

Layout under MEDIA_STORAGE_ROOT:
    runs/{run_id}/{source}/{content_hash}.jpg

In production MEDIA_STORAGE_ROOT points at a mounted Railway volume (or swap
this module for an S3/R2 backend without touching callers).
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings


def run_dir(run_id: int) -> Path:
    return Path(settings.MEDIA_STORAGE_ROOT) / "runs" / str(run_id)


def photo_path(run_id: int, source: str, content_hash: str) -> Path:
    return run_dir(run_id) / source / f"{content_hash}.jpg"


def write_photo(run_id: int, source: str, content_hash: str, jpeg_bytes: bytes) -> str:
    """Write downsampled JPEG bytes; return the storage path as a string."""
    dest = photo_path(run_id, source, content_hash)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(jpeg_bytes)
    return str(dest)
