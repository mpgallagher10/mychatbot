"""
Data model for turn inspection processing.

Entity map:

    Property 1───* InspectionRun 1───* Photo
                        │
                        └──────────* Finding *──── evidence ───* Photo

    Job is a standalone Postgres-backed work queue row (see turns/queue.py).

An InspectionRun is the unit of idempotency: one contractor walk of one
property on one date. Re-firing the webhook for the same (property, walk_date)
resolves to the same run rather than creating a duplicate.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.crypto import get_random_string


def _make_review_token() -> str:
    # Unguessable capability token for the review URL.
    return get_random_string(40)


class Property(models.Model):
    """A managed home. Mirrors the Salesforce property record."""

    salesforce_id = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=255, blank=True)
    # Free-form external folder root, if a property overrides the convention.
    dropbox_root = models.CharField(max_length=1024, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name_plural = "properties"

    def __str__(self) -> str:
        return f"{self.name or self.salesforce_id}"


class InspectionRun(models.Model):
    """One contractor walk of one property on one date."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        INGESTING = "ingesting", "Ingesting"
        INGESTED = "ingested", "Ingested"
        EVALUATING = "evaluating", "Evaluating"
        READY_FOR_REVIEW = "ready_for_review", "Ready for review"
        REVIEWED = "reviewed", "Reviewed"
        FAILED = "failed", "Failed"

    property = models.ForeignKey(
        Property, on_delete=models.PROTECT, related_name="runs"
    )
    walk_date = models.DateField()

    # The turn/walk Work_Item__c that triggered this run. Primary trigger key
    # and the anchor for all Salesforce context (photo folder, condition notes,
    # tenant, prior-turn findings). Blank only for legacy property+date runs.
    salesforce_work_item_id = models.CharField(
        max_length=18, blank=True, db_index=True
    )

    # The prior turn's Work_Item__c id (resolved from Salesforce at ingest),
    # whose photos this run is compared against.
    prior_work_item_id = models.CharField(max_length=18, blank=True)

    # Idempotency key: the Work Item id when triggered by a turn, else
    # "{property}:{walk_date}". UNIQUE so concurrent/re-fired webhooks collapse
    # to one run.
    idempotency_key = models.CharField(max_length=128, unique=True)

    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.PENDING
    )

    # Where the current walk's photos live. Either a Dropbox path (convention)
    # or a folder URL resolved from the turn's Photo_Folder_URL__c.
    dropbox_folder_path = models.CharField(max_length=1024, blank=True)

    # Resolved photo-folder source for the current walk (Dropbox/Drive URL).
    photo_folder_url = models.CharField(max_length=1024, blank=True)

    # Raw contractor form notes (reconciled against model findings in Pass 3).
    contractor_notes = models.TextField(blank=True)

    # The immediately-preceding run for this property, used for photo-to-photo
    # comparison. Resolved at ingest time; may be null for a property's first
    # ever walk.
    previous_run = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="following_runs",
    )

    # Snapshot of everything pulled from Salesforce at ingest time (prior
    # findings, open tickets, inventory/baseline, current booking/guest). Kept
    # as JSON so the run is reproducible even if Salesforce changes later.
    salesforce_snapshot = models.JSONField(default=dict, blank=True)

    # Proposed guest-billing total, populated by the synthesis pass (Pass 3).
    proposed_billing_total = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    # Synthesis pass output: run summary, contractor/model reconciliation,
    # flagged duplicate finding ids.
    evaluation_summary = models.JSONField(default=dict, blank=True)

    # Unguessable capability token for the review URL.
    review_token = models.CharField(
        max_length=40, unique=True, default=_make_review_token, editable=False
    )
    # Link to the review micro-app view for this run (written back to SF).
    review_url = models.URLField(blank=True)

    error_detail = models.TextField(blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    ingested_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["property", "walk_date"]),
            models.Index(fields=["status"]),
        ]
        ordering = ["-walk_date", "-created_at"]

    def __str__(self) -> str:
        return f"Run {self.property} @ {self.walk_date} ({self.status})"

    @staticmethod
    def make_idempotency_key(salesforce_property_id: str, walk_date) -> str:
        return f"{salesforce_property_id}:{walk_date.isoformat()}"

    @staticmethod
    def make_work_item_key(work_item_id: str) -> str:
        return f"WI:{work_item_id}"

    def build_review_url(self) -> str:
        return f"{settings.REVIEW_BASE_URL}/review/{self.review_token}"


class Photo(models.Model):
    """A single inspection photo (current or prior walk)."""

    class Source(models.TextChoices):
        CURRENT = "current", "Current walk"
        PRIOR = "prior", "Prior walk"

    run = models.ForeignKey(
        InspectionRun, on_delete=models.CASCADE, related_name="photos"
    )
    source = models.CharField(max_length=8, choices=Source.choices)

    # Provenance in Dropbox.
    dropbox_path = models.CharField(max_length=1024)
    filename = models.CharField(max_length=512)

    # Local/volume path of the downsampled JPEG produced at ingest.
    storage_path = models.CharField(max_length=1024, blank=True)

    # sha256 of the original bytes — dedup + idempotent re-download.
    content_hash = models.CharField(max_length=64, blank=True, db_index=True)

    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    bytes_original = models.BigIntegerField(null=True, blank=True)
    bytes_downsampled = models.BigIntegerField(null=True, blank=True)

    # EXIF capture time — temporal clusters approximate room boundaries.
    exif_taken_at = models.DateTimeField(null=True, blank=True)

    # Room/area + subject label, populated by the Pass 1 classifier later.
    room_label = models.CharField(max_length=128, blank=True)
    subject_label = models.CharField(max_length=128, blank=True)
    classification = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "dropbox_path"], name="uniq_photo_per_run_path"
            )
        ]
        indexes = [
            models.Index(fields=["run", "source"]),
            models.Index(fields=["run", "room_label"]),
        ]

    def __str__(self) -> str:
        return f"{self.source}:{self.filename}"


class Finding(models.Model):
    """A single evaluated issue for a run (populated by the eval passes)."""

    class Category(models.TextChoices):
        DAMAGE = "damage", "Damage"
        WEAR = "wear", "Wear & tear"
        MISSING_ITEM = "missing_item", "Missing item"
        PRE_EXISTING = "pre_existing", "Pre-existing"
        MAINTENANCE = "maintenance", "Maintenance"

    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    class ReviewStatus(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        EDITED = "edited", "Edited"
        REJECTED = "rejected", "Rejected"

    run = models.ForeignKey(
        InspectionRun, on_delete=models.CASCADE, related_name="findings"
    )
    room_label = models.CharField(max_length=128, blank=True)
    description = models.TextField()
    category = models.CharField(max_length=16, choices=Category.choices)
    severity = models.CharField(
        max_length=8, choices=Severity.choices, default=Severity.LOW
    )
    billable_to_guest = models.BooleanField(default=False)
    estimated_cost = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    confidence = models.FloatField(default=0.0)  # 0..1 from the model

    # Evidence photos cited by the model, from both walks.
    evidence_photos = models.ManyToManyField(
        Photo, related_name="findings", blank=True
    )

    review_status = models.CharField(
        max_length=16,
        choices=ReviewStatus.choices,
        default=ReviewStatus.PENDING,
    )
    reviewer_notes = models.TextField(blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["run", "review_status"]),
            models.Index(fields=["run", "-confidence"]),
        ]
        ordering = ["-confidence"]

    def __str__(self) -> str:
        return f"[{self.category}] {self.description[:60]}"


class Job(models.Model):
    """
    Postgres-backed work queue row.

    Claimed with SELECT ... FOR UPDATE SKIP LOCKED so multiple worker
    processes can drain the queue concurrently without double-processing.
    See turns/queue.py.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    job_type = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED
    )
    attempts = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=3)

    # Don't run before this time (backoff / scheduling).
    run_after = models.DateTimeField(default=timezone.now)

    # Optional dedup handle — enqueue is a no-op if an unfinished job with the
    # same key already exists.
    dedup_key = models.CharField(max_length=200, blank=True, default="")

    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.CharField(max_length=128, blank=True)
    last_error = models.TextField(blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "run_after"]),
            models.Index(fields=["job_type", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["dedup_key"],
                condition=models.Q(status__in=["queued", "running"])
                & ~models.Q(dedup_key=""),
                name="uniq_active_job_dedup_key",
            )
        ]
        ordering = ["run_after", "id"]

    def __str__(self) -> str:
        return f"Job#{self.pk} {self.job_type} ({self.status})"
