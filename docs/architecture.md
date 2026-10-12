# Architecture

![The system map](architecture-map.svg)

The map above shows the parts and what flows between them. Teal is code that is always deterministic, amber is the optional language model, grey is stored data, and white is what is outside the system or what you look at. The sketch below is the same picture in text.

```text
 files (upload, or an S3 bucket)                    people (browser)
            |                                              |
            v                                              v
   register_file --> source_file_version             FastAPI  ---- job queue (Postgres)
   (normalize, hash,   + source_record                  |                |
    immutable)                                          |                v
            |                                           |          worker process
            +--------------- run (per period) <---------+------------- |
                                  |                                      |
                       engine for the profile's type                     |
                  (invoices/payments, deposits/payouts)                  |
                                  |                                      |
                  result_match / result_exception  --> cases (append-only events)
                                                          |
                                         alerts, explanations, suggestions (advice only)
```

**One database.** Postgres holds the files' records, runs, results, cases, the job queue, alert state and the log of what the bucket scan did (ADR 0003, 0007).

**Files are never edited.** A file is stored as a version; new content is the next version. A run reads the latest version of each file, and when an earlier run with the same configuration exists, only the counterparties whose records changed are matched again (ADR 0004).

**The engine decides; the model advises.** Matching and arithmetic are pure functions of the records. A language model proposes column mappings (checked against the data), suggests which invoice an ambiguous payment was meant for, and writes explanations whose records and figures a program verifies before they are shown. None of that changes a match or a case (ADR 0002).

**Cases close on data.** An exception becomes a case that follows its records across runs. A person can report a fix, accept a difference with a reason, or add notes; only new data resolves a case (ADR 0005).

**Two types of reconciliation share one shape.** One record on the left is settled by several on the right: an invoice by its payments, a deposit by its payouts (ADR 0008).

**Work runs in a worker.** Reconciliation, bucket scans, alerts, digests and retention are jobs with leases, retries and fencing (ADR 0007). With `RECON_JOBS=inline` reconciliation runs in the request and no worker is needed.

## Where things are

| | |
|---|---|
| `backend/src/recon/engine` | the matching engines (`reconcile.py`, `deposits.py`) and their result types |
| `backend/src/recon/persistence` | models, repositories, runs, cases, setup |
| `backend/src/recon/api` | the HTTP API and the queries behind it |
| `backend/src/recon/mapping`, `adjudication`, `explanation` | the stages that use a model, each with a rules-only path |
| `backend/src/recon/jobs`, `worker.py`, `ingest`, `alerts`, `retention.py` | the queue, the worker, bucket ingestion, alerts, cleanup |
| `backend/src/recon/datagen`, `evaluation` | synthetic data with known truth, and the evaluations that score against it |
| `frontend/src` | the dashboard, the setup screen, the alerts page |
