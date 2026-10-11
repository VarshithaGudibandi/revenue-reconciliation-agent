# 0005. Exceptions close on evidence from the data

## Context

When a record fails to reconcile, I fix the problem in the source system and a new export arrives. If I also had to mark the exception resolved by hand, I would be maintaining the same fact in two places, and an exception could be marked resolved while the data still doesn't match.

## Decision

**A case follows a problem by the records it involves.** An exception is assigned to an existing case when they share records, not when they have the same category. I first planned to fingerprint a case from the profile, the category and the record keys, and that is wrong: an unpaid invoice that later receives a partial payment changes category, so the old case would close as resolved and a new one open, although the invoice is still unreconciled. With overlap, the case keeps its identity, notes and history while its kind, amounts and records change.

Each exception takes one case and each case one exception, in a fixed order. An active case is preferred over a closed one, so a problem that comes back reopens its old case. Then the case sharing the most records wins, then the oldest, by a creation number. A case that was merged into another is not reused.

**Statuses.**

- `open`: the data doesn't reconcile.
- `fix_reported`: I say I fixed it in the source and I'm waiting for the data.
- `resolved`: set only by the system, when records of the case now appear in a match. The case names the run that resolved it, and through the run the file versions.
- `accepted`: I accept a difference that is real. It needs a reason (timing difference, rounding difference, write-off, known data issue, other) and a note.
- `withdrawn`: the case no longer applies without having been reconciled, either because its records are gone from the data or because it was merged into another case. It claims nothing.

Check constraints tie each status to the fields that justify it, so the database refuses, for example, a resolved case with no resolving run.

**Cases are brought up to date from the run for the current files.** The step records which run the cases reflect, so running it again changes nothing. A run for older files that finishes late cannot roll the cases back, and a file reverted to earlier content brings back the cases that content had. It does nothing until the run for the current files has finished.

**Verification.** A case in `fix_reported` is checked when the cases are next brought up to date with new data. If the records reconcile it is resolved. If the problem is still there it returns to `open` with a flag that the fix was reported but the data still doesn't match.

**Accepting.** An accepted case stores a signature of its kind, variance and the content of its records. It stays accepted while the signature is unchanged, comes back to `open` if the data changes, and becomes `resolved` if the data reconciles.

**Actions and concurrency.** A person can report a fix, retract it, accept a variance, reopen an accepted case and add notes. They cannot resolve a case. Each action locks the case, takes the version the caller last saw and refuses a newer one, and an action whose result already holds is a no-op, so a retry is safe. The version moves only when something a person could have seen changed.

**Audit.** Every transition is an event with the run it happened under. The database refuses to update, delete or truncate events.

## Consequences

- One action fixes a problem: correcting the source. There is no second system to keep in sync.
- Resolution can always be traced to the data that proved it.
- A hand-edited file can't silently clear an exception, because the records that changed show up in the diff between versions.
- Cases can't be closed by clicking a button, except by the explicit `accepted` route, which the data overrides.
- Assignment is greedy and depends on the order of the exceptions, which is fixed by the engine's output order. A different but equally reasonable choice could be made when two cases tie, and when several exceptions overlap one case only the first keeps it.
- When a fixed ambiguous payment leaves look-alike invoices unpaid, one of them continues the old case and the others become new cases. Going back to the old file folds them together again.
- Cases are kept per profile and period. A problem that moves to another period is a new case.

## Alternatives

- Fingerprint from profile, category and record keys: simple, but it reports false resolutions when a problem changes category.
- Manual resolve: easy to build, but it lets cases be closed that are still wrong.
