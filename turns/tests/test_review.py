import io
import tempfile
from datetime import date
from pathlib import Path

from django.test import TestCase
from PIL import Image

from turns.models import Finding, InspectionRun, Photo, Property
from turns.review.suggestions import build_suggestions


def _jpeg_file(dir_, name):
    img = Image.new("RGB", (64, 48), (100, 150, 200))
    p = Path(dir_) / name
    img.save(p, format="JPEG")
    return str(p)


class ReviewFlowTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.prop = Property.objects.create(salesforce_id="a01Kj000000Prop01", name="123 Main")
        self.run = InspectionRun.objects.create(
            property=self.prop,
            walk_date=date(2026, 7, 16),
            idempotency_key="WI:a0XKj000000Turn01",
            status=InspectionRun.Status.READY_FOR_REVIEW,
            evaluation_summary={"summary": "One damaged TV."},
        )
        self.cur = Photo.objects.create(
            run=self.run, source=Photo.Source.CURRENT, dropbox_path="/c/tv.jpg",
            filename="tv.jpg", storage_path=_jpeg_file(self.tmp, "tv.jpg"),
        )
        self.pri = Photo.objects.create(
            run=self.run, source=Photo.Source.PRIOR, dropbox_path="/p/tv.jpg",
            filename="old_tv.jpg", storage_path=_jpeg_file(self.tmp, "old_tv.jpg"),
        )
        self.f1 = Finding.objects.create(
            run=self.run, room_label="living room", description="Cracked TV screen",
            category="damage", severity="high", billable_to_guest=True,
            estimated_cost=600, confidence=0.92,
        )
        self.f1.evidence_photos.set([self.cur, self.pri])
        self.f2 = Finding.objects.create(
            run=self.run, room_label="kitchen", description="Scuffed floor (pre-existing)",
            category="pre_existing", severity="low", billable_to_guest=False,
            confidence=0.4,
        )
        self.token = self.run.review_token

    def test_overview_lists_progress(self):
        r = self.client.get(f"/review/{self.token}")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "0 of 2 reviewed")
        self.assertContains(r, "One damaged TV.")

    def test_processing_when_not_ready(self):
        self.run.status = InspectionRun.Status.INGESTING
        self.run.save()
        r = self.client.get(f"/review/{self.token}")
        self.assertContains(r, "still processing")

    def test_finding_page_shows_evidence(self):
        r = self.client.get(f"/review/{self.token}/finding/{self.f1.id}")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Cracked TV screen")
        self.assertContains(r, f"/review/{self.token}/photo/{self.cur.id}")
        self.assertContains(r, f"/review/{self.token}/photo/{self.pri.id}")
        self.assertContains(r, "Billable to guest")

    def test_findings_sorted_by_confidence(self):
        # First pending (highest confidence) is f1.
        r = self.client.get(f"/review/{self.token}")
        self.assertContains(r, f"/finding/{self.f1.id}")

    def test_approve_advances_to_next_then_summary(self):
        r = self.client.post(
            f"/review/{self.token}/finding/{self.f1.id}/act", {"action": "approve"}
        )
        self.assertRedirects(r, f"/review/{self.token}/finding/{self.f2.id}",
                             fetch_redirect_response=False)
        self.f1.refresh_from_db()
        self.assertEqual(self.f1.review_status, Finding.ReviewStatus.APPROVED)
        # Approve the last one -> summary.
        r2 = self.client.post(
            f"/review/{self.token}/finding/{self.f2.id}/act", {"action": "reject"}
        )
        self.assertRedirects(r2, f"/review/{self.token}/summary",
                             fetch_redirect_response=False)

    def test_edit_updates_fields(self):
        r = self.client.post(
            f"/review/{self.token}/finding/{self.f1.id}/act",
            {"action": "save", "description": "Cracked 65in TV",
             "category": "damage", "severity": "medium",
             "estimated_cost": "450.00", "billable_to_guest": "on",
             "reviewer_notes": "confirmed guest damage"},
        )
        self.assertEqual(r.status_code, 302)
        self.f1.refresh_from_db()
        self.assertEqual(self.f1.review_status, Finding.ReviewStatus.EDITED)
        self.assertEqual(self.f1.description, "Cracked 65in TV")
        self.assertEqual(self.f1.severity, "medium")
        self.assertEqual(str(self.f1.estimated_cost), "450.00")
        self.assertEqual(self.f1.reviewer_notes, "confirmed guest damage")

    def test_photo_serves_image_bytes(self):
        r = self.client.get(f"/review/{self.token}/photo/{self.cur.id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/jpeg")
        content = b"".join(r.streaming_content)
        self.assertTrue(content.startswith(b"\xff\xd8"))  # JPEG magic

    def test_photo_scoped_to_run(self):
        other = InspectionRun.objects.create(
            property=self.prop, walk_date=date(2026, 6, 1),
            idempotency_key="WI:other", status=InspectionRun.Status.READY_FOR_REVIEW,
        )
        r = self.client.get(f"/review/{other.review_token}/photo/{self.cur.id}")
        self.assertEqual(r.status_code, 404)  # photo belongs to a different run

    def test_summary_and_finalize(self):
        self.client.post(f"/review/{self.token}/finding/{self.f1.id}/act", {"action": "approve"})
        self.client.post(f"/review/{self.token}/finding/{self.f2.id}/act", {"action": "reject"})
        r = self.client.get(f"/review/{self.token}/summary")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Cracked TV screen")     # accepted maintenance item
        self.assertContains(r, "$600.00")               # billing total
        # Finalize.
        rf = self.client.post(f"/review/{self.token}/finalize")
        self.assertRedirects(rf, f"/review/{self.token}/summary",
                             fetch_redirect_response=False)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, InspectionRun.Status.REVIEWED)
        self.assertEqual(str(self.run.proposed_billing_total), "600.00")

    def test_bad_token_404(self):
        self.assertEqual(self.client.get("/review/nope").status_code, 404)


class SuggestionsTests(TestCase):
    def test_only_accepted_findings_and_billing_math(self):
        prop = Property.objects.create(salesforce_id="a01Kj000000Prop02")
        run = InspectionRun.objects.create(
            property=prop, walk_date=date(2026, 7, 16), idempotency_key="WI:s",
        )
        Finding.objects.create(  # approved billable damage -> work item + billing
            run=run, room_label="bath", description="broken mirror", category="damage",
            severity="high", billable_to_guest=True, estimated_cost=120, confidence=0.9,
            review_status=Finding.ReviewStatus.APPROVED,
        )
        Finding.objects.create(  # edited maintenance, not billable -> work item only
            run=run, room_label="hvac", description="replace filter", category="maintenance",
            severity="low", billable_to_guest=False, confidence=0.8,
            review_status=Finding.ReviewStatus.EDITED,
        )
        Finding.objects.create(  # rejected -> excluded entirely
            run=run, description="phantom", category="damage", billable_to_guest=True,
            estimated_cost=999, review_status=Finding.ReviewStatus.REJECTED,
        )
        Finding.objects.create(  # pending -> excluded
            run=run, description="unreviewed", category="damage", billable_to_guest=True,
            estimated_cost=50,
        )
        s = build_suggestions(run)
        self.assertEqual(len(s.maintenance_items), 2)
        self.assertEqual(len(s.billing_lines), 1)
        self.assertEqual(str(s.billing_total), "120.00")
