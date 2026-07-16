"""
Pass 2 — per-room comparison (Sonnet).

For each room, send the current walk's photos alongside the prior walk's photos
of the same room, plus the contractor's condition notes and the prior turn's
findings. The prior photos are what let the model separate guest-caused damage
from pre-existing wear — so findings are required to cite evidence photos from
both sets. Findings are persisted as Finding rows with evidence photo links.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

from django.conf import settings

from ..models import Finding, Photo
from . import llm
from .schemas import RoomComparison

logger = logging.getLogger(__name__)

SYSTEM = """You are an expert property-turn inspector for a short-term rental \
management company. You compare the CURRENT move-out walk against the PRIOR walk \
to identify property condition issues and damages, and decide what is billable to \
the departing guest.

For each real issue, produce a finding with:
- category: damage | wear | missing_item | pre_existing | maintenance
- severity: low | medium | high
- billable_to_guest: true ONLY if the issue is guest-caused damage or a missing \
item that was present in the prior walk. Normal wear-and-tear and pre-existing \
conditions (visible in the prior photos) are NOT billable.
- confidence: 0..1
- estimated_cost: your best estimate in USD for a billable repair/replacement, \
else null
- current_evidence / prior_evidence: the exact filenames of the photos that \
support this finding, from the current and prior sets respectively.

Rules:
- Cite prior_evidence whenever you call something pre_existing or wear — the \
prior photo is the justification.
- Do not bill for something you cannot distinguish from pre-existing condition.
- Reconcile with the contractor notes, but rely on the photos as ground truth.
- If a room has no issues, return an empty findings list."""

_TAG_RE = re.compile(r"<[^>]+>")


def _strip(text) -> str:
    if not text:
        return ""
    return _TAG_RE.sub(" ", str(text)).strip()


def build_context(run) -> str:
    """Assemble the contractor notes + prior-findings context text for a run."""
    snap = run.salesforce_snapshot or {}
    turn = snap.get("turn") or {}
    parts: list[str] = []

    notes = "\n".join(
        filter(
            None,
            [
                run.contractor_notes,
                _strip(turn.get("Problems_Found__c")),
                _strip(turn.get("Maintenance_Issues_Observed__c")),
                _strip(turn.get("Notes__c")),
            ],
        )
    )
    parts.append(f"CONTRACTOR NOTES:\n{notes or '(none provided)'}")

    prior_findings = snap.get("prior_findings") or []
    if prior_findings:
        lines = []
        for f in prior_findings[:50]:
            desc = _strip(f.get("Description__c") or f.get("External_Desc_AI__c"))
            lines.append(
                f"- [{f.get('Issue_Type__c') or 'issue'} / {f.get('Status__c')}] {desc}"
            )
        parts.append("PRIOR WALK FINDINGS (maintenance items):\n" + "\n".join(lines))

    prior_turns = snap.get("prior_turns") or []
    if prior_turns:
        prior_notes = _strip(prior_turns[0].get("Problems_Found__c"))
        if prior_notes:
            parts.append("PRIOR WALK CONDITION NOTES:\n" + prior_notes)

    return "\n\n".join(parts)


def _group_by_room(photos) -> dict[str, list[Photo]]:
    grouped: dict[str, list[Photo]] = defaultdict(list)
    for p in photos:
        grouped[p.room_label or "unknown"].append(p)
    return grouped


def _batches(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def compare_rooms(run) -> int:
    """Run the comparison for every room; persist findings. Returns count."""
    context = build_context(run)
    current = _group_by_room(run.photos.filter(source=Photo.Source.CURRENT))
    prior = _group_by_room(run.photos.filter(source=Photo.Source.PRIOR))
    cap = settings.EVAL_MAX_IMAGES_PER_ROOM

    # Clear any prior evaluation for idempotent re-runs.
    run.findings.all().delete()

    total_findings = 0
    for room, cur_photos in sorted(current.items()):
        prior_photos = prior.get(room, [])
        # Split large rooms into batches so no single call is oversized; pair
        # each current batch with the full (capped) prior set for context.
        cur_batches = list(_batches(cur_photos, cap))
        prior_capped = prior_photos[:cap]
        if len(prior_photos) > cap:
            logger.warning(
                "run#%s room %s: capping prior photos %s->%s for comparison",
                run.pk, room, len(prior_photos), cap,
            )
        for bi, cur_batch in enumerate(cur_batches):
            total_findings += _compare_one(
                run, room, cur_batch, prior_capped, context, bi, len(cur_batches)
            )
    logger.info("run#%s: %s findings across %s rooms", run.pk, total_findings, len(current))
    return total_findings


def _photo_blocks(label: str, photos: list[Photo]) -> list[dict]:
    blocks: list[dict] = [{"type": "text", "text": f"--- {label} PHOTOS ---"}]
    for p in photos:
        blocks.append({"type": "text", "text": f"Filename: {p.filename}"})
        blocks.append(llm.image_block(p.storage_path))
    return blocks


def _compare_one(run, room, cur_batch, prior_capped, context, batch_idx, n_batches) -> int:
    header = f"ROOM: {room}"
    if n_batches > 1:
        header += f" (batch {batch_idx + 1}/{n_batches})"
    content = [{"type": "text", "text": f"{header}\n\n{context}"}]
    content += _photo_blocks("CURRENT", cur_batch)
    if prior_capped:
        content += _photo_blocks("PRIOR", prior_capped)
    else:
        content.append(
            {"type": "text", "text": "--- No prior photos available for this room ---"}
        )

    result: RoomComparison = llm.parse(
        model=settings.EVAL_MODEL_COMPARE,
        system=SYSTEM,
        content=content,
        schema=RoomComparison,
    )

    # Map cited filenames back to Photo rows for this run.
    by_name = {p.filename: p for p in run.photos.all()}
    created = 0
    for fo in result.findings:
        finding = Finding.objects.create(
            run=run,
            room_label=(fo.room or room)[:128],
            description=fo.description,
            category=fo.category,
            severity=fo.severity,
            billable_to_guest=fo.billable_to_guest,
            estimated_cost=fo.estimated_cost,
            confidence=max(0.0, min(1.0, fo.confidence)),
        )
        evidence = [
            by_name[n]
            for n in (fo.current_evidence + fo.prior_evidence)
            if n in by_name
        ]
        if evidence:
            finding.evidence_photos.set(evidence)
        created += 1
    return created
