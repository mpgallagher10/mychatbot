"""
Run (or re-run) the LLM evaluation passes on an ingested run, synchronously:

    python manage.py evaluate <run_id>

Useful for testing prompts against a real ingested turn, or re-evaluating after
a prompt change, without waiting for the worker. Requires ANTHROPIC_API_KEY and
that the run has already been ingested (photos present).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from turns.jobs.evaluate import run_evaluate
from turns.models import InspectionRun


class _Job:
    def __init__(self, run_id):
        self.payload = {"run_id": run_id}


class Command(BaseCommand):
    help = "Run the classification/comparison/synthesis passes on a run."

    def add_arguments(self, parser):
        parser.add_argument("run_id", type=int)

    def handle(self, *args, **options):
        run_id = options["run_id"]
        try:
            run = InspectionRun.objects.get(pk=run_id)
        except InspectionRun.DoesNotExist:
            raise CommandError(f"run #{run_id} not found")
        if not run.photos.exists():
            raise CommandError(f"run #{run_id} has no photos — ingest it first")

        self.stdout.write(f"Evaluating run #{run_id} ({run.photos.count()} photos)…")
        run_evaluate(_Job(run_id))
        run.refresh_from_db()

        self.stdout.write(self.style.SUCCESS(f"status: {run.status}"))
        self.stdout.write(f"findings: {run.findings.count()}")
        self.stdout.write(f"proposed billing: ${run.proposed_billing_total}")
        summary = (run.evaluation_summary or {}).get("summary")
        if summary:
            self.stdout.write(f"\n{summary}")
