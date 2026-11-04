# Operations

How the parts outside the model work: how a run reuses earlier work, the job queue, files from a bucket, alerts, the second type of reconciliation, cases, the API and the dashboard, and what is cleaned up. The README has the short version.

## How a run works

A profile holds the column mappings for each source file and the matching thresholds. Registering a file stores its content as a new version, with a snapshot of its normalized records; rows that cannot be normalized are kept with the reason. A run reads the latest version of each file for a period.

When an earlier run exists with the same configuration, the new run compares the records of the two versions and matches again only the customers whose records changed, reusing the earlier result for the rest. Matching every customer again gives the same result.

## Running reconciliations in a queue

A reconciliation of a large period can take longer than a request should. With `RECON_JOBS=queue` the upload and the setup confirmation queue a `reconcile_period` job and return `202` with the job; `GET /api/jobs/{id}` shows it queued, running, succeeded (with the run and the case changes) or failed (with the reason). The dashboard waits for the job and then shows what it would have shown from the request. The default, `RECON_JOBS=inline`, runs the work in the request and needs no worker.

The queue is a table in Postgres (`job`, `recon/jobs/queue.py`) and the worker is `python -m recon.worker`:

- A worker claims one due job with `FOR UPDATE SKIP LOCKED`, so two workers never take the same one, and the claim counts as an attempt.
- The claim sets a lease (`RECON_JOB_LEASE_SECONDS`, 60) that a second thread of the worker extends while the job runs, so a job can take longer than the lease. If the worker dies, the lease runs out and the next worker to look puts the job back, or fails it if it has used its attempts (`RECON_JOB_MAX_ATTEMPTS`, 5).
- Only the holder of the lease can finish a job. A worker that was cut off for longer than the lease finds out when it finishes, and its result is dropped; the job belongs to whoever took it over.
- A failed attempt waits before the next one: 5 seconds doubling to 5 minutes, with up to a fifth added at random. An error the handler marks permanent, such as a payload that cannot be read, fails at once.
- Two requests to reconcile the same period while the first is still waiting are one job. A request made while a job runs queues another, because the running one may already have read its input, and the second does not start until the first has finished. The reconciliation itself is safe to run twice at the same time, so this ordering is for tidiness, not correctness.
- The database refuses a job that makes no sense, for example one that is running with no lease or succeeded with no result.

What is not covered: a handler cannot be cancelled, so a worker that lost its job still runs it to the end. Times come from the workers' clocks, so the lease has to be much longer than their clock error. There is one queue and no priorities. The wait for a result in the dashboard is a poll once a second.

## Files from a bucket

A worker can watch an S3-compatible bucket and register the files that arrive, so an export can be dropped in a bucket instead of uploaded through the screen. It is on when `RECON_S3_BUCKET` is set (with `RECON_S3_ENDPOINT_URL` for anything other than AWS, `RECON_S3_ACCESS_KEY`, `RECON_S3_SECRET_KEY`, `RECON_S3_PREFIX` and `RECON_S3_POLL_SECONDS`, 30).

A key says where the file belongs:

```text
<prefix><profile>/<YYYY-MM>/<source>[-anything].csv      spanish-erp-and-bank/2026-01/payments-2026-01-31.csv
```

`<profile>` is the profile's name in lower case with each run of other characters turned into one hyphen, and `<source>` is `invoices` or `payments`. Anything after the source name and before `.csv` is free, so a re-export under a new name works.

The worker queues a `scan_bucket` job every poll interval (several workers may do so; waiting duplicates count as one). A scan lists the bucket and, for each object it has not dealt with, finds the profile, downloads the object and registers it as a file version, exactly as an upload does. When that completes a period it queues the reconciliation. What was done with each object is kept in `ingested_object`, keyed by key and ETag:

- an object is read once per ETag, so a second scan reads nothing it already has, and an overwritten object is new;
- the same bytes under a new ETag (a re-upload) are noted as unchanged and change nothing;
- a file that cannot be read with the profile's mapping is recorded as failed with the reason and is not tried again until the object changes;
- an object that cannot be placed (the name does not fit, no such profile, no such source, two profiles with the same slug, too large) is recorded as skipped with the reason, and is looked at again on every scan, so a file dropped before its profile exists is picked up when the profile is created;
- if the store fails in the middle of a scan the job fails and is retried; what was already registered stays.

`uv run pytest tests/test_ingest.py` (in `backend`) runs the naming, the scan against an in-memory store, a worker running scans on a timer, and the S3 client over HTTP against an in-process S3 server (moto), including a listing of more than one page. `docker compose --profile s3 up --build` adds SeaweedFS, a bucket named `recon`, and a worker that ingests from it (the keys in `docker-compose.yml` are for that local stack only). MinIO's community images are no longer published, which is why the compose file uses SeaweedFS. A CI job drops the demo files in that bucket and waits for the period to reconcile.

Not covered: an object is read whole into memory, so the size limit (`RECON_MAX_UPLOAD_MB`) matters; there is no notification from the bucket, only polling; and an object deleted from the bucket does not remove anything that was registered from it.

## Alerts

`Alerts` in the header sets up where messages go: a Slack or Teams webhook or an email address. A channel gets a message in two cases, each of which it can switch off: after a run that opens or reopens exceptions (how many, what is open now and the amount at stake by currency, with a link to the period), and once a day as a digest of everything open, at an hour of its choosing in UTC. A worker that was down at that hour sends the digest when it is back, and no digest is sent when nothing is open. "Send a test" sends a message now and shows how it went; the page lists the latest messages with their result and, for a failure, the reason.

After a reconciliation job succeeds the worker queues a `send_alerts` job for that run, and a timer queues `send_digest` every ten minutes. Both are jobs on the queue described above, so a failed send is retried with backoff. What stops a retry from sending twice is `alert_delivery`, which has one row per channel and event (`run:<run id>`, `digest:<date>`): a delivery is claimed before it is sent and marked afterwards. This makes delivery at-least-once, not exactly-once: a worker that dies between the send and the mark causes a second message when the claim expires after two minutes. Reconciling the same files again is the same run and sends nothing new. An error that cannot pass (a revoked webhook, a refused address) is recorded and not retried; a timeout, a refused connection, a 429 or a 5xx is.

A webhook URL is typed in by whoever sets up the channel and the server posts to it, so it is checked. It must be https and must not resolve to a loopback, private, link-local or otherwise internal address, which stops a channel from being used to reach services on the server's own network. Redirects are not followed. The check is made again when sending. It resolves the name here and the HTTP client resolves it again, so a hostile DNS server could answer differently the second time; I have not closed that. `RECON_ALERTS_ALLOW_HTTP` and `RECON_ALERTS_ALLOW_PRIVATE` relax the checks for a local setup, and the tests use them.

Email goes through the SMTP server named by `RECON_SMTP_HOST` (`RECON_SMTP_PORT`, `RECON_SMTP_USER`, `RECON_SMTP_PASSWORD`, `RECON_SMTP_STARTTLS`, `RECON_SMTP_FROM`); without one an email channel cannot be created. `RECON_PUBLIC_URL` is the address put in the links.

The Slack message is `{"text": …}` for an incoming webhook. The Teams message is an Adaptive Card in a `message`, which is the format a Teams Workflows webhook takes after Microsoft's retirement of the old connectors; I wrote it from Microsoft's description and community examples and have not sent it to a Teams tenant, so it is untested against the real service. Slack is likewise tested against a local server that records what it receives, not against Slack. Email is tested against a real SMTP server run in the tests.

Not covered: webhook URLs are stored as typed in the database, not encrypted, and shown masked; there is no one-click unsubscribe, rate limit or quiet hours; the digest is UTC only; and the page has no way to edit a channel's target, only to delete and add it again.

## Bank deposits against processor payouts

A second type of reconciliation matches the deposits on a bank statement against the payouts a payment processor says it sent. It differs from invoices and payments in one way: a deposit usually carries several payouts, so the relation is many payouts to one deposit. In the engine's terms the deposit is the one record and its payouts are the several that settle it, which is the shape an invoice and its payments already have, so the stored match, the cases and the incremental runs are unchanged. The stored role names are still `invoice` (the deposit) and `payment` (a payout) for either type; a profile's type (`recon/profiles.py`) says what to call them, and the API and the screens use "deposit" and "payout".

`recon/engine/deposits.py` is the engine. It is a pure function like the invoice engine, and every record ends in one match or one exception:

1. A payout whose reference is a deposit's id, for the same account (the counterparty), belongs to that deposit. The deposit matches if its payouts add up to it; otherwise it is a `deposit_mismatch` with the difference (deposit minus payouts, so a fee taken shows as negative), or a `currency_mismatch`. This is the reference pass of the invoice engine, which both now share.
2. For the deposits that are left, a search looks for sets of payouts with no reference, from the same account and currency and paid out up to ten days before the deposit, that add up to it. One set that no other deposit could also use is `proposed_grouping`: it is shown and never applied, because amounts adding up is not a reference. Several sets, or deposits that could share payouts, are `ambiguous_grouping`.
3. A deposit nothing explains is `missing_payouts`, and a payout that is in no deposit is `undeposited_payout`.

The search is bounded: at most 30 candidate payouts per deposit (the nearest in date), at most 8 payouts in a group, and a fixed number of steps, and a search that runs out of steps is reported as ambiguous rather than as nothing found. Refunds and zero payouts never join a group. The window is ten days and not the 45 days of the invoice engine because with a month of unreferenced payouts, unrelated payouts add up to the same total by chance: one seed of generated data came out ambiguous where the groups were distinct until I narrowed it. A payout that reaches the bank more than ten days after it was paid out is not found by amount: without a reference it is reported as `undeposited_payout` and its deposit as `missing_payouts`. A payout with a reference is not subject to the window.

`python -m recon.datagen.batches --out DIR --seed 1 --deposits 100` writes `payouts.csv`, `deposits.csv` and `ground_truth.json`: most deposits are clean, and the rest carry a known problem of each of the six kinds (a fee taken, an unexplained deposit, a payout that names a deposit that is not in the file, payouts without a reference that add up to a deposit, two different ways to make a deposit, a deposit in another currency). `tests/test_deposits.py` runs the engine over twelve seeds and checks that every seeded problem is found with the right kind, records and difference, that every record is explained once, that the result does not depend on the order of the input, and that counterparties are independent, which is what lets an incremental run reuse them. `tests/test_deposit_runs.py` takes the same files through storage, cases and the API, and puts a reference on payouts that were only proposed to see the case resolve by the data in an incremental run that is checked against matching everything again.

The demo has a second profile, "Demo: payouts and deposits", seeded by `python -m recon.demo`.

The setup screen has a choice of what to reconcile. For deposits and payouts the first file is the deposits and the second the payouts, and only the rules propose the mapping (`POST /api/setup/proposals` takes a `type`, and its `invoices` and `payments` parts are the first and second file whatever the type). `tests/test_setup_deposits.py` runs that flow.

Not covered: the language-model stages and the adjudication of ambiguous payments are for invoices and payments only; the mapping rules were checked on one layout of deposit and payout exports, not on styles I did not write; and a payout's reference is expected to hold one deposit id, so a bank memo that lists several payout ids is not read.

## Cases

Each exception becomes a case that is tracked across runs. A case follows a problem by the records it involves, so it keeps its notes and history while its amounts change. It closes by itself, as resolved, only when its records appear in a match in the data. I can report a fix, accept a variance with a reason and a note, reopen an accepted one, and add notes; I cannot mark a case resolved by hand. A reported fix is checked when new data arrives: if the problem is still there the case reopens with a flag. An accepted case comes back if the data behind it changes. Every transition is kept in an append-only log.

## API and dashboard

The API is under `/api`: profiles and periods, a per-period summary, the list of matches, the list of cases with filters, sorting and paging, one case with its records, history and notes, and the case actions (report fix, retract fix, accept, reopen, add note). Files are uploaded with a multipart request per source; when every source of a period is present the upload reconciles the period and updates the cases, inside the request or, with a job queue, as a job (see "Running reconciliations in a queue"). Case actions take the version of the case I was looking at and fail with a 409 `stale_case` if it has changed, and they accept an `Idempotency-Key` header so a retried request is applied once. Errors all have the shape `{"error": {"code", "message"}}`.

Setting up a reconciliation starts from two sample files. `POST /api/setup/proposals` takes an invoice file, a payment file and a period, profiles both, and returns the proposed mapping: for each field the column, whether the proposal is confident, the other columns that were possible, and the reasons; the same for the date format and the decimal separator; and how many fields still need an answer. `POST /api/setup/proposals/{id}/preview` shows how the files would be read with a mapping, with the first rows and the rows that were rejected. `POST /api/setup/proposals/{id}/confirm` takes the name and the final mapping, creates the profile, registers the two files for the period and reconciles it. A mapping that reads no records from a file is refused and nothing is created. Confirming again with the same name and mapping returns the profile that exists; confirming with a different one is a 409.

Who proposes the mapping is set by `RECON_MAPPER`. `auto` (the default) adds the language model to the rules when the provider's key is present or `RECON_LLM_REPLAY` names a file of recorded answers, and uses the rules alone otherwise, so the application runs without a key. `rules` never calls a model, and `hybrid` refuses to start without credentials or a replay file. `RECON_LLM_PROVIDER` chooses the provider: `gemini` (the default, `GEMINI_API_KEY` or `GOOGLE_API_KEY`), `anthropic` (`ANTHROPIC_API_KEY`) or `openai` (`OPENAI_API_KEY`), and `RECON_LLM_API_KEY` overrides whichever variable that provider reads. `openai` with `RECON_LLM_BASE_URL` reaches any server that speaks the OpenAI chat-completions protocol, such as a gateway or a local model server; it has no default model, so `RECON_LLM_MODEL` must name one. `RECON_LLM_MODEL` otherwise defaults to `gemini-3.1-flash-lite` or `claude-opus-5-5`. `RECON_LLM_EFFORT` and `RECON_LLM_FALLBACKS` apply to Anthropic only. All three providers are asked for a structured output that the schema constrains (`backend/src/recon/ai/llm/providers.py`), and a client waits and retries when it is rate limited. Every proposal says which model made it. The Gemini client has been called live; the Anthropic and OpenAI-compatible clients are tested only against stand-ins for their APIs.

`frontend/openapi.json` is the committed OpenAPI description. A backend test fails when it no longer matches the app, and the frontend types in `frontend/src/api/schema.ts` are generated from it (`npm run api:types`); CI fails if they are out of date.

The dashboard shows the share of invoices that reconciled, what is at stake by problem type and currency, and two tabs: cases that need attention, and invoices that reconciled. Every chart has a table view. "New reconciliation" (shown on its own when none exists yet) opens the setup: choose an invoice file, a payment file and a month, and the screen shows the proposed mapping field by field with the evidence for it. A field the data settles is marked "Confident" and anything else "Needs your answer"; the date order and an amount column the rules cannot tell apart stay open until the user picks one, and the create button stays disabled until nothing is open. Below the mapping, a table shows the first rows of each file as they will be read with the current choices, with the rows that were rejected, so a wrong choice shows up before anything is saved. A banner says whether the suggestions came from the rules alone or from the rules and a model. Opening a case shows the records involved, an explanation, the history and notes. The explanation is written by rules from the records (`backend/src/recon/cases/explain.py`) and says so. When a model is configured, "Explain with the model" adds a second, checked reading under it (see "Explaining a case with a model" in [ai.md](ai.md)).

## Retention

The worker deletes what is finished with every six hours (`recon/retention.py`): succeeded and failed jobs after 14 days, mapping proposals that were never confirmed (with their sample files) after 7, idempotency records after 7, and alert deliveries after 90 (`RECON_RETENTION_JOBS_DAYS`, `RECON_RETENTION_PROPOSALS_DAYS`, `RECON_RETENTION_IDEMPOTENCY_DAYS`, `RECON_RETENTION_DELIVERIES_DAYS`). A request repeated with an idempotency key older than that is applied again. Reconciliation results, cases, file versions and the log of ingested objects are history and are not deleted.

## Browser tests

The browser tests in `frontend/e2e` run against a server in demo mode that serves the built frontend (`RECON_STATIC_DIR=frontend/dist`) and has just been seeded with `python -m recon.demo`:

```bash
E2E_BASE_URL=http://127.0.0.1:8000 npx playwright test
```

`e2e/empty-start.e2e.ts` covers the other way in, with no sample data: against a server started with `RECON_DEMO=false` and an empty database, run `E2E_EMPTY=1 E2E_BASE_URL=http://127.0.0.1:8000 npx playwright test e2e/empty-start.e2e.ts`; it skips itself otherwise, so CI does not run it.

They change the data: one test loads the corrected export and another creates a reconciliation named "Spanish ERP and bank", so start from a freshly seeded server for each run. The sample files for the setup tests are in `frontend/e2e/fixtures`; they were written with `python -m recon.datagen --seed 5 --invoices 120 --first-days 12 --invoice-style spanish --payment-style bank_statement --no-corrections`. CI runs them against the compose stack.
