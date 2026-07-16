from django.test import TransactionTestCase
from django.utils import timezone

from turns import queue
from turns.models import Job


class QueueTests(TransactionTestCase):
    def test_enqueue_and_claim(self):
        queue.enqueue("noop", {"x": 1})
        job = queue.claim_next("worker-a")
        self.assertIsNotNone(job)
        self.assertEqual(job.status, Job.Status.RUNNING)
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.locked_by, "worker-a")

    def test_dedup_key_reuses_active_job(self):
        j1 = queue.enqueue("ingest_run", {"run_id": 1}, dedup_key="ingest:1")
        j2 = queue.enqueue("ingest_run", {"run_id": 1}, dedup_key="ingest:1")
        self.assertEqual(j1.pk, j2.pk)
        self.assertEqual(Job.objects.count(), 1)

    def test_empty_queue_returns_none(self):
        self.assertIsNone(queue.claim_next("worker-a"))

    def test_future_run_after_not_claimed(self):
        queue.enqueue(
            "noop", {}, run_after=timezone.now() + timezone.timedelta(hours=1)
        )
        self.assertIsNone(queue.claim_next("worker-a"))

    def test_mark_failed_retries_then_permanent(self):
        job = queue.enqueue("noop", {}, max_attempts=2)
        # attempt 1
        claimed = queue.claim_next("w")
        queue.mark_failed(claimed, "boom")
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, Job.Status.QUEUED)  # requeued
        # force it runnable again and burn attempt 2
        Job.objects.filter(pk=claimed.pk).update(run_after=timezone.now())
        claimed2 = queue.claim_next("w")
        queue.mark_failed(claimed2, "boom again")
        claimed2.refresh_from_db()
        self.assertEqual(claimed2.status, Job.Status.FAILED)  # permanent

    def test_mark_succeeded(self):
        queue.enqueue("noop", {})
        job = queue.claim_next("w")
        queue.mark_succeeded(job)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.SUCCEEDED)
