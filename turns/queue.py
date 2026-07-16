"""
A minimal, robust Postgres-backed job queue.

Why not Celery/BullMQ+Redis? A turn run is coarse-grained (a handful of jobs
per walk, minutes each), so we don't need a broker — one Postgres table plus
`SELECT ... FOR UPDATE SKIP LOCKED` gives us concurrent, at-least-once
processing with retries and zero extra infrastructure on Railway.

`SKIP LOCKED` degrades gracefully on SQLite (used for local checks): the
`.select_for_update(skip_locked=True)` flag is silently ignored there, which
is fine for single-process dev.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from django.db import transaction
from django.utils import timezone

from .models import Job

logger = logging.getLogger(__name__)


def enqueue(
    job_type: str,
    payload: dict,
    *,
    dedup_key: str = "",
    max_attempts: int = 3,
    run_after=None,
) -> Job:
    """
    Insert a job. If `dedup_key` is set and an active (queued/running) job
    with that key already exists, return the existing job instead of creating
    a duplicate — this is what makes re-fired webhooks safe.
    """
    if dedup_key:
        existing = (
            Job.objects.filter(
                dedup_key=dedup_key,
                status__in=[Job.Status.QUEUED, Job.Status.RUNNING],
            )
            .order_by("id")
            .first()
        )
        if existing:
            logger.info(
                "enqueue: reusing active job#%s for dedup_key=%s",
                existing.pk,
                dedup_key,
            )
            return existing

    job = Job.objects.create(
        job_type=job_type,
        payload=payload,
        dedup_key=dedup_key,
        max_attempts=max_attempts,
        run_after=run_after or timezone.now(),
    )
    logger.info("enqueue: created job#%s type=%s", job.pk, job_type)
    return job


@transaction.atomic
def claim_next(worker_id: str) -> Optional[Job]:
    """
    Atomically claim the next runnable job. Returns None if the queue is empty.

    The row is locked FOR UPDATE and SKIP LOCKED, so concurrent workers each
    grab a different job. Marked RUNNING inside the same transaction.
    """
    job = (
        Job.objects.select_for_update(skip_locked=True)
        .filter(status=Job.Status.QUEUED, run_after__lte=timezone.now())
        .order_by("run_after", "id")
        .first()
    )
    if job is None:
        return None

    job.status = Job.Status.RUNNING
    job.attempts += 1
    job.locked_at = timezone.now()
    job.locked_by = worker_id
    job.save(update_fields=["status", "attempts", "locked_at", "locked_by", "updated_at"])
    return job


def mark_succeeded(job: Job) -> None:
    job.status = Job.Status.SUCCEEDED
    job.last_error = ""
    job.save(update_fields=["status", "last_error", "updated_at"])


def mark_failed(job: Job, error: str, *, backoff_base_seconds: int = 30) -> None:
    """
    Record a failure. Requeue with exponential backoff while attempts remain,
    otherwise mark permanently FAILED.
    """
    job.last_error = error[:10000]
    if job.attempts >= job.max_attempts:
        job.status = Job.Status.FAILED
        logger.error("job#%s permanently failed after %s attempts", job.pk, job.attempts)
    else:
        delay = backoff_base_seconds * (2 ** (job.attempts - 1))
        job.status = Job.Status.QUEUED
        job.run_after = timezone.now() + timedelta(seconds=delay)
        job.locked_at = None
        job.locked_by = ""
        logger.warning(
            "job#%s attempt %s failed; retrying in %ss",
            job.pk,
            job.attempts,
            delay,
        )
    job.save(
        update_fields=[
            "status",
            "last_error",
            "run_after",
            "locked_at",
            "locked_by",
            "updated_at",
        ]
    )
