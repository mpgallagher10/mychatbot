from datetime import date

from django.test import TestCase, override_settings

from turns.eval import classify, compare, llm, synthesize
from turns.eval.schemas import (
    FindingOut,
    PhotoClassification,
    RoomComparison,
    RunSynthesis,
)
from turns.models import Finding, InspectionRun, Photo, Property


def _make_run(**snapshot):
    prop = Property.objects.create(salesforce_id="a01Kj000000Prop01")
    run = InspectionRun.objects.create(
        property=prop,
        walk_date=date(2026, 7, 16),
        idempotency_key="WI:a0XKj000000Turn01",
        salesforce_work_item_id="a0XKj000000Turn01",
        contractor_notes="broken lamp in living room",
        salesforce_snapshot=snapshot or {},
    )
    return run


def _photo(run, source, filename, room=""):
    return Photo.objects.create(
        run=run,
        source=source,
        dropbox_path=f"/{source}/{filename}",
        filename=filename,
        storage_path=f"/tmp/{filename}",
        room_label=room,
    )


class _StubImage:
    """Patch llm.image_block so tests need no real files."""

    def __enter__(self):
        self._orig = llm.image_block
        llm.image_block = lambda path, **k: {"type": "text", "text": f"[img {path}]"}
        return self

    def __exit__(self, *a):
        llm.image_block = self._orig


def _patch_parse(fn):
    orig = llm.parse
    llm.parse = fn
    return orig


@override_settings(EVAL_CLASSIFY_CONCURRENCY=1)
class ClassifyTests(TestCase):
    def test_classifies_every_photo(self):
        run = _make_run()
        _photo(run, Photo.Source.CURRENT, "a.jpg")
        _photo(run, Photo.Source.PRIOR, "old.jpg")

        def fake_parse(*, schema, content, **k):
            return PhotoClassification(
                room="kitchen", subject="refrigerator", visible_issues=["dent"]
            )

        with _StubImage():
            orig = _patch_parse(fake_parse)
            try:
                n = classify.classify_run(run)
            finally:
                llm.parse = orig

        self.assertEqual(n, 2)
        for p in run.photos.all():
            self.assertEqual(p.room_label, "kitchen")
            self.assertEqual(p.subject_label, "refrigerator")
            self.assertEqual(p.classification["visible_issues"], ["dent"])

    def test_skips_already_classified(self):
        run = _make_run()
        _photo(run, Photo.Source.CURRENT, "a.jpg", room="kitchen")
        calls = []

        def fake_parse(**k):
            calls.append(1)
            return PhotoClassification(room="x", subject="y", visible_issues=[])

        with _StubImage():
            orig = _patch_parse(fake_parse)
            try:
                n = classify.classify_run(run)
            finally:
                llm.parse = orig
        self.assertEqual(n, 0)
        self.assertEqual(calls, [])


class CompareTests(TestCase):
    def test_creates_findings_with_evidence(self):
        run = _make_run(
            turn={"Problems_Found__c": "<p>scratched floor</p>"},
            prior_findings=[
                {"Issue_Type__c": "Flooring", "Status__c": "Closed",
                 "Description__c": "prior scuff"}
            ],
        )
        cur = _photo(run, Photo.Source.CURRENT, "cur.jpg", room="kitchen")
        pri = _photo(run, Photo.Source.PRIOR, "old.jpg", room="kitchen")

        def fake_parse(*, schema, **k):
            self.assertIs(schema, RoomComparison)
            return RoomComparison(
                room="kitchen",
                findings=[
                    FindingOut(
                        room="kitchen",
                        description="Gouge in the floor not present prior",
                        category="damage",
                        severity="medium",
                        billable_to_guest=True,
                        confidence=0.82,
                        estimated_cost=250.0,
                        current_evidence=["cur.jpg"],
                        prior_evidence=["old.jpg"],
                    )
                ],
            )

        with _StubImage():
            orig = _patch_parse(fake_parse)
            try:
                n = compare.compare_rooms(run)
            finally:
                llm.parse = orig

        self.assertEqual(n, 1)
        f = Finding.objects.get(run=run)
        self.assertTrue(f.billable_to_guest)
        self.assertEqual(f.category, "damage")
        self.assertAlmostEqual(f.confidence, 0.82)
        self.assertEqual(
            set(f.evidence_photos.values_list("filename", flat=True)),
            {"cur.jpg", "old.jpg"},
        )

    def test_build_context_extracts_notes(self):
        run = _make_run(
            turn={
                "Problems_Found__c": "<div>guest broke the blinds</div>",
                "Maintenance_Issues_Observed__c": "AC filter dirty",
            },
        )
        ctx = compare.build_context(run)
        self.assertIn("broken lamp in living room", ctx)  # webhook notes
        self.assertIn("guest broke the blinds", ctx)      # HTML stripped
        self.assertIn("AC filter dirty", ctx)

    def test_reruns_replace_findings(self):
        run = _make_run()
        _photo(run, Photo.Source.CURRENT, "cur.jpg", room="bath")
        Finding.objects.create(run=run, description="stale", category="damage")

        def fake_parse(**k):
            return RoomComparison(room="bath", findings=[])

        with _StubImage():
            orig = _patch_parse(fake_parse)
            try:
                compare.compare_rooms(run)
            finally:
                llm.parse = orig
        # Stale finding cleared, none re-created.
        self.assertEqual(run.findings.count(), 0)


class SynthesizeTests(TestCase):
    def test_computes_billing_total_excluding_duplicates(self):
        run = _make_run()
        Finding.objects.create(
            run=run, description="broken tv", category="damage",
            billable_to_guest=True, estimated_cost=400, confidence=0.9,
        )
        dup = Finding.objects.create(
            run=run, description="broken tv again", category="damage",
            billable_to_guest=True, estimated_cost=400, confidence=0.9,
        )
        Finding.objects.create(
            run=run, description="normal wear", category="wear",
            billable_to_guest=False, estimated_cost=None, confidence=0.5,
        )

        # findings ordered by -confidence; both billables tie -> order stable by id.
        findings = list(run.findings.all())
        dup_index = findings.index(next(f for f in findings if f.pk == dup.pk))

        def fake_parse(*, schema, **k):
            self.assertIs(schema, RunSynthesis)
            return RunSynthesis(
                summary="One TV damaged.",
                contractor_reported_not_found=[],
                model_found_not_reported=["broken tv"],
                duplicate_finding_indices=[dup_index],
            )

        orig = _patch_parse(fake_parse)
        try:
            synthesize.synthesize_run(run)
        finally:
            llm.parse = orig

        run.refresh_from_db()
        # 400 (one TV) — duplicate excluded, wear non-billable.
        self.assertEqual(str(run.proposed_billing_total), "400.00")
        self.assertEqual(run.evaluation_summary["counts"]["billable"], 2)
        self.assertEqual(run.evaluation_summary["counts"]["duplicates_flagged"], 1)
        self.assertEqual(run.evaluation_summary["duplicate_finding_ids"], [dup.pk])


class EvaluateJobTests(TestCase):
    def test_full_pipeline_dispatches_by_schema(self):
        run = _make_run()
        _photo(run, Photo.Source.CURRENT, "cur.jpg")
        _photo(run, Photo.Source.PRIOR, "old.jpg")

        def fake_parse(*, schema, **k):
            if schema is PhotoClassification:
                return PhotoClassification(room="kitchen", subject="wall", visible_issues=[])
            if schema is RoomComparison:
                return RoomComparison(
                    room="kitchen",
                    findings=[FindingOut(
                        room="kitchen", description="hole in wall", category="damage",
                        severity="high", billable_to_guest=True, confidence=0.9,
                        estimated_cost=150.0, current_evidence=["cur.jpg"],
                        prior_evidence=["old.jpg"],
                    )],
                )
            return RunSynthesis(summary="ok")

        from turns.jobs.evaluate import run_evaluate

        class _Job:
            payload = {"run_id": run.pk}

        with _StubImage(), override_settings(EVAL_CLASSIFY_CONCURRENCY=1):
            orig = _patch_parse(fake_parse)
            try:
                run_evaluate(_Job())
            finally:
                llm.parse = orig

        run.refresh_from_db()
        self.assertEqual(run.status, InspectionRun.Status.READY_FOR_REVIEW)
        self.assertEqual(run.findings.count(), 1)
        self.assertEqual(str(run.proposed_billing_total), "150.00")
