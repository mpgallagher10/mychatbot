"""
Ingest job: pull a walk's data from Dropbox + Salesforce into the run.

Steps:
  1. Resolve context + photo sources:
       - Salesforce-anchored (preferred): from the turn Work_Item__c, read
         Photo_Folder_URL__c (current photos) and the prior turn's
         Photo_Folder_URL__c (prior photos), and snapshot the full turn context.
       - Fallback (no work item / SF unconfigured): use the run's Dropbox folder
         path convention and the prior run recorded in our own DB.
  2. Download + downsample the CURRENT walk's photos.
  3. Download + downsample the PRIOR walk's photos (source=prior) so the
     comparison pass has both sets keyed to this run.
  4. Mark the run INGESTED.

Idempotent: photos are keyed by (run, dropbox_path) and skipped if already
present, so a retried job resumes rather than duplicating work.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from ..models import InspectionRun, Photo
from ..services import images, storage
from ..services.dropbox_client import DropboxClient, DropboxEntry
from ..services.photo_source import (
    PhotoSourceUnsupported,
    SourceKind,
    classify_source,
)

logger = logging.getLogger(__name__)


@dataclass
class SourceResolution:
    current_source: str            # path or URL for the current walk
    prior_source: Optional[str]    # path or URL for the prior walk, if any


def run_ingest(job) -> None:
    run_id = job.payload["run_id"]
    run = InspectionRun.objects.select_related("property", "previous_run").get(pk=run_id)

    run.status = InspectionRun.Status.INGESTING
    run.error_detail = ""
    run.save(update_fields=["status", "error_detail", "updated_at"])

    try:
        resolution = _resolve_sources(run)

        dbx = DropboxClient()

        current_entries = _list_entries(dbx, resolution.current_source)
        _ingest_photos(run, dbx, current_entries, Photo.Source.CURRENT)

        if resolution.prior_source:
            prior_entries = _list_entries(dbx, resolution.prior_source)
            _ingest_photos(run, dbx, prior_entries, Photo.Source.PRIOR)
        else:
            logger.info("run#%s has no prior walk to compare against", run.pk)

        run.status = InspectionRun.Status.INGESTED
        run.ingested_at = timezone.now()
        run.save(
            update_fields=[
                "status",
                "ingested_at",
                "salesforce_snapshot",
                "photo_folder_url",
                "prior_work_item_id",
                "previous_run",
                "updated_at",
            ]
        )
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


def _resolve_sources(run: InspectionRun) -> SourceResolution:
    """Determine current/prior photo sources and snapshot Salesforce context."""
    if run.salesforce_work_item_id:
        resolved = _resolve_via_salesforce(run)
        if resolved is not None:
            return resolved
    return _resolve_via_convention(run)


def _resolve_via_salesforce(run: InspectionRun) -> Optional[SourceResolution]:
    """Anchor on the turn Work_Item__c. Returns None if SF is unconfigured."""
    from ..services.salesforce_client import SalesforceClient

    try:
        client = SalesforceClient()
    except RuntimeError as exc:
        logger.warning("Salesforce unconfigured, falling back to convention: %s", exc)
        return None

    snapshot = client.fetch_context_for_turn(run.salesforce_work_item_id)
    run.salesforce_snapshot = snapshot

    turn = snapshot.get("turn") or {}
    current_source = (turn.get("Photo_Folder_URL__c") or "").strip()
    if current_source:
        run.photo_folder_url = current_source
    else:
        # SF has no folder URL on the turn -> fall back to the run's path.
        current_source = run.dropbox_folder_path

    prior_source: Optional[str] = None
    prior_turns = snapshot.get("prior_turns") or []
    if prior_turns:
        prior_turn = prior_turns[0]
        run.prior_work_item_id = prior_turn.get("Id") or ""
        prior_source = (prior_turn.get("Photo_Folder_URL__c") or "").strip() or None
        # Link our own prior run if we've processed that turn before.
        prev = InspectionRun.objects.filter(
            salesforce_work_item_id=run.prior_work_item_id
        ).first()
        if prev:
            run.previous_run = prev

    return SourceResolution(current_source=current_source, prior_source=prior_source)


def _resolve_via_convention(run: InspectionRun) -> SourceResolution:
    """Legacy path: use the run's Dropbox folder + prior run from our DB."""
    _resolve_previous_run(run)
    _snapshot_salesforce_by_property(run)
    prior_source = (
        run.previous_run.dropbox_folder_path if run.previous_run else None
    )
    return SourceResolution(
        current_source=run.dropbox_folder_path, prior_source=prior_source
    )


def _resolve_previous_run(run: InspectionRun) -> None:
    """Link the most recent earlier run for the same property (from our DB)."""
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
        logger.info("run#%s previous_run=%s (%s)", run.pk, prev.pk, prev.walk_date)


def _list_entries(dbx: DropboxClient, source: str) -> list[DropboxEntry]:
    """List image entries for a source, branching on its kind."""
    kind = classify_source(source)
    if kind in (SourceKind.DROPBOX_PATH, SourceKind.DROPBOX_URL):
        # DropboxClient.list_images handles both account paths and shared links.
        return dbx.list_images(source)
    if kind == SourceKind.DRIVE_URL:
        raise PhotoSourceUnsupported(
            f"Google Drive photo source not yet wired: {source!r}."
        )
    raise PhotoSourceUnsupported(f"unrecognized photo source: {source!r}")


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
        raw = dbx.download(entry)
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


def _snapshot_salesforce_by_property(run: InspectionRun) -> None:
    from ..services.salesforce_client import SalesforceClient

    try:
        client = SalesforceClient()
    except RuntimeError as exc:
        logger.warning("skipping Salesforce snapshot: %s", exc)
        run.salesforce_snapshot = {"_skipped": str(exc)}
        return
    run.salesforce_snapshot = client.fetch_context(run.property.salesforce_id)
