# Revenue Reconciliation Agent

I'm building an agent that reconciles revenue data across financial systems, for example invoices against payments, or processor payouts against bank deposits. It finds the records that don't match and explains each difference: how large it is, where it probably comes from, and what to check next.

Two principles shape the design:

- **The engine does the matching and the arithmetic.** An LLM never computes a sum or decides a match on its own. It proposes column mappings, reads reference documents, judges ambiguous candidates, and writes the explanations.
- **The data decides when an issue is resolved.** Source files are never edited. When a corrected file arrives, the affected records are matched again, and an exception closes only when the new data reconciles.

## Run locally

```bash
docker compose up --build
```

This starts Postgres, runs the migrations, loads a demo month of generated invoices and payments, and serves the API and the dashboard at <http://127.0.0.1:8000>. The demo has 400 invoices, about one in ten of which does not reconcile. The "Load corrected export" button in the demo banner loads a second payments file in which some of the problems are fixed at the source, so I can watch cases close and the ones that were not fixed stay open.

The demo endpoints and the banner exist only when `RECON_DEMO=true`, which the compose file sets.

### Backend checks

```bash
docker compose up -d postgres
cd backend
export RECON_DATABASE_URL=postgresql+psycopg://recon:recon@localhost:5432/recon
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

The tests create and drop a scratch database for each test that needs one, so the database user must be allowed to create databases.

### Frontend

```bash
cd frontend
npm ci
npm run dev          # Vite on :5173, proxying /api to a backend on :8000
npm run lint && npm run typecheck && npm test && npm run build
```

The browser tests in `frontend/e2e` run against a server in demo mode that serves the built frontend (`RECON_STATIC_DIR=frontend/dist`) and has just been seeded with `python -m recon.demo`:

```bash
E2E_BASE_URL=http://127.0.0.1:8000 npx playwright test
```

They change the demo data (they load the corrected export), so seed again before running them a second time. CI runs them against the compose stack.

## How a run works

A profile holds the column mappings for each source file and the matching thresholds. Registering a file stores its content as a new version, with a snapshot of its normalized records; rows that cannot be normalized are kept with the reason. A run reads the latest version of each file for a period.

When an earlier run exists with the same configuration, the new run compares the records of the two versions and matches again only the customers whose records changed, reusing the earlier result for the rest. Matching every customer again gives the same result.

## Cases

Each exception becomes a case that is tracked across runs. A case follows a problem by the records it involves, so it keeps its notes and history while its amounts change. It closes by itself, as resolved, only when its records appear in a match in the data. I can report a fix, accept a variance with a reason and a note, reopen an accepted one, and add notes; I cannot mark a case resolved by hand. A reported fix is checked when new data arrives: if the problem is still there the case reopens with a flag. An accepted case comes back if the data behind it changes. Every transition is kept in an append-only log.

## API and dashboard

The API is under `/api`: profiles and periods, a per-period summary, the list of matches, the list of cases with filters, sorting and paging, one case with its records, history and notes, and the case actions (report fix, retract fix, accept, reopen, add note). Files are uploaded with a multipart request per source; when every source of a period is present the upload runs the reconciliation and updates the cases. Case actions take the version of the case I was looking at and fail with a 409 `stale_case` if it has changed, and they accept an `Idempotency-Key` header so a retried request is applied once. Errors all have the shape `{"error": {"code", "message"}}`.

Setting up a reconciliation starts from two sample files. `POST /api/setup/proposals` takes an invoice file, a payment file and a period, profiles both, and returns the proposed mapping: for each field the column, whether the proposal is confident, the other columns that were possible, and the reasons; the same for the date format and the decimal separator; and how many fields still need an answer. `POST /api/setup/proposals/{id}/preview` shows how the files would be read with a mapping, with the first rows and the rows that were rejected. `POST /api/setup/proposals/{id}/confirm` takes the name and the final mapping, creates the profile, registers the two files for the period and reconciles it. A mapping that reads no records from a file is refused and nothing is created. Confirming again with the same name and mapping returns the profile that exists; confirming with a different one is a 409.

Who proposes the mapping is set by `RECON_MAPPER`. `auto` (the default) adds the language model to the rules when `ANTHROPIC_API_KEY` is present or `RECON_LLM_REPLAY` names a file of recorded answers, and uses the rules alone otherwise, so the application runs without a key. `rules` never calls a model, and `hybrid` refuses to start without credentials or a replay file. `RECON_LLM_MODEL`, `RECON_LLM_EFFORT` and `RECON_LLM_FALLBACKS` set the model, its effort and whether server-side refusal fallbacks are requested. Every proposal says which of them made it.

`frontend/openapi.json` is the committed OpenAPI description. A backend test fails when it no longer matches the app, and the frontend types in `frontend/src/api/schema.ts` are generated from it (`npm run api:types`); CI fails if they are out of date.

The dashboard shows the share of invoices that reconciled, what is at stake by problem type and currency, and two tabs: cases that need attention, and invoices that reconciled. Every chart has a table view. Opening a case shows the records involved, an explanation, the history and notes. The explanations are written by rules from the records (`backend/src/recon/api/explain.py`); no language model is involved yet, and the interface says so.

## Column mapping

A profile of a source file (`backend/src/recon/mapping/profile.py`) holds facts about each column that are computed locally: how many values read as a date in each known format, how many read as an amount with a decimal point or comma, how many are currency codes, how unique the values are, and what shapes they take (`INV-000123` becomes `A+-9+`). `FileProfile.describe()` returns the profile without any cell value.

The baseline mapper (`mapping/baseline.py`) uses no language model. It decides from the data first and from header words last:

- the amount column is the one whose values equal the other file's amounts, which separates a gross amount from a net amount, a tax amount or a fee;
- the invoice id and the payment reference are found together, because payments cite invoice ids;
- the customer columns are the pair that share their values across the two files;
- among several date columns, the issue date is the one payments fall after, not before;
- a date that fits both day-month and month-day is returned as a question, not as a guess.

Every field is either confident or comes with the alternatives, so a user can be asked. `python -m recon.evaluation.mapping_eval` runs the mapper over every pair of export styles and reports, per field, whether it was correct, asked (the right answer was among the options), missed, or confidently wrong. The last is the one that matters. A second variant keeps only invoices from the first 12 days of the month, where the date order cannot be read from the values and asking is the right answer.

Three groups of export styles are reported separately: development styles (the baseline was built around them), held-out styles (`xero_like`, `stripe_like`, written first and never tuned against) and stress styles (Italian, Dutch and French vocabularies the baseline's word lists do not cover, and `generic_columns`, whose headers are `Column A`, `Column B` and so on). `--mode single` maps each file on its own, as when the first of two files is uploaded, so there is no other file to cross-check against.

Files by outcome, 3 seeds of 120 invoices, full month and first-12-days variants (`python -m recon.evaluation.mapping_eval --seeds 3 --mode paired|single`):

| | development | held out | stress |
|---|---|---|---|
| both files together: exact / needs the user | 89% / 11% | 77% / 23% | 47% / 53% |
| each file alone: exact / needs the user | 72% / 28% | 75% / 25% | 30% / 70% |

No file in any group was confidently wrong, and no field was missed (a field is missed when the right answer is not among the options offered). Everything not exact is a question for the user. I wrote the generator, the styles and the baseline, so these numbers show how the baseline behaves on my data, not on real exports; the stress column is the part that says where header words run out.

### Mapping with a language model

`mapping/llm.py` asks Claude to map one file at a time and then checks the answer against the data. The model receives the role of the file (invoices or payments) and a description of each column: its header, the shapes of its values, the date formats and decimal separators its values read under, how unique it is, and up to three examples. Short all-capital codes in a column with few distinct values (`EUR`, `OPEN`) are shown as they are. Every other example is masked: digits after the first of each run and letters after the first of each run longer than three become `*`, so `INV-000123` is sent as `INV-0*****` and `1500.00` as `1***.0*`. Cell values are not sent in any other form. A test checks that none of the first rows of a generated export appears in the description.

The answer is a structured output in which every column field can only be one of the file's own headers or `(none)`. The engine then downgrades any answer the data contradicts: a column whose values do not fit the field, a column used for two fields, a decimal separator the amounts do not read with, and a date order that the values leave open. A date order is never confident unless the values settle it, whatever the model says. `--mapper llm-unchecked` shows what the model says without these checks, so the effect of the checks can be measured.

`python -m recon.evaluation.mapping_eval --mapper llm` runs the same evaluation as the baseline, with the model mapping each file on its own. It needs `ANTHROPIC_API_KEY`. `--record FILE` saves each answer to a file, and `--replay FILE` serves those answers without the network or a key, so the evaluation can be repeated by someone who cannot call the API; it cannot answer a question that was not recorded. The default model is `claude-opus-5-5` at low effort, with server-side refusal fallbacks switched on. Forced tool use is not available on that model, which is why the answer is a structured output. The command prints the number of calls, the tokens, and an estimated cost at list price.

`--mapper hybrid` combines the rules (mapped with both files in sight) with the model: where they agree the answer is confident, a confident rule is not overruled by the model, a model that is sure where the rules are not is followed as a suggestion that is still a question, and two confident answers that disagree are a conflict left to the user.

No answers are recorded in this repository yet, so there are no measured results for the model. The unit tests use a fake model, a stand-in for the API client and a recorded file written by the tests.

## Synthetic data

The repository is public, so it only uses generated data. The generator writes an invoice export and a payment export with a known set of problems, a ground-truth file, and a second revision in which some of the problems are fixed at the source:

```bash
cd backend
uv run python -m recon.datagen --out /tmp/recon-data --seed 42
```

Real exports differ in header text, language, date format and number format, and they carry columns that look like the one you want. `--invoice-style` and `--payment-style` write the same data as one of several export styles, for example a German ERP export with `Bruttobetrag` and a decimal comma, a SAP-style export with `BELNR` and `BUDAT`, or a payment processor export with timestamps and a fee column next to the amount. Each run also writes `mapping_truth.json` with the correct column mapping, the lookalike columns that must not be mapped, and whether the date order can be told from the values at all. Two styles are held out, modelled on publicly described layouts of an accounting product and a payment processor (`xero_like`, `stripe_like`); I wrote them before the baseline mapper and don't tune against them. Their headers come from secondhand descriptions, not from an official export. A test writes every style, reads it back with its true mapping and checks that the records equal the generated ones.

```bash
uv run python -m recon.datagen --out /tmp/recon-data --invoice-style german --payment-style processor
```

## License

MIT. See [LICENSE](LICENSE).
