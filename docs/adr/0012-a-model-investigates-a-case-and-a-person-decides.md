# 0012. A model investigates a case, and a person decides

## Context

The engine reports what is wrong with a case and the difference. Why it happened (a fee taken out of a payment, an invoice paid by a parent company, two invoices paid in one transfer) is what a person spends their time on, and finding out means looking at the rest of the month. That is open-ended reading, which is where a model can help, but its answer can change a case, which is where a wrong answer costs something.

## Decision

**A LangGraph run with a stop for a person.** The model reads the case under aliases and looks around the month with four read-only tools (a customer's records, records by amount, open invoices that add up to a total, a calculator). Its finding is checked by a program: every record it cites must have been shown by a tool, every figure above 100 must have been returned by one, and the action must suit the kind of case. A finding that fails is not shown. One that passes waits for a person. LangGraph is used for that wait: it keeps the run's state in Postgres after each step, so the decision can come in a later request or after a restart and resumes the run without asking the model again. A loop of my own on the job queue could do the same; LangGraph's checkpoints and interrupts are the part that would otherwise have to be written.

**Approval acts through the person's own actions.** Approving adds the finding to the case's notes, and for a cost the business bears it accepts the variance. Both go through the case actions a person uses, with their locks and version checks. The model never resolves a case: that stays with the data (ADR 0005).

**Postgres stays the only database.** LangGraph creates and migrates its own tables; they live in a `langgraph` schema created by a migration, apart from the tables the migrations own (ADR 0003).

**Measured against ground truth.** The generator can write the cause behind each problem, so findings are scored on their cause and on whether they found the other records involved. On the first 26 of 30 cases with `gemini-3.1-flash-lite`, the cause was right for 25 and the other records were found for all 26; every finding passed the checks.

## Consequences

- Rules written for the generator get the cause right 99% of the time, because the generator follows formulas I wrote. The model's case is that it gets close without anyone writing those formulas, and finds and cites the evidence; it is not that it beats rules that know the answer.
- A small invented amount (100 or less) passes the figure check, because percentages and days are written the same way.
- Deposits and payouts, and the other kinds of case, are not investigated yet.
- The investigation runs inside the request; with a slow provider that is a wait of seconds to a minute.
