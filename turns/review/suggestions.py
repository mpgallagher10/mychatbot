"""
Turn the reviewer-approved findings into suggested Salesforce work items.

This is the bridge to step 5 (write-back): the same suggestions shown on the
review summary page are what will be created as maintenance Work_Item__c rows and
guest billing lines once the user confirms. Kept pure (no Salesforce calls) so it
can be rendered and unit-tested independently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..models import Finding

# Category -> Work_Item__c Issue_Type__c value (confirm against the org picklist).
CATEGORY_TO_ISSUE_TYPE = {
    Finding.Category.DAMAGE: "Damage",
    Finding.Category.MISSING_ITEM: "Missing Item",
    Finding.Category.MAINTENANCE: "Maintenance",
    Finding.Category.WEAR: "Wear & Tear",
    Finding.Category.PRE_EXISTING: "Pre-existing",
}
SEVERITY_TO_PRIORITY = {
    Finding.Severity.HIGH: "High",
    Finding.Severity.MEDIUM: "Medium",
    Finding.Severity.LOW: "Low",
}

# Categories that become maintenance work orders.
_MAINTENANCE_CATEGORIES = {
    Finding.Category.DAMAGE,
    Finding.Category.MISSING_ITEM,
    Finding.Category.MAINTENANCE,
}

# Review states that count as accepted.
_ACCEPTED = {Finding.ReviewStatus.APPROVED, Finding.ReviewStatus.EDITED}


@dataclass
class WorkItemSuggestion:
    finding_id: int
    room: str
    issue_type: str
    priority: str
    description: str
    billable_to_guest: bool
    estimated_cost: Decimal | None


@dataclass
class BillingLine:
    finding_id: int
    description: str
    amount: Decimal


@dataclass
class Suggestions:
    maintenance_items: list[WorkItemSuggestion] = field(default_factory=list)
    billing_lines: list[BillingLine] = field(default_factory=list)
    billing_total: Decimal = Decimal("0")


def build_suggestions(run) -> Suggestions:
    """Build maintenance + billing suggestions from a run's accepted findings."""
    result = Suggestions()
    findings = run.findings.filter(review_status__in=_ACCEPTED).order_by("-confidence", "id")
    for f in findings:
        if f.category in _MAINTENANCE_CATEGORIES:
            result.maintenance_items.append(
                WorkItemSuggestion(
                    finding_id=f.pk,
                    room=f.room_label,
                    issue_type=CATEGORY_TO_ISSUE_TYPE.get(f.category, "Maintenance"),
                    priority=SEVERITY_TO_PRIORITY.get(f.severity, "Medium"),
                    description=f.description,
                    billable_to_guest=f.billable_to_guest,
                    estimated_cost=f.estimated_cost,
                )
            )
        if f.billable_to_guest and f.estimated_cost is not None:
            result.billing_lines.append(
                BillingLine(
                    finding_id=f.pk,
                    description=f"{f.room_label}: {f.description}"[:255],
                    amount=f.estimated_cost,
                )
            )
            result.billing_total += f.estimated_cost
    return result
