"""
HTTP entrypoints.

The turn webhook does the minimum synchronous work — validate, upsert the run,
enqueue an ingest job — and returns 202 immediately. A 500-photo walk takes
minutes to ingest, far longer than any webhook timeout, so all real work
happens in the worker.
"""
from __future__ import annotations

import hmac
import json
import logging
from datetime import date

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import queue
from .models import InspectionRun, Property
from .services.folders import FolderConventionError, build_folder_path, parse_folder_path
from .services.salesforce_client import is_valid_salesforce_id

logger = logging.getLogger(__name__)


def healthz(request: HttpRequest) -> HttpResponse:
    return JsonResponse({"status": "ok"})


def _unauthorized() -> JsonResponse:
    return JsonResponse({"error": "unauthorized"}, status=401)


def _check_secret(request: HttpRequest) -> bool:
    """Constant-time comparison of the shared webhook secret."""
    if not settings.WEBHOOK_SHARED_SECRET:
        # No secret configured (local dev) — allow, but warn once per request.
        logger.warning("WEBHOOK_SHARED_SECRET not set; webhook auth is disabled")
        return True
    provided = request.headers.get("X-Webhook-Secret", "")
    return hmac.compare_digest(provided, settings.WEBHOOK_SHARED_SECRET)


@csrf_exempt
@require_POST
def turn_completed(request: HttpRequest) -> JsonResponse:
    """
    Contractor form completion -> kick off a turn inspection run.

    Expected JSON body:
        {
          "work_item_id":         "<turn Work_Item__c id>",      # preferred key
          "property_id":          "<salesforce property id>",    # required
          "walk_date":            "2026-07-16",                   # required (ISO)
          "dropbox_folder_path":  "/Turns/<pid>/2026-07-16/",     # optional*
          "contractor_notes":     "free text..."                  # optional
        }

    When `work_item_id` is present it is the idempotency key, and the ingest
    worker resolves the photo folder + condition notes from that turn record's
    Photo_Folder_URL__c / Problems_Found__c in Salesforce. Without it, the run
    falls back to (property_id, walk_date) with the Dropbox folder convention.

    *dropbox_folder_path is optional. If given it must match the convention
     /Turns/{property_id}/{yyyy-mm-dd}/ and agree with property_id + walk_date;
     it is used as a fallback source when Salesforce has no Photo_Folder_URL__c.
    """
    if not _check_secret(request):
        return _unauthorized()

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    work_item_id = (body.get("work_item_id") or "").strip()
    property_id = (body.get("property_id") or "").strip()
    walk_date_raw = (body.get("walk_date") or "").strip()
    contractor_notes = body.get("contractor_notes") or ""
    folder_path_in = (body.get("dropbox_folder_path") or "").strip()

    if not property_id or not walk_date_raw:
        return JsonResponse(
            {"error": "property_id and walk_date are required"}, status=400
        )
    if work_item_id and not is_valid_salesforce_id(work_item_id):
        return JsonResponse(
            {"error": "work_item_id is not a valid Salesforce id"}, status=400
        )
    try:
        walk_date = date.fromisoformat(walk_date_raw)
    except ValueError:
        return JsonResponse(
            {"error": "walk_date must be ISO format YYYY-MM-DD"}, status=400
        )

    # Resolve the fallback Dropbox folder. Only a supplied folder is validated
    # against the convention (the SF Photo_Folder_URL__c is the real source when
    # a work_item_id is present).
    folder_normalized = ""
    if folder_path_in:
        try:
            ref = parse_folder_path(folder_path_in)
        except FolderConventionError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        if ref.property_id != property_id or ref.walk_date != walk_date:
            return JsonResponse(
                {
                    "error": "dropbox_folder_path disagrees with property_id/walk_date",
                    "folder": ref.normalized_path,
                },
                status=400,
            )
        folder_normalized = ref.normalized_path
    elif not work_item_id:
        # No SF anchor and no explicit folder -> derive by convention.
        folder_normalized = build_folder_path(property_id, walk_date)

    if work_item_id:
        idempotency_key = InspectionRun.make_work_item_key(work_item_id)
    else:
        idempotency_key = InspectionRun.make_idempotency_key(property_id, walk_date)

    with transaction.atomic():
        prop, _ = Property.objects.get_or_create(salesforce_id=property_id)
        run, created = InspectionRun.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "property": prop,
                "walk_date": walk_date,
                "salesforce_work_item_id": work_item_id,
                "dropbox_folder_path": folder_normalized,
                "contractor_notes": contractor_notes,
            },
        )
        if not created:
            # Re-fired webhook: refresh notes but keep the same run.
            if contractor_notes and contractor_notes != run.contractor_notes:
                run.contractor_notes = contractor_notes
                run.save(update_fields=["contractor_notes", "updated_at"])

        queue.enqueue(
            "ingest_run",
            {"run_id": run.pk},
            dedup_key=f"ingest:{idempotency_key}",
            max_attempts=settings.JOB_MAX_ATTEMPTS,
        )

    return JsonResponse(
        {
            "run_id": run.pk,
            "status": run.status,
            "created": created,
            "idempotency_key": idempotency_key,
            "work_item_id": work_item_id,
            "folder": folder_normalized,
        },
        status=202,
    )
