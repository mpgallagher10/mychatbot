"""
Review micro-app views (token-gated capability URLs).

Flow: overview -> one finding at a time (approve / edit / reject), sorted by
confidence -> summary with suggested work items -> finalize.

Access control is the unguessable per-run token in the URL. In production this
should additionally sit behind SSO/login; documented in the README.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import Finding, InspectionRun
from .suggestions import build_suggestions

_READY = {InspectionRun.Status.READY_FOR_REVIEW, InspectionRun.Status.REVIEWED}


def _get_run(token: str) -> InspectionRun:
    return get_object_or_404(InspectionRun, review_token=token)


def _ordered(run):
    return run.findings.order_by("-confidence", "id")


def _progress(run):
    findings = _ordered(run)
    total = findings.count()
    reviewed = findings.exclude(review_status=Finding.ReviewStatus.PENDING).count()
    return total, reviewed


def _next_pending(run, after_id=None):
    qs = _ordered(run).filter(review_status=Finding.ReviewStatus.PENDING)
    return qs.first()


def overview(request, token):
    run = _get_run(token)
    if run.status not in _READY:
        return render(request, "review/processing.html", {"run": run})
    total, reviewed = _progress(run)
    nxt = _next_pending(run)
    return render(
        request,
        "review/overview.html",
        {
            "run": run,
            "total": total,
            "reviewed": reviewed,
            "next": nxt,
            "summary": (run.evaluation_summary or {}).get("summary", ""),
        },
    )


def finding(request, token, finding_id):
    run = _get_run(token)
    f = get_object_or_404(Finding, pk=finding_id, run=run)
    ordered = list(_ordered(run))
    index = next((i for i, x in enumerate(ordered) if x.pk == f.pk), 0)
    total, reviewed = _progress(run)
    return render(
        request,
        "review/finding.html",
        {
            "run": run,
            "f": f,
            "position": index + 1,
            "total": total,
            "reviewed": reviewed,
            "current_photos": f.evidence_photos.filter(source="current"),
            "prior_photos": f.evidence_photos.filter(source="prior"),
            "editing": request.GET.get("edit") == "1",
            "categories": Finding.Category.choices,
            "severities": Finding.Severity.choices,
            "prev": ordered[index - 1] if index > 0 else None,
            "next": ordered[index + 1] if index + 1 < len(ordered) else None,
        },
    )


@require_POST
def act(request, token, finding_id):
    run = _get_run(token)
    f = get_object_or_404(Finding, pk=finding_id, run=run)
    action = request.POST.get("action")

    if action == "approve":
        f.review_status = Finding.ReviewStatus.APPROVED
        f.save(update_fields=["review_status", "updated_at"])
    elif action == "reject":
        f.review_status = Finding.ReviewStatus.REJECTED
        f.reviewer_notes = request.POST.get("reviewer_notes", "")
        f.save(update_fields=["review_status", "reviewer_notes", "updated_at"])
    elif action == "save":
        _apply_edit(f, request.POST)
    else:
        raise Http404("unknown action")

    nxt = _next_pending(run)
    if nxt:
        return redirect(reverse("review:finding", args=[token, nxt.pk]))
    return redirect(reverse("review:summary", args=[token]))


def _apply_edit(f: Finding, post) -> None:
    f.description = post.get("description", f.description)
    if post.get("category") in Finding.Category.values:
        f.category = post["category"]
    if post.get("severity") in Finding.Severity.values:
        f.severity = post["severity"]
    f.billable_to_guest = post.get("billable_to_guest") == "on"
    cost = post.get("estimated_cost", "").strip()
    if cost:
        try:
            f.estimated_cost = Decimal(cost)
        except (InvalidOperation, ValueError):
            pass
    else:
        f.estimated_cost = None
    f.reviewer_notes = post.get("reviewer_notes", f.reviewer_notes)
    f.review_status = Finding.ReviewStatus.EDITED
    f.save()


def photo(request, token, photo_id):
    run = _get_run(token)
    p = get_object_or_404(run.photos, pk=photo_id)
    path = Path(p.storage_path)
    if not path.exists():
        raise Http404("image not found")
    return FileResponse(open(path, "rb"), content_type="image/jpeg")


def summary(request, token):
    run = _get_run(token)
    total, reviewed = _progress(run)
    suggestions = build_suggestions(run)
    accepted = run.findings.filter(
        review_status__in=[Finding.ReviewStatus.APPROVED, Finding.ReviewStatus.EDITED]
    ).order_by("-confidence", "id")
    return render(
        request,
        "review/summary.html",
        {
            "run": run,
            "total": total,
            "reviewed": reviewed,
            "pending": total - reviewed,
            "accepted": accepted,
            "rejected_count": run.findings.filter(
                review_status=Finding.ReviewStatus.REJECTED
            ).count(),
            "suggestions": suggestions,
            "summary": (run.evaluation_summary or {}).get("summary", ""),
            "run_summary": run.evaluation_summary or {},
        },
    )


@require_POST
def finalize(request, token):
    run = _get_run(token)
    suggestions = build_suggestions(run)
    run.proposed_billing_total = suggestions.billing_total
    run.status = InspectionRun.Status.REVIEWED
    run.save(update_fields=["proposed_billing_total", "status", "updated_at"])
    return redirect(reverse("review:summary", args=[token]))
