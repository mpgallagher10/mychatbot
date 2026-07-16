"""
Queue worker. Run as a separate Railway process:

    python manage.py runworker

Polls the Job table, claims one job at a time with SELECT ... FOR UPDATE
SKIP LOCKED, dispatches it, and records success/failure (with backoff).
Run multiple replicas for horizontal throughput — SKIP LOCKED keeps them from
colliding.
"""
from __future__ import annotations

import os
import signal
import socket
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from turns import jobs, queue


class Command(BaseCommand):
    help = "Run the Postgres-backed job queue worker."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Drain currently-available jobs then exit (useful for tests/CI).",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=settings.WORKER_POLL_INTERVAL_SECONDS,
            help="Seconds to sleep when the queue is empty.",
        )

    def handle(self, *args, **options):
        worker_id = f"{socket.gethostname()}:{os.getpid()}"
        poll_interval = options["poll_interval"]
        run_once = options["once"]

        self._running = True

        def _stop(signum, _frame):
            self.stdout.write(self.style.WARNING(f"received signal {signum}, stopping"))
            self._running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        self.stdout.write(self.style.SUCCESS(f"worker {worker_id} started"))

        while self._running:
            job = queue.claim_next(worker_id)
            if job is None:
                if run_once:
                    break
                time.sleep(poll_interval)
                continue

            self.stdout.write(f"job#{job.pk} {job.job_type} started (attempt {job.attempts})")
            try:
                jobs.dispatch(job)
                queue.mark_succeeded(job)
                self.stdout.write(self.style.SUCCESS(f"job#{job.pk} succeeded"))
            except Exception as exc:  # noqa: BLE001 - queue records every failure
                import traceback

                queue.mark_failed(job, traceback.format_exc())
                self.stdout.write(self.style.ERROR(f"job#{job.pk} failed: {exc}"))

        self.stdout.write(self.style.SUCCESS("worker stopped"))
