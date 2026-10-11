# 0005. Exceptions close on evidence from the data

## Context

When a record fails to reconcile, I fix the problem in the source system and a new export arrives. If I also had to mark the exception resolved by hand, I would be maintaining the same fact in two places, and an exception could be marked resolved while the data still doesn't match.

## Decision

**Stable identity.** Each exception has a fingerprint built from the profile, the category and the keys of the records involved. A later run that finds the same problem updates the existing case, and never creates a second one.

**Statuses.**

- `open`: the data doesn't reconcile.
- `fix_reported`: I say I fixed it in the source and I'm waiting for the data.
- `resolved`: set only by the system, when a later file version reconciles the records. The case links to the file version that resolved it.
- `accepted`: I accept a difference that is real, such as a timing difference. It requires a reason code and a note, and it reopens if the underlying records change.

**Verification.** A case in `fix_reported` is checked on the next run. If it still doesn't reconcile, it returns to `open` with a flag that the fix was reported but the data still doesn't match.

**Notes and history.** A case holds free-text notes, and every status change is appended to an audit log.

## Consequences

- One action fixes a problem: correcting the source. There is no second system to keep in sync.
- Resolution can always be traced to the data that proved it.
- A hand-edited file can't silently clear an exception, because the changed-since-last-run view lists every changed record.
- Cases can't be closed by clicking a button, except by the explicit `accepted` route.

## Alternatives

- Manual resolve: easy to build, but it lets cases be closed that are still wrong.
