# 0007. The job queue is a table in Postgres

## Context

Reconciling a large period, scanning a bucket, sending alerts and cleaning up are work that a request should not wait for, and that must survive a crash. Postgres is already the only database (ADR 0003). A broker would be a second system to run, back up and keep consistent with it.

## Decision

**A `job` table, claimed with `FOR UPDATE SKIP LOCKED`.** A worker takes one due job, which counts as an attempt, and holds it under a lease that a second thread extends while the job runs. A job whose lease runs out is put back by the next worker to look, or failed if it has used its attempts. Only the holder of the lease can finish a job, so a worker that was cut off drops its result.

**Retries wait.** 5 seconds doubling to 5 minutes, with up to a fifth added at random. An error the handler marks permanent fails at once.

**Same work, one job.** Two requests for the same thing while it waits are one job. A request made while it runs is a new job, because the running one may already have read its input, and the new one does not start until the old one has finished. I first considered one job per key across running and waiting; that loses a file uploaded while a run is reading the previous ones.

**Inline stays the default.** With `RECON_JOBS=inline` the work runs in the request, so the project runs with no worker. The compose file sets `queue`.

**The database refuses nonsense.** Check constraints: a running job has a lease, a succeeded job has a result, a failed one has an error and attempts stay within the maximum.

## Consequences

- Workers need clocks that agree to well within the lease; times come from the caller.
- A handler cannot be cancelled. A worker that lost a job still runs it to the end and then finds out.
- Throughput is what one table and its indexes give. There is one queue and no priorities. If that stopped being enough, the interface (`recon/jobs/queue.py`) is small enough to put a broker behind.
- Finished jobs accumulate until retention deletes them.
