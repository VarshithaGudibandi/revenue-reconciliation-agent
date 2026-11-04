# Revenue Reconciliation Agent

Finance teams spend days at month end working out why the money doesn't tie: which invoices were never paid, which payments match nothing, and why a payment came in short. I built a system that does that work on the exports finance systems already produce. It matches invoices against payments (and bank deposits against processor payouts), turns every difference into a tracked case, and uses a language model where judgement is needed: reading unfamiliar files, reading what a payer wrote, explaining a case, and investigating why it happened. A person approves anything the model proposes, and a case closes only when new data reconciles it.

![How the parts fit together](docs/architecture-map.svg)

Teal is deterministic code, amber is the language model, grey is stored data. `docs/architecture.md` explains the picture.

## Where the AI is, and what checks it

The matching engine and all arithmetic are plain code. Deciding that money matches is never left to a model. The model does the work that needs reading and judgement, and every answer it gives is checked by a program before anyone sees it.

| Stage | What the model does | What checks it | Measured (Gemini 3.1 Flash-Lite, generated data) |
|---|---|---|---|
| **Column mapping** | Reads an unfamiliar export (any language, or headers like `Column A`) and says which column is the invoice id, amount, date and so on | Every choice must fit the column's values. A date order the values leave open is always a question | With the rules: 92% / 83% / 70% of files read exactly (development / held-out / stress styles), against 89% / 77% / 47% for the rules alone. None confidently wrong |
| **Ambiguous payments** | Reads the payer's memo and says which of several look-alike invoices it was meant for | It must quote the memo. A quote that isn't there downgrades the answer to a suggestion | 42 of 44 answerable cases settled, 0 wrong. Rules alone settle 27 |
| **Explanations** | Writes a plain-language reading of a case | Every record it cites, figure it states and id it writes must be the engine's | 46 of 46 passed the checks, on simple cases |
| **Investigation** (LangGraph) | Looks around the month with read-only tools and proposes why the case happened, the evidence, and an action | The evidence and figures must come from its tools. Then **a person approves or rejects** | Right cause in 25 of the first 26 cases, with the other records involved found in all 26 |

A fifth experiment lost and is documented as such. A model with tools mapping both files at once did not beat the rules with a single-file model, and it was confidently wrong 3 times where they never were ([ADR 0011](docs/adr/0011-a-mapping-agent-with-tools-was-measured-and-is-not-the-default.md)).

**What the numbers do and don't mean.** All of them come from generated data, and I wrote the generator, the export styles and the rules. They show how the system behaves on data with known answers, not on a real company's exports. Rules written knowing the generator's formulas find the investigation causes 99% of the time; the model's case is that it gets close without anyone writing those rules. It finds and cites the evidence, and its write-up is ready for a person to approve. Every model answer is recorded in `backend/cassettes/`, so each table can be replayed without a key, and tests fail if a replay stops giving these numbers. The details, including the baselines and what is not measured, are in [docs/ai.md](docs/ai.md).

## Try it

You need Docker with Compose. No account, key or data of your own.

```bash
git clone https://github.com/VarshithaGudibandi/revenue-reconciliation-agent
cd revenue-reconciliation-agent
docker compose up --build
```

Open <http://127.0.0.1:8000> once the backend says it is ready (the first build takes a few minutes). It loads two generated sample reconciliations: *invoices and payments*, and *payouts and deposits*. Things to try:

- **Open a case** to see its records, a plain-language reading, and its history. Report a fix, add a note, or accept the difference with a reason. You can't mark a case resolved; only data does that.
- **Close the loop.** Report fixes on a few invoice cases, then press "Load corrected export". Only the customers whose records changed are matched again, and the fixed cases close by themselves.
- **Set up from files.** "New reconciliation" takes two CSV exports, shows how it read each column and why, and asks only what the data can't settle. Sample files are in `frontend/e2e/fixtures`.
- **Alerts.** Add a Slack, Teams or email channel and send a test.

Without a key this runs on rules only, and nothing leaves your machine. To turn on the model stages, put a free Gemini key from Google AI Studio in a `.env` file (`GEMINI_API_KEY=...`) and restart. "Explain with the model", "Ask the model" and "Investigate with the model" then appear in the case view. Stop everything with `docker compose down` (add `-v` to delete the data).

## Use your own data

1. **Start empty:** run `RECON_DEMO=false docker compose up -d --build`, then choose what to reconcile, upload one export of each kind for a month, check the columns, and confirm. "Add a file" takes the next export, and only what changed is matched again.
2. **Or drop exports in an S3-compatible bucket**, named `<profile>/<YYYY-MM>/<source>.csv`, and a worker picks them up. Set the `RECON_S3_*` values in `.env` and run `docker compose --profile s3 up -d --build`.
3. **Add a model:** Gemini by default, or set `RECON_LLM_PROVIDER` to `anthropic`, or to `openai`, which also reaches any OpenAI-compatible server through `RECON_LLM_BASE_URL`. Use that provider's usual key variable. The model is sent masked column samples for mapping, and the memo and candidate invoices for a suggestion. For explanations and investigations it gets amounts, dates and memo text under aliases (`R1`, `I1`, `C1`), never real ids or customer ids. **On Google's free tier, what you send may be used to improve Google's products and read by reviewers**, so use it with sample data only.
4. **Alerts:** add a channel on the Alerts page. Email needs `RECON_SMTP_*`.

There is no live connection to an accounting system, a processor or a bank: what this reads is the CSV export they produce. The optional settings (keys, a bucket, a mail server, starting empty) are explained in `.env.example`; the rest are in `backend/src/recon/config.py`.

## How it works, briefly

- **Files are never edited.** Each upload is stored as a new version. A run reads the latest version of each file and matches again only the customers whose records changed ([ADR 0004](docs/adr/0004-immutable-file-versions-and-partition-level-recompute.md)).
- **Cases close on evidence.** Each difference becomes a case that follows its records across runs, with an append-only history. A person can report a fix, accept a difference with a reason, or add a note; only new data resolves a case ([ADR 0005](docs/adr/0005-evidence-based-exception-resolution.md)).
- **Money is integers.** Amounts are stored and compared in minor units, never floats ([ADR 0001](docs/adr/0001-money-as-integer-minor-units.md)).
- **One database.** Postgres holds the records, runs, cases, a job queue (leases, retries, fencing), alert state, and paused investigations ([ADR 0003](docs/adr/0003-postgres-as-the-only-database.md), [0007](docs/adr/0007-a-job-queue-in-postgres.md)).
- **Any model provider.** One small interface with three clients (Gemini, Anthropic, OpenAI-compatible), structured output and tool calling, and retries on rate limits. A used-up daily quota fails at once: in the app the rules stand, and an evaluation stops instead of scoring a cut-off run ([ADR 0010](docs/adr/0010-any-model-provider-gemini-by-default.md)).

How the queue, bucket ingestion, alerts, the deposits engine, the API and retention work is in [docs/operations.md](docs/operations.md).

## Where things are

| | |
|---|---|
| `backend/src/recon/engine` | the matching engines, pure functions |
| `backend/src/recon/ai` | everything that uses a model: `llm` (providers, tools, recordings), `mapping`, `adjudication`, `explanation`, `investigation` |
| `backend/src/recon/cases` | a case's life: assignment, sync with each run, actions, the rule-written reading |
| `backend/src/recon/files` | reading exports, versions, the bucket scan |
| `backend/src/recon/jobs`, `alerts`, `persistence`, `api` | the queue and worker, alerts, the database layer, the HTTP API |
| `backend/src/recon/datagen`, `evaluation` | generated data with known answers, and the evaluations scored against it |
| `backend/cassettes` | every recorded model answer behind the published numbers |
| `frontend/src` | the React dashboard |
| `docs` | architecture, the model stages, operations, and 12 decision records |

## Development

```bash
docker compose up -d postgres
cd backend && uv sync
export RECON_DATABASE_URL=postgresql+psycopg://recon:recon@localhost:5432/recon
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest

cd ../frontend && npm ci
npm run lint && npm run typecheck && npm test && npm run build
```

The backend tests create a scratch database per test, so the database user needs permission to create databases. No test calls a real model: they use scripted models and the recordings. The browser tests (`frontend/e2e`, Playwright) run in CI against the full compose stack; see [docs/operations.md](docs/operations.md) for running them locally. The evaluations are under `python -m recon.evaluation.*`, each with `--replay` to rerun from the recordings.

## Not done yet

- **Real exports.** Every number here is on generated data.
- **A stronger model.** Only Gemini 3.1 Flash-Lite on the free tier has been measured, because Flash's free tier allows 20 requests a day. The Anthropic and OpenAI-compatible clients are tested only against stand-ins for their APIs.
- **Investigating more kinds of case,** and deposits and payouts with the model stages.
- **Login and permissions.** It's a single-user tool meant to run on your own machine.
- **Smaller gaps:** the Teams alert format was never sent to a real Teams tenant, webhook URLs are stored unencrypted, and the investigation panel is tested in component and API tests but hasn't been tried with a live model in a browser.

## License

MIT. See [LICENSE](LICENSE).
