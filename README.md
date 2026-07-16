# Turn Inspection Processing

Automated intake and evaluation pipeline for Texas Corporate Homes property
turn inspections. When a field contractor completes a walk form, this service
pulls the walk's ~400–500 photos from Dropbox plus the prior walk's photos and
Salesforce context, runs an LLM evaluation, and produces a structured findings
set to make the final human review fast and accurate.

This repository currently implements **Foundation (build steps 1–2)**: the
webhook, the job queue, the data model, and the ingest worker. The LLM
evaluation passes (3), the review micro-app (4), and Salesforce writeback (5)
build on these interfaces next.

## Architecture

```
Contractor form ──POST──▶ /webhooks/turn-completed  (web process)
  completes                    │ validate + upsert InspectionRun
                               │ enqueue "ingest_run" job  ──▶ Job table (Postgres)
                               └ 202 immediately
                                                                     │
                          runworker (worker process) ◀── claim (SKIP LOCKED)
                               │
                               ├─ Dropbox: list + download current walk photos
                               ├─ Dropbox: list + download prior walk photos
                               ├─ Pillow: downsample → 1568px q80, extract EXIF
                               ├─ Salesforce: snapshot prior findings / tickets /
                               │              inventory / current booking
                               └─ mark run INGESTED  ─▶ (next: eval passes)
```

### Why these choices

- **202-then-worker.** A 500-photo walk takes minutes to ingest — far longer
  than any webhook timeout. The webhook does only validation + enqueue.
- **Postgres job queue, no Redis.** Coarse-grained jobs (a few per walk) don't
  need a broker. `SELECT … FOR UPDATE SKIP LOCKED` gives concurrent,
  at-least-once processing with retries and backoff from one table. Run more
  `worker` replicas for throughput.
- **Turn record is the source of truth.** The turn `Work_Item__c` owns the photo
  folder (`Photo_Folder_URL__c`), condition notes, tenant, and — via
  `Related_Turn_Walk__c` — the prior turn's findings. The webhook keys on its id.
  A `/Turns/{property_id}/{yyyy-mm-dd}/` Dropbox convention remains as a
  no-Salesforce fallback.
- **Idempotency = Work Item id** (or `(property, walk_date)` in fallback mode).
  Re-fired webhooks resolve to the same `InspectionRun`; the ingest job skips
  already-downloaded photos. Safe to retry anywhere.
- **Downsample at ingest.** 1568px / JPEG q80 cuts vision token cost 5–10× with
  no meaningful loss for damage detection, and keeps each image under API
  per-image limits.

## Data model (`turns/models.py`)

| Model           | Purpose |
|-----------------|---------|
| `Property`      | Managed home, mirrors the Salesforce property record. |
| `InspectionRun` | One walk of one property on one date. Unit of idempotency; links to `previous_run` for comparison; holds the Salesforce snapshot. |
| `Photo`         | One inspection photo (`current`/`prior` source), downsampled + hashed + EXIF-dated. Room/subject labels filled by the classifier pass later. |
| `Finding`       | One evaluated issue (populated by the eval passes) with category, severity, billable flag, confidence, and cited evidence photos. |
| `Job`           | Postgres-backed work queue row. |

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in secrets as needed
python manage.py migrate
python manage.py createsuperuser        # optional, for /admin
python manage.py runserver              # web
python manage.py runworker              # worker (separate terminal)
```

With no `DATABASE_URL`, the app uses a local SQLite file. With no Dropbox /
Salesforce credentials, the ingest job raises cleanly / skips the Salesforce
snapshot, so you can exercise the webhook + queue without external accounts.

### Trigger a run

The Salesforce Flow fires when a turn `Work_Item__c` is completed and sends its
id as `work_item_id` (the primary key). The worker then reads the photo folder
(`Photo_Folder_URL__c`), condition notes (`Problems_Found__c`), tenant, and the
prior turn's findings off that record.

```bash
curl -X POST http://localhost:8000/webhooks/turn-completed \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SHARED_SECRET" \
  -d '{"work_item_id":"a0XKj000000Turn01",
       "property_id":"a01Kj000000Prop01","walk_date":"2026-07-16"}'
```

Response `202`:

```json
{"run_id": 1, "status": "pending", "created": true,
 "idempotency_key": "WI:a0XKj000000Turn01",
 "work_item_id": "a0XKj000000Turn01", "folder": ""}
```

Without `work_item_id`, the run falls back to `(property_id, walk_date)` keying
and the `/Turns/{property_id}/{yyyy-mm-dd}/` Dropbox folder convention — useful
for local testing with no Salesforce.

## Tests

```bash
python manage.py test turns
```

Covers the folder convention, image downsampling, the queue (claim / dedup /
retry-backoff), the webhook (happy path / idempotency / auth / validation), and
the ingest orchestration (with Dropbox/Salesforce faked).

## Deploying to Railway

1. Provision a **Postgres** plugin (injects `DATABASE_URL`) and a **Volume**
   mounted where `MEDIA_STORAGE_ROOT` points.
2. Set the environment variables from `.env.example`.
3. Two processes from the `Procfile`:
   - `web` — gunicorn (runs `migrate` on boot).
   - `worker` — `python manage.py runworker` (scale replicas for throughput).

## Configuration

All configuration is environment-driven — see `.env.example` for the full list
(Django, database, storage, webhook secret, image params, Dropbox, Salesforce,
Anthropic).

> **Salesforce schema:** turns/walks and maintenance items are both
> `Work_Item__c` rows discriminated by RecordTypeId (`RT_TURN` /
> `RT_MAINTENANCE`), linked to `Property__c`. Object + record-type ids are set
> in `turns/services/salesforce_client.py`; field-level API names in `WI_FIELDS`
> are marked `# TODO(confirm)` until the Work_Item__c/Property__c field lists
> are finalized.

## Next increments

3. **Evaluation** — Pass 1 classification (Haiku 4.5, Batch API), Pass 2
   per-room comparison (Sonnet 4.6), Pass 3 synthesis → populate `Finding`.
4. **Review micro-app** — one-finding-at-a-time UI, side-by-side current/prior
   evidence, approve / edit / reject, sorted by confidence.
5. **Salesforce writeback** — maintenance Cases + billing line items on
   approval; write the review URL back to the run's SF record.
```
