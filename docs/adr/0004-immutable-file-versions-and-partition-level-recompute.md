# 0004. Source files are immutable versions; changes recompute only affected partitions

## Context

Users fix discrepancies in their source systems and then deliver a new export. Re-running the whole reconciliation on every new file is slow and discards history. Editing the delivered files in place would break the audit trail.

## Decision

**Immutable versions.** Each distinct state of a source file for a period is a file version, with a SHA-256 hash of its content that I compute. I don't use the S3 ETag as a content hash, because for multipart uploads it is not the MD5 of the content. The agent only reads from the bucket and never writes to it. Versions form a history: registering content equal to the latest version changes nothing, and content that matches an older version becomes a new version, so the latest version is always what the source currently holds.

**Record ledger.** Each file version is normalized into records with a stable key per source, such as an invoice ID or transaction ID, and a content hash. Comparing a version with the previous version of the same period gives added, removed and changed records.

**Partitions.** Records are grouped into partitions, for example one customer or entity within one period. A changed record marks its partition dirty. Only dirty partitions are matched again, and every other partition keeps its existing result.

**Run identity.** A run is identified by the content hashes of its input files plus a hash of the configuration. Asking again for the same contents returns the same run, so a file reverted to earlier content reuses the earlier run. A change to match rules or tolerances changes the configuration hash, so no earlier run is a valid base and everything is matched again.

**Equivalence property.** Incremental recompute must give exactly the same result as a full recompute. Two property tests back this, with no database: matching is additive over counterparties, and incremental recompute equals a full recompute after random removals, changes and additions, including records that move between counterparties. A run can also verify itself by matching everything and failing if the two results differ.

**What is saved.** The matching work is proportional to the changed partitions. Copying the unchanged results into the new run is still linear in the size of the result, because each run stores its own result rows.

## Consequences

- Matching cost is proportional to the change. Reading the fingerprints of both versions and copying the unchanged results are still linear in the dataset, which is far cheaper than matching.
- Matches that span records, such as many-to-one payout batches, are always recomputed within their whole partition, so they stay correct.
- Partition keys must be chosen so that no match crosses a partition boundary. For invoices against payments the partition is the customer, and a reference that points at another customer's invoice is not a match. A reconciliation type that can't satisfy that falls back to one partition per period.
- The diff is computed anyway, so a "changed since last run" view needs no extra computation.

## Alternatives

- Record-level recompute: finer, but a freed record can enable a new match elsewhere, which needs a fixpoint loop. I'd only consider it if profiling shows partitions are too coarse.
- Full recompute each time: simplest, and the reference implementation for the equivalence test.
