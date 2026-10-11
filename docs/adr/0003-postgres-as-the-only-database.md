# 0003. Postgres is the only database service

## Context

The system needs relational data (records, matches, cases), a durable job queue, scheduling state, and persisted state for the agent's workflow, which pauses to ask the user questions. Each extra service adds setup, failure modes and operational surface.

## Decision

Postgres holds all of these:

- domain tables, with constraints and migrations
- the job queue, using `SELECT ... FOR UPDATE SKIP LOCKED`, with retries, backoff and a dead-letter state
- schedules and per-source ingestion state
- agent workflow state that has to survive a restart while waiting for an answer

File contents live in S3-compatible object storage, not in the database (MinIO locally). Domain state, meaning matches, cases and their history, lives in domain tables. Workflow checkpoints record where the agent stopped and are not the source of truth for any domain fact.

## Consequences

- One database to run, back up and test against. Integration tests use a real Postgres.
- A job and the state change it produces can commit in one transaction.
- A Postgres-backed queue is slower than a dedicated broker. Reconciliation workloads are batch jobs, so the throughput is sufficient.
- If throughput ever becomes the limit, the queue sits behind an interface and can move to a broker.

## Alternatives

- Redis plus a task framework: more throughput, but a second stateful service. Pub/sub in particular drops messages when no consumer is connected, which is not acceptable for jobs.
