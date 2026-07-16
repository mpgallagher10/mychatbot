"""
Structured-output schemas for the evaluation passes.

These Pydantic models are passed to the Anthropic SDK's `messages.parse()` as
`output_format`, so the model is constrained to return exactly this shape (no
free-form JSON parsing). Keep them free of unsupported JSON-Schema constraints
(no min/max length, no numeric bounds) — the SDK validates those client-side but
the API ignores them.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

# Category / severity vocabularies, mirrored on the Finding model.
Category = Literal["damage", "wear", "missing_item", "pre_existing", "maintenance"]
Severity = Literal["low", "medium", "high"]


# --- Pass 1: per-photo classification --------------------------------------
class PhotoClassification(BaseModel):
    room: str            # normalized area, e.g. "kitchen", "master bath", "exterior"
    subject: str         # primary subject, e.g. "refrigerator", "wall", "flooring"
    visible_issues: list[str]  # short phrases; empty when nothing notable
    notes: str = ""


# --- Pass 2: per-room comparison -------------------------------------------
class FindingOut(BaseModel):
    room: str
    description: str
    category: Category
    severity: Severity
    billable_to_guest: bool
    confidence: float          # 0..1
    estimated_cost: Optional[float] = None
    # Evidence cited by filename (the model is given the filenames alongside
    # each image); mapped back to Photo rows after the call.
    current_evidence: list[str] = []
    prior_evidence: list[str] = []


class RoomComparison(BaseModel):
    room: str
    findings: list[FindingOut]


# --- Pass 3: synthesis / reconciliation ------------------------------------
class RunSynthesis(BaseModel):
    summary: str
    # Contractor reported it but the model didn't independently find it.
    contractor_reported_not_found: list[str] = []
    # Model found it but the contractor didn't report it.
    model_found_not_reported: list[str] = []
    # Indices (into the input findings list) the model judges duplicates.
    duplicate_finding_indices: list[int] = []
