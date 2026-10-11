# Revenue Reconciliation Agent

I'm building an agent that reconciles revenue data across financial systems, for example invoices against payments, or processor payouts against bank deposits. It finds the records that don't match and explains each difference: how large it is, where it probably comes from, and what to check next.

Two principles shape the design:

- **The engine does the matching and the arithmetic.** An LLM never computes a sum or decides a match on its own. It proposes column mappings, reads reference documents, judges ambiguous candidates, and writes the explanations.
- **The data decides when an issue is resolved.** Source files are never edited. When a corrected file arrives, the affected records are matched again, and an exception closes only when the new data reconciles.

## Run locally

Start Postgres, run the migrations and start the API:

```bash
docker compose up --build
curl http://127.0.0.1:8000/health/ready
```

Run the backend checks against the compose Postgres:

```bash
docker compose up -d postgres
cd backend
export RECON_DATABASE_URL=postgresql+psycopg://recon:recon@localhost:5432/recon
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

The tests create and drop a scratch database for each test that needs one, so the database user must be allowed to create databases.

## How a run works

A profile holds the column mappings for each source file and the matching thresholds. Registering a file stores its content as a new version, with a snapshot of its normalized records; rows that cannot be normalized are kept with the reason. A run reads the latest version of each file for a period.

When an earlier run exists with the same configuration, the new run compares the records of the two versions and matches again only the customers whose records changed, reusing the earlier result for the rest. Matching every customer again gives the same result.

## Synthetic data

The repository is public, so it only uses generated data. The generator writes an invoice export and a payment export with a known set of problems, a ground-truth file, and a second revision in which some of the problems are fixed at the source:

```bash
cd backend
uv run python -m recon.datagen --out /tmp/recon-data --seed 42
```

## License

MIT. See [LICENSE](LICENSE).
