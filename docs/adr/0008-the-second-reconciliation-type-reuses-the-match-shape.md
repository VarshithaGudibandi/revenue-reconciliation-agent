# 0008. Deposits against payouts reuse the shape of invoices against payments

## Context

The second type of reconciliation is bank deposits against processor payouts. A deposit usually carries several payouts, so it is many to one. The first type is one to many: an invoice and the payments that settle it.

## Decision

**Put the deposit where the invoice is.** The deposit is the one record and its payouts are the several that settle it, so a stored match, a case, the incremental run and the record roles keep their shape. The stored role names stay `invoice` and `payment` for both types, and a type (`recon/profiles.py`) says what a person is shown: Deposit and Payout. Renaming the roles in the schema, the API and the cases would have touched every table that has them for no behaviour.

**Share the reference pass, write a new grouping pass.** A payout that names a deposit is the same relation as a payment that names an invoice, so that pass is shared with the classifier and the ambiguity builder passed in. What is new is the case with no reference at all: a bounded search for sets of payouts that add up to a deposit. A single set no other deposit could use is proposed and never applied, because amounts adding up is not a reference.

**A ten-day window, not 45.** Sets found by amount alone are only evidence when few payouts are candidates. With a month of unreferenced payouts, unrelated sets hit the same total by chance; one seed of generated data was ambiguous where the groups were distinct until I narrowed the window. A payout with a reference is not subject to it.

**Say ambiguous when the search gives up.** The search has a step budget, and running out of it is reported as an ambiguous grouping, not as nothing found.

## Consequences

- An exception kind that means something for deposits is stored as text like any other, so the new kinds needed no migration.
- The names `invoice` and `payment` in the code and in the stored data mean "the one" and "the several" for the second type. That is a lasting piece of confusion that the API labels hide from users but not from someone reading the code.
- The language-model stages, adjudication and mapping evaluation are for invoices and payments. Deposits and payouts get the rules' mapping, checked on one layout I wrote.
- A payout that reaches the bank more than ten days after it was paid out, with no reference, is reported as undeposited and its deposit as having no payouts.
