# 0002. The engine matches and calculates; the LLM interprets

## Context

An LLM is good at reading messy column names and reference documents, and at explaining things in plain language. It is unreliable at exact arithmetic, and it cannot be audited the way a rule can. A reconciliation result has to be reproducible and defensible.

## Decision

Deterministic code owns everything numeric and every final match decision:

- normalization of files into records
- candidate generation and matching passes
- sums, variances and tolerance checks
- exception classification by rule where a rule exists

The LLM is used only for bounded tasks with structured output that is validated before use:

- proposing a mapping from source columns to canonical fields
- extracting a data dictionary and rules from reference documents
- ranking candidates for an ambiguous match, from a set the engine supplies
- writing the explanation, likely cause and next step for an exception
- phrasing questions to the user when a mapping or match needs confirmation

The LLM never produces a number that is stored as a result. It cites records by ID, and the cited IDs are checked against the data. A mapping or a match the LLM proposes takes effect only after validation, and a mapping takes effect only after the user confirms it.

## Consequences

- The same input and configuration always give the same matches. Only the explanations vary between runs.
- Accuracy of mapping and explanation can be measured against synthetic data with known injected errors.
- The LLM provider can be replaced without touching the matching logic.
- Some cases a free-form agent might resolve end up as exceptions or as questions to the user. I accept that.

## Alternatives

- Letting the LLM read rows and decide matches directly: simpler to build, but not reproducible, and it scales poorly with file size and cost.
