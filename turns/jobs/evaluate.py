"""
Evaluate job: run the three LLM passes over an ingested run.

  Pass 1 (classify)   -> label every photo by room/subject/issues
  Pass 2 (compare)    -> per-room current-vs-prior comparison -> Finding rows
  Pass 3 (synthesize) -> reconcile with contractor notes, billing total, summary

Enqueued automatically when ingest completes. Idempotent: Pass 1 skips already
classified photos and Pass 2 clears prior findings before writing.
"""
from __future__ import annotations

import logging

from ..models import InspectionRun

logger = logging.getLogger(__name__)


def run_evaluate(job) -> None:
    from ..eval import classify, compare, synthesize

    run = InspectionRun.objects.select_related("property").get(pk=job.payload["run_id"])
    run.status = InspectionRun.Status.EVALUATING
    run.error_detail = ""
    run.save(update_fields=["status", "error_detail", "updated_at"])

    try:
        classify.classify_run(run)
        compare.compare_rooms(run)
        synthesize.synthesize_run(run)

        run.status = InspectionRun.Status.READY_FOR_REVIEW
        run.save(update_fields=["status", "updated_at"])
        logger.info("run#%s evaluation complete", run.pk)
    except Exception as exc:
        run.status = InspectionRun.Status.FAILED
        run.error_detail = str(exc)
        run.save(update_fields=["status", "error_detail", "updated_at"])
        raise
