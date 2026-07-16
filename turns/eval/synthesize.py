"""
Pass 3 — synthesis / reconciliation (Sonnet, text-only).

Takes the per-room findings from Pass 2 and:
  - reconciles them against the contractor's form notes (what the contractor
    reported that the model didn't find, and vice versa),
  - flags likely duplicate findings across rooms,
  - writes a run summary.

The proposed billing total is computed deterministically in code (sum of
billable findings' estimated costs) rather than trusting the model to add —
duplicates flagged here are excluded from that total.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings

from . import compare, llm
from .schemas import RunSynthesis

logger = logging.getLogger(__name__)

SYSTEM = """You are a senior reviewer finalizing a property-turn inspection. \
You are given the contractor's notes and a list of findings produced from photo \
comparison. Produce:
- summary: a concise overview of the turn's condition and the billable issues.
- contractor_reported_not_found: issues the contractor reported that are NOT \
represented in the findings list (possible misses).
- model_found_not_reported: findings not mentioned by the contractor (worth a \
closer look).
- duplicate_finding_indices: indices (0-based, into the findings list below) of \
findings that are duplicates of an earlier finding in the list.
Be precise; refer to findings by their content."""


def _findings_text(findings) -> str:
    lines = []
    for i, f in enumerate(findings):
        cost = f"${f.estimated_cost}" if f.estimated_cost is not None else "n/a"
        lines.append(
            f"[{i}] room={f.room_label} | {f.category}/{f.severity} | "
            f"billable={f.billable_to_guest} | conf={f.confidence:.2f} | "
            f"est={cost} | {f.description}"
        )
    return "\n".join(lines) if lines else "(no findings)"


def synthesize_run(run) -> None:
    findings = list(run.findings.all())
    context = compare.build_context(run)
    findings_text = _findings_text(findings)

    content = [
        {
            "type": "text",
            "text": f"{context}\n\nFINDINGS:\n{findings_text}",
        }
    ]
    result: RunSynthesis = llm.parse(
        model=settings.EVAL_MODEL_SYNTHESIZE,
        system=SYSTEM,
        content=content,
        schema=RunSynthesis,
    )

    dup_indices = {i for i in result.duplicate_finding_indices if 0 <= i < len(findings)}
    dup_finding_ids = [findings[i].pk for i in sorted(dup_indices)]

    # Deterministic billing total: billable, non-duplicate findings.
    total = Decimal("0")
    for i, f in enumerate(findings):
        if f.billable_to_guest and i not in dup_indices and f.estimated_cost is not None:
            total += Decimal(str(f.estimated_cost))

    run.proposed_billing_total = total
    run.evaluation_summary = {
        "summary": result.summary,
        "contractor_reported_not_found": result.contractor_reported_not_found,
        "model_found_not_reported": result.model_found_not_reported,
        "duplicate_finding_ids": dup_finding_ids,
        "counts": {
            "findings": len(findings),
            "billable": sum(1 for f in findings if f.billable_to_guest),
            "duplicates_flagged": len(dup_finding_ids),
        },
    }
    run.save(update_fields=["proposed_billing_total", "evaluation_summary", "updated_at"])
    logger.info(
        "run#%s: synthesized %s findings, proposed billing $%s",
        run.pk, len(findings), total,
    )
