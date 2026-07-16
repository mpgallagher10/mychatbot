import io
import tempfile
from datetime import date

from django.test import TestCase, override_settings
from PIL import Image

from turns.jobs import ingest
from turns.models import InspectionRun, Photo, Property
from turns.services.dropbox_client import DropboxEntry


def _jpeg(w=2000, h=1500):
    img = Image.new("RGB", (w, h), color=(10, 200, 90))
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


class FakeDropbox:
    """In-memory stand-in for DropboxClient."""

    def __init__(self, folders):
        # folders: {folder_path: [(name, bytes), ...]}
        self._folders = folders

    def list_images(self, folder_path):
        return [
            DropboxEntry(
                path=f"{folder_path}/{name}".lower(),
                name=name,
                size=len(data),
                content_hash="dbxhash",
            )
            for name, data in self._folders.get(folder_path, [])
        ]

    def download(self, path):
        for folder, files in self._folders.items():
            for name, data in files:
                if f"{folder}/{name}".lower() == path:
                    return data
        raise KeyError(path)


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
