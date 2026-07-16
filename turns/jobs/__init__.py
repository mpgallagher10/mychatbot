"""
Job handler registry.

Each job_type maps to a callable taking the Job instance. The worker
(turns/management/commands/runworker.py) dispatches through `HANDLERS`.
"""
from __future__ import annotations

from typing import Callable

from ..models import Job
from .evaluate import run_evaluate
from .ingest import run_ingest

HANDLERS: dict[str, Callable[[Job], None]] = {
    "ingest_run": run_ingest,
    "evaluate_run": run_evaluate,
}


class UnknownJobType(Exception):
    pass


def dispatch(job: Job) -> None:
    handler = HANDLERS.get(job.job_type)
    if handler is None:
        raise UnknownJobType(job.job_type)
    handler(job)
