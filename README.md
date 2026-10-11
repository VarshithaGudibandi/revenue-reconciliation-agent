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

## License

MIT. See [LICENSE](LICENSE).
