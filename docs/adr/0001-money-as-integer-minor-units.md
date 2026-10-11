# 0001. Money is stored as integer minor units with an explicit currency

## Context

Reconciliation is arithmetic on money: sums, differences and tolerance checks. Binary floating point cannot represent most decimal amounts exactly, so `0.1 + 0.2 != 0.3`. Across thousands of records, those errors turn into phantom variances or hide real ones.

## Decision

Every amount is an integer count of the currency's minor unit (cents for USD), stored together with an ISO 4217 currency code. Parsing converts source strings to integers once, at the ingestion boundary, using decimal parsing and never a float. The minor-unit exponent comes from a currency table, so JPY has 0 decimal places and KWD has 3. Arithmetic across different currencies is rejected; converting between them requires an explicit rate.

Tolerances are also integers in minor units, or basis points of an amount, and never floats.

## Consequences

- Sums and differences are exact, and the same inputs always give the same variance.
- The integer representation maps directly to `BIGINT` in Postgres and to integer columns in dataframes, so I don't depend on how any dataframe library handles decimals.
- Parsing has to handle thousands separators, parentheses for negatives and currency symbols in one place, with tests.
- Display formatting is a separate concern from storage.

## Alternatives

- `NUMERIC` in the database plus `Decimal` in Python: exact, but it spreads decimal semantics through every layer, including the dataframe layer.
- Floats with rounding at the end: not exact, and rounding hides errors.
