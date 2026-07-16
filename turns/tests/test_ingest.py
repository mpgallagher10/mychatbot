import io
import tempfile
from datetime import date

from django.test import TestCase, override_settings
from PIL import Image

from turns.jobs import ingest
from turns.models import InspectionRun, Photo, Property
from turns.services import salesforce_client
from turns.services.dropbox_client import (
    DropboxEntry,
    is_shared_url,
    normalize_dropbox_shared_url,
)


def _jpeg(w=2000, h=1500):
    img = Image.new("RGB", (w, h), color=(10, 200, 90))
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


class FakeDropbox:
    """In-memory stand-in for DropboxClient (path or shared-link sources)."""

    def __init__(self, folders):
        # folders: {source(path or url): [(name, bytes), ...]}
        self._folders = folders
        self._by_key = {}

    def list_images(self, source):
        if is_shared_url(source):
            source = normalize_dropbox_shared_url(source)
        entries = []
        is_url = source.lower().startswith("http")
        for name, data in self._folders.get(source, []):
            key = f"{source}#{name}".lower() if is_url else f"{source}/{name}".lower()
            entry = DropboxEntry(
                path=key,
                name=name,
                size=len(data),
                content_hash="dbxhash",
                shared_url=source if is_url else "",
                rel_path=f"/{name}" if is_url else "",
            )
            self._by_key[key] = data
            entries.append(entry)
        return entries

    def download(self, entry):
        return self._by_key[entry.path]


class _Job:
    def __init__(self, run_id):
        self.payload = {"run_id": run_id}


class IngestJobTests(TestCase):
    def _run_with_fake(self, run, folders):
        fake = FakeDropbox(folders)
        orig = ingest.DropboxClient
        ingest.DropboxClient = lambda *a, **k: fake
        try:
            ingest.run_ingest(_Job(run.pk))
        finally:
            ingest.DropboxClient = orig

    def test_ingests_current_and_prior_photos(self):
        prop = Property.objects.create(salesforce_id="P-1")
        prior = InspectionRun.objects.create(
            property=prop,
            walk_date=date(2026, 6, 1),
            idempotency_key="P-1:2026-06-01",
            dropbox_folder_path="/Turns/P-1/2026-06-01",
        )
        current = InspectionRun.objects.create(
            property=prop,
            walk_date=date(2026, 7, 16),
            idempotency_key="P-1:2026-07-16",
            dropbox_folder_path="/Turns/P-1/2026-07-16",
        )
        folders = {
            "/Turns/P-1/2026-07-16": [("a.jpg", _jpeg()), ("b.jpg", _jpeg())],
            "/Turns/P-1/2026-06-01": [("old.jpg", _jpeg())],
        }

        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(MEDIA_STORAGE_ROOT=tmp):
                self._run_with_fake(current, folders)

        current.refresh_from_db()
        self.assertEqual(current.status, InspectionRun.Status.INGESTED)
        self.assertEqual(current.previous_run_id, prior.pk)
        self.assertEqual(current.photos.filter(source=Photo.Source.CURRENT).count(), 2)
        self.assertEqual(current.photos.filter(source=Photo.Source.PRIOR).count(), 1)
        # downsampled + stored
        p = current.photos.filter(source=Photo.Source.CURRENT).first()
        self.assertEqual(p.width, 1568)
        self.assertTrue(p.storage_path.endswith(".jpg"))
        # salesforce not configured in tests -> snapshot marked skipped
        self.assertIn("_skipped", current.salesforce_snapshot)

    def test_reingest_is_idempotent(self):
        prop = Property.objects.create(salesforce_id="P-2")
        current = InspectionRun.objects.create(
            property=prop,
            walk_date=date(2026, 7, 16),
            idempotency_key="P-2:2026-07-16",
            dropbox_folder_path="/Turns/P-2/2026-07-16",
        )
        folders = {"/Turns/P-2/2026-07-16": [("a.jpg", _jpeg())]}
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(MEDIA_STORAGE_ROOT=tmp):
                self._run_with_fake(current, folders)
                self._run_with_fake(current, folders)  # second pass
        self.assertEqual(current.photos.count(), 1)  # no duplicate


class FakeSalesforce:
    """Stand-in returning a turn whose photo folder is a Dropbox path."""

    def __init__(self, *a, **k):
        pass

    def fetch_context_for_turn(self, work_item_id):
        return {
            "turn": {
                "Id": work_item_id,
                "Property__c": "a01Kj000000Prop01",
                "Date__c": "2026-07-16",
                "Photo_Folder_URL__c": "/Turns/P-1/2026-07-16",
                "Problems_Found__c": "<p>Cracked tile in master bath</p>",
            },
            "property": {"Id": "a01Kj000000Prop01", "Name": "123 Main"},
            "prior_turns": [
                {"Id": "a0XKj000000Prior1", "Photo_Folder_URL__c": "/Turns/P-1/2026-06-01"}
            ],
            "prior_findings": [],
            "open_maintenance_items": [],
        }


class SalesforceAnchoredIngestTests(TestCase):
    def test_resolves_photos_and_prior_from_salesforce(self):
        prop = Property.objects.create(salesforce_id="a01Kj000000Prop01")
        run = InspectionRun.objects.create(
            property=prop,
            walk_date=date(2026, 7, 16),
            idempotency_key="WI:a0XKj000000Turn01",
            salesforce_work_item_id="a0XKj000000Turn01",
        )
        folders = {
            "/Turns/P-1/2026-07-16": [("cur1.jpg", _jpeg()), ("cur2.jpg", _jpeg())],
            "/Turns/P-1/2026-06-01": [("old1.jpg", _jpeg())],
        }
        fake_dbx = FakeDropbox(folders)
        orig_dbx, orig_sf = ingest.DropboxClient, salesforce_client.SalesforceClient
        ingest.DropboxClient = lambda *a, **k: fake_dbx
        salesforce_client.SalesforceClient = FakeSalesforce
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with override_settings(MEDIA_STORAGE_ROOT=tmp):
                    ingest.run_ingest(_Job(run.pk))
        finally:
            ingest.DropboxClient = orig_dbx
            salesforce_client.SalesforceClient = orig_sf

        run.refresh_from_db()
        self.assertEqual(run.status, InspectionRun.Status.INGESTED)
        self.assertEqual(run.photo_folder_url, "/Turns/P-1/2026-07-16")
        self.assertEqual(run.prior_work_item_id, "a0XKj000000Prior1")
        self.assertEqual(run.photos.filter(source=Photo.Source.CURRENT).count(), 2)
        self.assertEqual(run.photos.filter(source=Photo.Source.PRIOR).count(), 1)
        self.assertEqual(run.salesforce_snapshot["turn"]["Id"], "a0XKj000000Turn01")

    def test_ingests_from_dropbox_shared_links(self):
        cur_url = "https://www.dropbox.com/scl/fo/cur123/AAA?rlkey=k1&st=abc&dl=0"
        prior_url = "https://www.dropbox.com/scl/fo/old456/BBB?rlkey=k2&st=xyz&dl=0"

        class SharedLinkSF(FakeSalesforce):
            def fetch_context_for_turn(self, work_item_id):
                ctx = super().fetch_context_for_turn(work_item_id)
                ctx["turn"]["Photo_Folder_URL__c"] = cur_url
                ctx["prior_turns"][0]["Photo_Folder_URL__c"] = prior_url
                return ctx

        prop = Property.objects.create(salesforce_id="a01Kj000000Prop02")
        run = InspectionRun.objects.create(
            property=prop,
            walk_date=date(2026, 7, 16),
            idempotency_key="WI:a0XKj000000Turn02",
            salesforce_work_item_id="a0XKj000000Turn02",
        )
        # Fake keyed by the NORMALIZED url (st/dl stripped) — what ingest lists.
        norm_cur = "https://www.dropbox.com/scl/fo/cur123/AAA?rlkey=k1"
        norm_prior = "https://www.dropbox.com/scl/fo/old456/BBB?rlkey=k2"
        folders = {
            norm_cur: [("a.jpg", _jpeg()), ("b.jpg", _jpeg())],
            norm_prior: [("a.jpg", _jpeg())],  # same filename as current
        }
        fake_dbx = FakeDropbox(folders)
        orig_dbx, orig_sf = ingest.DropboxClient, salesforce_client.SalesforceClient
        ingest.DropboxClient = lambda *a, **k: fake_dbx
        salesforce_client.SalesforceClient = SharedLinkSF
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with override_settings(MEDIA_STORAGE_ROOT=tmp):
                    ingest.run_ingest(_Job(run.pk))
        finally:
            ingest.DropboxClient = orig_dbx
            salesforce_client.SalesforceClient = orig_sf

        run.refresh_from_db()
        self.assertEqual(run.status, InspectionRun.Status.INGESTED)
        # Normalized (no volatile st/dl) URL stored on the run.
        self.assertEqual(run.photo_folder_url, cur_url)
        self.assertEqual(run.photos.filter(source=Photo.Source.CURRENT).count(), 2)
        # Identical filename across walks must NOT collide (namespaced by url).
        self.assertEqual(run.photos.filter(source=Photo.Source.PRIOR).count(), 1)
        self.assertEqual(run.photos.count(), 3)
