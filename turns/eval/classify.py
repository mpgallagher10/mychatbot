"""
Pass 1 — per-photo classification (Haiku).

Labels every current and prior photo with a room/area, primary subject, and any
visible issues, as structured JSON. Cheap and parallelizable; results are stored
on the Photo rows and drive the room grouping in Pass 2.

Ordering note: these photos frequently carry no EXIF timestamp, so we do NOT
rely on capture time — the filename (which embeds a timestamp for this data set)
is passed to the model as a weak ordering/context hint only.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings

from ..models import Photo
from . import llm
from .schemas import PhotoClassification

logger = logging.getLogger(__name__)

SYSTEM = """You are a property-inspection photo classifier for a short-term \
rental turn (guest move-out) walk. For each photo, identify:
- room: the area shown, normalized to a short lowercase label \
(e.g. "kitchen", "master bath", "living room", "garage", "exterior", "hallway"). \
Use "unknown" only if genuinely indeterminate.
- subject: the primary thing shown (e.g. "refrigerator", "wall", "flooring", \
"sofa", "toilet", "countertop").
- visible_issues: short phrases for any damage, wear, stains, missing items, or \
maintenance concerns visible. Empty list if the photo looks normal.
- notes: optional brief extra context.
Be precise and conservative; do not invent issues that aren't clearly visible."""


def _classify_one(photo: Photo) -> None:
    content = [
        llm.image_block(photo.storage_path),
        {"type": "text", "text": f"Filename: {photo.filename}\nClassify this photo."},
    ]
    result: PhotoClassification = llm.parse(
        model=settings.EVAL_MODEL_CLASSIFY,
        system=SYSTEM,
        content=content,
        schema=PhotoClassification,
        max_tokens=1024,
    )
    photo.room_label = result.room[:128]
    photo.subject_label = result.subject[:128]
    photo.classification = result.model_dump()
    photo.save(update_fields=["room_label", "subject_label", "classification"])


def classify_run(run) -> int:
    """Classify all not-yet-classified photos for a run. Returns the count."""
    photos = list(run.photos.filter(room_label="").only("id", "storage_path", "filename"))
    if not photos:
        logger.info("run#%s: no photos to classify", run.pk)
        return 0

    errors: list[tuple[int, Exception]] = []

    def _safe(p: Photo):
        try:
            _classify_one(p)
        except Exception as exc:  # noqa: BLE001 - collect, surface after
            errors.append((p.pk, exc))

    workers = max(1, settings.EVAL_CLASSIFY_CONCURRENCY)
    if workers == 1:
        # Run inline (no worker thread) — simpler for debugging and avoids
        # cross-thread DB connections.
        for p in photos:
            _safe(p)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_safe, photos))

    if errors:
        # Surface a representative failure; the run will be retried by the queue.
        pk, exc = errors[0]
        raise RuntimeError(
            f"classification failed for {len(errors)}/{len(photos)} photos "
            f"(e.g. photo#{pk}: {exc})"
        )
    logger.info("run#%s: classified %s photos", run.pk, len(photos))
    return len(photos)
