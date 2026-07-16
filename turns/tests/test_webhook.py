import json

from django.test import TestCase, override_settings

from turns.models import InspectionRun, Job, Property

URL = "/webhooks/turn-completed"


@override_settings(WEBHOOK_SHARED_SECRET="s3cret")
class WebhookTests(TestCase):
    def _post(self, body, secret="s3cret"):
        headers = {"HTTP_X_WEBHOOK_SECRET": secret} if secret is not None else {}
        return self.client.post(
            URL, data=json.dumps(body), content_type="application/json", **headers
        )

    def test_happy_path_creates_run_and_job(self):
        resp = self._post(
            {
                "property_id": "P-1",
                "walk_date": "2026-07-16",
                "contractor_notes": "broken lamp in living room",
            }
        )
        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertTrue(data["created"])
        self.assertEqual(data["folder"], "/Turns/P-1/2026-07-16")

        run = InspectionRun.objects.get(pk=data["run_id"])
        self.assertEqual(run.property.salesforce_id, "P-1")
        self.assertEqual(run.contractor_notes, "broken lamp in living room")
        self.assertEqual(
            Job.objects.filter(job_type="ingest_run", status=Job.Status.QUEUED).count(),
            1,
        )

    def test_refire_is_idempotent(self):
        first = self._post({"property_id": "P-1", "walk_date": "2026-07-16"})
        second = self._post({"property_id": "P-1", "walk_date": "2026-07-16"})
        self.assertEqual(first.json()["run_id"], second.json()["run_id"])
        self.assertFalse(second.json()["created"])
        self.assertEqual(InspectionRun.objects.count(), 1)
        self.assertEqual(Property.objects.count(), 1)
        # dedup key means only one active ingest job
        self.assertEqual(Job.objects.count(), 1)

    def test_bad_secret_rejected(self):
        resp = self._post({"property_id": "P-1", "walk_date": "2026-07-16"}, secret="wrong")
        self.assertEqual(resp.status_code, 401)

    def test_missing_fields_400(self):
        self.assertEqual(self._post({"walk_date": "2026-07-16"}).status_code, 400)
        self.assertEqual(self._post({"property_id": "P-1"}).status_code, 400)

    def test_bad_walk_date_400(self):
        resp = self._post({"property_id": "P-1", "walk_date": "07/16/2026"})
        self.assertEqual(resp.status_code, 400)

    def test_folder_disagreement_400(self):
        resp = self._post(
            {
                "property_id": "P-1",
                "walk_date": "2026-07-16",
                "dropbox_folder_path": "/Turns/P-2/2026-07-16/",
            }
        )
        self.assertEqual(resp.status_code, 400)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(URL).status_code, 405)

    def test_work_item_id_is_primary_key(self):
        resp = self._post(
            {
                "work_item_id": "a0XKj000000AbcdEFG",
                "property_id": "P-1",
                "walk_date": "2026-07-16",
            }
        )
        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertEqual(data["idempotency_key"], "WI:a0XKj000000AbcdEFG")
        self.assertEqual(data["work_item_id"], "a0XKj000000AbcdEFG")
        run = InspectionRun.objects.get(pk=data["run_id"])
        self.assertEqual(run.salesforce_work_item_id, "a0XKj000000AbcdEFG")
        # No explicit folder + SF anchor -> folder left for SF resolution.
        self.assertEqual(data["folder"], "")

    def test_invalid_work_item_id_400(self):
        resp = self._post(
            {"work_item_id": "not-an-id", "property_id": "P-1", "walk_date": "2026-07-16"}
        )
        self.assertEqual(resp.status_code, 400)
