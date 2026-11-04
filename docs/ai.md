# The model stages and how they were measured

Every place a language model is used, what it is sent, what checks its answer, and what was measured. All the numbers come from generated data (see the last section). The model's answers were recorded in `backend/cassettes/`, so `--replay` reproduces each model row without a key, and `tests/test_recorded_evaluations.py` fails if the adjudication, explanation, mapping-agent or investigation recordings stop giving the numbers below.

## Column mapping

A profile of a source file (`backend/src/recon/ai/mapping/profile.py`) holds facts about each column that are computed locally: how many values read as a date in each known format, how many read as an amount with a decimal point or comma, how many are currency codes, how unique the values are, and what shapes they take (`INV-000123` becomes `A+-9+`). `FileProfile.describe()` returns the profile without any cell value.

The baseline mapper (`ai/mapping/baseline.py`) uses no language model. It decides from the data first and from header words last:

- the amount column is the one whose values equal the other file's amounts, which separates a gross amount from a net amount, a tax amount or a fee;
- the invoice id and the payment reference are found together, because payments cite invoice ids;
- the customer columns are the pair that share their values across the two files;
- among several date columns, the issue date is the one payments fall after, not before;
- a column whose values are mostly several words is the optional free text (what an invoice is for, what a payer wrote), and such a column is never taken for a payment reference;
- a date that fits both day-month and month-day is returned as a question, not as a guess.

Every field is either confident or comes with the alternatives, so a user can be asked. `python -m recon.evaluation.mapping_eval` runs the mapper over every pair of export styles and reports, per field, whether it was correct, asked (the right answer was among the options), missed, or confidently wrong. The last is the one that matters. A second variant keeps only invoices from the first 12 days of the month, where the date order cannot be read from the values and asking is the right answer.

Three groups of export styles are reported separately: development styles (the baseline was built around them), held-out styles (`xero_like`, `stripe_like`, written first and never tuned against) and stress styles (Italian, Dutch and French vocabularies the baseline's word lists do not cover, and `generic_columns`, whose headers are `Column A`, `Column B` and so on). `--mode single` maps each file on its own, as when the first of two files is uploaded, so there is no other file to cross-check against.

Files by outcome, 3 seeds of 120 invoices, full month and first-12-days variants (`python -m recon.evaluation.mapping_eval --seeds 3 --mode paired|single`):

| | development | held out | stress |
|---|---|---|---|
| both files together: exact / needs the user | 89% / 11% | 77% / 23% | 47% / 53% |
| each file alone: exact / needs the user | 72% / 28% | 62% / 38% | 28% / 72% |

The held-out figure for a file alone fell from 75% to 62% when the styles gained a free-text column: in `xero_like` a column of sentences is a second candidate for the customer, and its headers (`*ContactName`) are not in the baseline's word lists, which I did not extend because the style is held out. No file in any group was confidently wrong, and no field was missed (a field is missed when the right answer is not among the options offered). Everything not exact is a question for the user. I wrote the generator, the styles and the baseline, so these numbers show how the baseline behaves on my data, not on real exports; the stress column is the part that says where header words run out.

### Mapping with a language model

`ai/mapping/llm.py` asks the model to map one file at a time and then checks the answer against the data. The model receives the role of the file (invoices or payments) and a description of each column: its header, the shapes of its values, the date formats and decimal separators its values read under, how unique it is, and up to three examples. Short all-capital codes in a column with few distinct values (`EUR`, `OPEN`) are shown as they are. Every other example is masked: digits after the first of each run and letters after the first of each run longer than three become `*`, so `INV-000123` is sent as `INV-0*****` and `1500.00` as `1***.0*`. Cell values are not sent in any other form. A test checks that none of the first rows of a generated export appears in the description.

The answer is a structured output in which every column field can only be one of the file's own headers or `(none)`. The engine then downgrades any answer the data contradicts: a column whose values do not fit the field, a column used for two fields, a decimal separator the amounts do not read with, and a date order that the values leave open. A date order is never confident unless the values settle it, whatever the model says. `--mapper llm-unchecked` shows what the model says without these checks, so the effect of the checks can be measured.

`python -m recon.evaluation.mapping_eval --mapper llm` runs the same evaluation as the baseline, with the model mapping each file on its own. It needs a key for the chosen provider (`--provider`, `--model`). `--record FILE` saves each answer to a file, and `--replay FILE` serves those answers without the network or a key, so the evaluation can be repeated by someone who cannot call the API; it cannot answer a question that was not recorded. A run stops, rather than scoring the rest as unanswered, when the provider's quota for the day is used up; the answers recorded so far are kept and the next run carries on from them. The command prints the number of calls and the tokens.

`--mapper hybrid` combines the rules (mapped with both files in sight) with the model: where they agree the answer is confident, a confident rule is not overruled by the model, a model that is sure where the rules are not is followed as a suggestion that is still a question, and two confident answers that disagree are a conflict left to the user.

Files mapped exactly, 1 seed of 120 invoices, full month and first-12-days variants, with `gemini-3.1-flash-lite` on Google's free tier (recorded in `backend/cassettes/`, so `--replay` reproduces every model row without a key):

| | development | held out | stress |
|---|---|---|---|
| rules, each file alone | 72% | 62% | 28% |
| model, each file alone | 78% | 35% | 36% |
| rules, both files together | 89% | 77% | 47% |
| rules and model together (`hybrid`) | 92% | 83% | 70% |

No file was confidently wrong in any row; everything not exact is a question for the user. The model alone is worse than the rules on the held-out styles: it leaves the payment reference of `stripe_like` and the free-text column of `xero_like` open, where the rules are unsure of the customer column of `xero_like` instead. Because they are unsure about different things, together they settle more than either, and the gain is largest on the stress styles, whose headers are in languages the rules' word lists do not cover or are just `Column A`. One seed is 360 files; the per-group figures for held out (48 files) are the least certain. The 29 calls that one seed needs used about 0.8 million input tokens.

### Mapping with a model that measures both files

`ai/mapping/agent.py` gives the model both files at once and tools to measure them: values two columns share, payment values that name an invoice, amount columns that agree, whether payments fall after a date, which of two dates is earlier, and more masked examples. The code only measures; the model decides, and its answer is checked against the data like the single-file model's. The idea was to replace the rules' word lists and thresholds with the model's judgement. `--mapper agent --sample 15` scores it on 15 cases per group, drawn the same way for every mapper.

Files mapped exactly, on a sample from data the prompt was not written against (`--first-seed 1 --sample 15`, `gemini-3.1-flash-lite`, recorded in `backend/cassettes/`):

| | development | held out | stress | files confidently wrong |
|---|---|---|---|---|
| rules, both files together | 77% | 70% | 43% | 0 |
| rules and the single-file model (`hybrid`) | 77% | 70% | 63% | 0 |
| model with tools (`agent`) | 77% | 60% | 63% | 3 |

The agent did not beat the rules with the single-file model, and it was confidently wrong where neither of the others was: twice it took a bank's value date for the booking date, and once it said an invoice file had no free-text column. It also read about 1.15 million input tokens for 45 cases against 0.2 million, because every turn sends the conversation again, and one case was lost to the provider being overloaded. A first version did worse on a first sample (73% / 63% / 53%); I fixed what that run showed (an instruction about value dates that I had left out, a date-comparison tool, settled formats) and measured the second version on fresh data, so the table above is not tuned to its own cases. This is one small model on the free tier; a stronger model may do better, and I have not measured one. ADR 0011 has the decision.

## Adjudicating ambiguous payments

The engine never decides which of several look-alike invoices a payment belongs to; it reports an `ambiguous_payment` case. `ai/adjudication/` reads what the payer wrote and may suggest one. A suggestion is advice for a person to act on at the source, never a match.

`adjudicate_by_rules` uses no model. It reads an invoice number in the memo (`inv 121`, `INV000121`, `invoice #121`, ignoring anything that reads as a year) or a word that only one invoice has, and it says nothing when the memo does not separate the candidates. It never decides from the order or the dates.

`LlmAdjudicator` sends the memo as written and, for each candidate labelled A, B, C, its invoice number, what it is for and how many days before the payment it was issued. The customer, the amounts and the payment id are not sent. The model must answer with a label or `none`, a confidence, and a quote of the memo that its choice rests on. The engine relies on the choice only if the confidence is high and the quote is really in the memo; an invented quote makes the choice a suggestion. `combine_rulings` follows the same policy as the mapping stage: agreement is confident, a confident rule is not overruled, a model that is sure where the rules abstain is a suggestion, and two confident answers that disagree are left to a person.

`python -m recon.evaluation.adjudication_eval --adjudicator rules|llm|llm-unchecked|hybrid` scores an adjudicator against the invoice the generator says was meant, by the kind of memo the payer wrote. On the generated data, 8 seeds of 200 invoices:

| memo | cases | rules: correct | rules: abstained |
|---|---|---|---|
| explicit | 23 | 23 | 0 |
| descriptive | 22 | 22 | 0 |
| paraphrased | 26 | 0 | 26 |
| useless | 25 | 0 | 25 |

The rules are never confidently wrong. No adjudicator can do better than abstain on a useless memo, so 74% of these cases is the most that can be answered; the rules answer 47%, and the 27% that are paraphrased, with synonyms, other languages or typos, are what a model could add.

With the model (`--adjudicator llm`, `gemini-3.1-flash-lite`, the default 5 seeds of 200 invoices, recorded in `backend/cassettes/`):

| memo | cases | rules: correct | model: correct | model: suggested, right | model: wrong |
|---|---|---|---|---|---|
| explicit | 13 | 13 | 13 | 0 | 0 |
| descriptive | 14 | 14 | 14 | 0 | 0 |
| paraphrased | 17 | 0 | 15 | 2 | 0 |
| useless | 16 | 0 | 0 | 0 | 0 |

The model settles 42 of the 44 cases that can be answered and suggests the right invoice for the other two, without settling them because it was not sure. It abstained on all 16 useless memos and was never wrong. The rules settle 27 of the same 60.

In the application, the free text of a record is an optional column in the mapping (shown as Description for invoices and Payment memo for payments), stored with each record and shown under it in the case view. Every ambiguous-payment case carries a suggestion: the rules' reading of the memo, computed when the case is opened. When a language model is configured, "Ask the model" (`POST /api/cases/{id}/suggestion`) asks it once for what the case holds now and keeps the answer for that exact input, so asking again costs nothing and a changed file is a new question. The case view says who read the memo, quotes the words the suggestion rests on, and marks a suggestion the rules could not settle as "A suggestion, not settled". `GET /api/capabilities` says whether a model is configured. Nothing about a case changes because of a suggestion; the next step stays correcting the reference at the source.

## Explaining a case with a model

Every case has a reading written by rules from its records (`cases/explain.py`). When a language model is configured, "Explain with the model" (`POST /api/cases/{id}/explanation`) asks it for a second one, shown under the first. A model's text is only shown if a program has checked it, which is the point of `ai/explanation/`.

The model is told the kind of exception, the difference the engine calculated, and each record's amount, date and free text, with the records labelled `R1`, `R2`. It is not given the customer or the real ids, and it must refer to a record only as `[R1]`. Customer names and ids can still reach it if a payer wrote them in a memo. `verify` then rejects the text if it cites a label that is not in the case, states a number that is not one of the engine's figures (the amounts, their totals, the record counts, the parts of a date, or a number a record's own text contains), writes out an id that no record text contains, cites no record, or is longer than its limits. Accepted text has the labels replaced by the real ids. Anything rejected is not shown; the response lists what failed and the rule-written reading stands.

A verified explanation is kept for the exact input it read (`case_explanation`), so asking again costs nothing and a changed file is a new question. A rejected attempt is kept as a record but not reused, so asking again asks again. What the check cannot do: it confirms that the figures and records are the engine's, not that the sentence built from them is wise. A sentence can use only true numbers and still point the reader at the wrong thing, so the text is labelled with who wrote it and what was checked, and says nothing was changed.

`python -m recon.evaluation.explanation_eval` asks the model to explain up to three cases of each kind the engine finds in generated months and counts how many explanations the checks accept. With `gemini-3.1-flash-lite` (2 seeds, recorded in `backend/cassettes/`), all 46 were accepted: none cited a record outside the case, stated a figure the engine had not computed, or wrote a real id. I read a sample, and they are correct and plain. That says less than it seems: these cases have two or three records and generated memos, so there was little to get wrong. The tests also use a scripted model that invents a figure, to show the check rejects it.

## Investigating why a case arose

The engine says what is wrong with a case: an invoice paid short, an invoice with no payment, a payment that settles nothing. Why is the question a person spends their time on, and it usually needs a look at the rest of the month. "Investigate with the model" (`POST /api/cases/{id}/investigation`) has a model do that look and propose an answer that a person then approves or rejects. It covers amount mismatches, missing payments and unmatched payments.

**What the model sees and can do** (`ai/investigation/`). The case's records, under aliases: `I1`, `I2` for invoices, `P1` for payments, `C1` for customers, numbered in the order they are first shown. It never sees a real record id or customer id; it sees amounts, dates and memo text, and a payer may have written a name in a memo. It has four read-only tools: every record of a customer this month, records of any customer within an amount range, sets of two or three open invoices of a customer that add up to a total, and a calculator that evaluates `+ - * /` and nothing else, so that it does arithmetic instead of guessing it. It ends by submitting a cause from a fixed list for the kind of case (or `other`), the records the cause rests on, an action, and a note.

**What checks its finding** (`agent.verify`). The finding is rejected if it cites a record that no tool showed, states a figure above 100 that no tool returned, proposes accepting a variance on a case that is not an amount mismatch, or cites no evidence. A rejected finding is not shown and cannot be approved. Numbers of 100 or less pass unchecked, because percentages, days and counts are written that way; a small invented amount would get through.

**What a person decides** (`graph.py`, `service.py`). The investigation is a LangGraph graph: investigate, check, then stop and wait. LangGraph keeps the run's state in Postgres, in a schema of its own (`langgraph`), so the decision (`POST /api/investigations/{id}/decision`) can come in a later request, after a restart, without asking the model again. Approving adds the finding to the case's notes and, when the cause is a cost the business bears (a processor fee, an early-payment discount, a bank charge), accepts the variance with the reason "write off". Both go through the same case actions a person uses, so they are locked and versioned the same way, and two people deciding at once take turns. Rejecting changes nothing. The case still closes only when the data reconciles. The case view shows the steps the model took, its finding, and Approve and Reject.

**How it was measured.** The generator can write a cause behind each problem (`Config(causes=True)`, off by default so no other recording changes): a processor fee, a discount, a bank charge or swapped digits behind an amount mismatch; not paid, paid by a related account under another customer id, or paid in one lump sum with another invoice behind a missing payment; and the other half of those, or unidentified, behind an unmatched payment. Fee rates, discounts and charges vary from case to case. `python -m recon.evaluation.investigation_eval` scores each finding on its cause, whether it found the records outside the case that the cause involves (the other invoice of a lump sum, the related account's payment), whether its action fits, and whether it passed the checks. Two baselines are scored on the same cases: always naming the commonest cause for the kind, and rules I wrote for this generator, knowing its exact rates and charges.

With `gemini-3.1-flash-lite` on the first 30 cases (10 of each kind, seeds 0 and 1). The free daily quota ran out with 26 complete; the 4 left are amount mismatches:

| | cases | right cause | found the other records | passed the checks |
|---|---|---|---|---|
| amount mismatch | 6 | 5 | 6 | 6 |
| missing payment | 10 | 10 | 10 | 10 |
| unmatched payment | 10 | 10 | 10 | 10 |

The commonest-cause baseline names the right cause for 37% of a larger sample of 133 cases, and the rules for 99% of it (26 of these 26). The one miss called a processor fee an early-payment discount.

What this does and does not show. The rules win because I generated the data with the formulas they encode; on these causes, someone who knows the formulas exactly needs no model. What the model shows is that it gets to nearly the same answers without anyone writing those formulas, finds the records outside the case that the answer rests on, and writes it up in a form a person can approve. Whether that holds on causes nobody anticipated, on real exports, or with long and messy memos is not measured. 26 cases is a small sample. ADR 0012 has the decision.


## Synthetic data

The repository is public, so it only uses generated data. The generator writes an invoice export and a payment export with a known set of problems, a ground-truth file, and a second revision in which some of the problems are fixed at the source:

```bash
cd backend
uv run python -m recon.datagen --out /tmp/recon-data --seed 42
```

Every generated invoice has a short description (`Hosting Jan 2026`) and every payment a memo, in the generator's data but not yet in the exported files. The memo of an ambiguous payment, one that could belong to two or three look-alike invoices, is one of four kinds that the ground truth records: `explicit` (it names the intended invoice, written the way people write it), `descriptive` (it says what it is for, in the words of the invoice), `paraphrased` (it says so in synonyms, another language or with a typo) or `useless`. Look-alike invoices always read differently. The text comes from its own random stream, so it changes none of the numbers, dates or ids; a test pins the hashes of the default exports.

Real exports differ in header text, language, date format and number format, and they carry columns that look like the one you want. `--invoice-style` and `--payment-style` write the same data as one of several export styles, for example a German ERP export with `Bruttobetrag` and a decimal comma, a SAP-style export with `BELNR` and `BUDAT`, or a payment processor export with timestamps and a fee column next to the amount. Each run also writes `mapping_truth.json` with the correct column mapping, the lookalike columns that must not be mapped, and whether the date order can be told from the values at all. Two styles are held out, modelled on publicly described layouts of an accounting product and a payment processor (`xero_like`, `stripe_like`); I wrote them before the baseline mapper and don't tune against them. Their headers come from secondhand descriptions, not from an official export. A test writes every style, reads it back with its true mapping and checks that the records equal the generated ones.

```bash
uv run python -m recon.datagen --out /tmp/recon-data --invoice-style german --payment-style processor
```
