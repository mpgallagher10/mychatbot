"""
Ingest job: pull a walk's data from Dropbox + Salesforce into the run.

Steps:
  1. Resolve the previous run (for photo-to-photo comparison).
  2. Download + downsample the CURRENT walk's photos.
  3. Download + downsample the PRIOR walk's photos (source=prior) so the
     comparison pass has both sets keyed to this run.
  4. Snapshot Salesforce context (prior findings, tickets, inventory, booking).
  5. Mark the run INGESTED.

The step is idempotent: photos are keyed by (run, dropbox_path) and skipped if
already present, so a retried job resumes rather than duplicating work.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from ..models import InspectionRun, Photo
from ..services import images, storage
from ..services.dropbox_client import DropboxClient, DropboxEntry

logger = logging.getLogger(__name__)


def run_ingest(job) -> None:
    run_id = job.payload["run_id"]
    run = InspectionRun.objects.select_related("property").get(pk=run_id)

    run.status = InspectionRun.Status.INGESTING
    run.error_detail = ""
    run.save(update_fields=["status", "error_detail", "updated_at"])

    try:
        _resolve_previous_run(run)

        dbx = DropboxClient()

        # --- Current walk photos ---
        current_entries = dbx.list_images(run.dropbox_folder_path)
        _ingest_photos(run, dbx, current_entries, Photo.Source.CURRENT)

        # --- Prior walk photos (for comparison) ---
        if run.previous_run and run.previous_run.dropbox_folder_path:
            prior_entries = dbx.list_images(run.previous_run.dropbox_folder_path)
            _ingest_photos(run, dbx, prior_entries, Photo.Source.PRIOR)
        else:
            logger.info("run#%s has no previous walk to compare against", run.pk)

        # --- Salesforce context snapshot ---
        _snapshot_salesforce(run)

        run.status = InspectionRun.Status.INGESTED
        run.ingested_at = timezone.now()
        run.save(update_fields=["status", "ingested_at", "salesforce_snapshot", "updated_at"])
        logger.info(
            "run#%s ingested: %s current, %s prior photos",
            run.pk,
            run.photos.filter(source=Photo.Source.CURRENT).count(),
            run.photos.filter(source=Photo.Source.PRIOR).count(),
        )
    except Exception as exc:  # re-raised so the queue records + retries
        run.status = InspectionRun.Status.FAILED
        run.error_detail = str(exc)
        run.save(update_fields=["status", "error_detail", "updated_at"])
        raise


def _resolve_previous_run(run: InspectionRun) -> None:
    """Link the most recent earlier run for the same property."""
    if run.previous_run_id:
        return
    prev = (
        InspectionRun.objects.filter(
            property=run.property, walk_date__lt=run.walk_date
        )
        .exclude(pk=run.pk)
        .order_by("-walk_date", "-created_at")
        .first()
    )
    if prev:
        run.previous_run = prev
        run.save(update_fields=["previous_run", "updated_at"])
        logger.info("run#%s previous_run=%s (%s)", run.pk, prev.pk, prev.walk_date)


def _ingest_photos(
    run: InspectionRun,
    dbx: DropboxClient,
    entries: list[DropboxEntry],
    source: str,
) -> None:
    existing_paths = set(
        run.photos.filter(source=source).values_list("dropbox_path", flat=True)
    )
    for entry in entries:
        if entry.path in existing_paths:
            continue
        raw = dbx.download(entry.path)
        processed = images.process_image(
            raw,
            long_edge_px=settings.IMAGE_LONG_EDGE_PX,
            jpeg_quality=settings.IMAGE_JPEG_QUALITY,
        )
        storage_path = storage.write_photo(
            run.pk, source, processed.content_hash, processed.jpeg_bytes
        )
        try:
            Photo.objects.create(
                run=run,
                source=source,
                dropbox_path=entry.path,
                filename=entry.name,
                storage_path=storage_path,
                content_hash=processed.content_hash,
                width=processed.width,
                height=processed.height,
                bytes_original=processed.bytes_original,
                bytes_downsampled=processed.bytes_downsampled,
                exif_taken_at=processed.exif_taken_at,
            )
        except IntegrityError:
            # Concurrent/retried insert of the same (run, path) — safe to skip.
            logger.debug("photo already recorded: %s", entry.path)


def _snapshot_salesforce(run: InspectionRun) -> None:
    from ..services.salesforce_client import SalesforceClient

    try:
        client = SalesforceClient()
    except RuntimeError as exc:
        # Salesforce not configured (e.g. local dev) — proceed without it.
        logger.warning("skipping Salesforce snapshot: %s", exc)
        run.salesforce_snapshot = {"_skipped": str(exc)}
        return
    run.salesforce_snapshot = client.fetch_context(run.property.salesforce_id)
