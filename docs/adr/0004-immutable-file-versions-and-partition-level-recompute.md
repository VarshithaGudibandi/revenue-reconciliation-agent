# 0004. Source files are immutable versions; changes recompute only affected partitions

## Context

Users fix discrepancies in their source systems and then deliver a new export. Re-running the whole reconciliation on every new file is slow and discards history. Editing the delivered files in place would break the audit trail.

## Decision

**Immutable versions.** Each object seen in the bucket becomes a file version identified by a SHA-256 hash that I compute. I don't use the S3 ETag as a content hash, because for multipart uploads it is not the MD5 of the content. The agent only reads from the bucket and never writes to it.

**Record ledger.** Each file version is normalized into records with a stable key per source, such as an invoice ID or transaction ID, and a content hash. Comparing a version with the previous version of the same period gives added, removed and changed records.

**Partitions.** Records are grouped into partitions, for example one customer or entity within one period. A changed record marks its partition dirty. Only dirty partitions are matched again, and every other partition keeps its existing result.

**Run identity.** A run is identified by its input file versions plus the configuration version. Repeating a run with the same identity yields the same result. A change to match rules or tolerances changes the configuration version and forces a full recompute.

**Equivalence property.** Incremental recompute must give exactly the same result as a full recompute. A property-based test applies random sequences of edits and asserts this.

## Consequences

- Corrected data costs time proportional to the change, not to the dataset.
- Matches that span records, such as many-to-one payout batches, are always recomputed within their whole partition, so they stay correct.
- Partition keys must be chosen so that no match crosses a partition boundary. A reconciliation type that can't satisfy that falls back to one partition per period.
- A "changed since last run" view is available, because the diff is computed anyway.

## Alternatives

- Record-level recompute: finer, but a freed record can enable a new match elsewhere, which needs a fixpoint loop. I'd only consider it if profiling shows partitions are too coarse.
- Full recompute each time: simplest, and the reference implementation for the equivalence test.
