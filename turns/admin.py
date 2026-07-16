from django.contrib import admin

from .models import Finding, InspectionRun, Job, Photo, Property


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ("salesforce_id", "name", "created_at")
    search_fields = ("salesforce_id", "name")


@admin.register(InspectionRun)
class InspectionRunAdmin(admin.ModelAdmin):
    list_display = ("id", "property", "walk_date", "status", "created_at")
    list_filter = ("status", "walk_date")
    search_fields = ("idempotency_key", "property__salesforce_id")
    readonly_fields = ("idempotency_key", "salesforce_snapshot")


@admin.register(Photo)
class PhotoAdmin(admin.ModelAdmin):
    list_display = ("id", "run", "source", "filename", "room_label", "exif_taken_at")
    list_filter = ("source",)
    search_fields = ("filename", "dropbox_path", "content_hash")


@admin.register(Finding)
class FindingAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "run",
        "category",
        "severity",
        "billable_to_guest",
        "confidence",
        "review_status",
    )
    list_filter = ("category", "severity", "review_status", "billable_to_guest")
    search_fields = ("description",)


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("id", "job_type", "status", "attempts", "run_after", "updated_at")
    list_filter = ("status", "job_type")
    search_fields = ("dedup_key", "last_error")
