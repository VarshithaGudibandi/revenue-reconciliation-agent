# Revenue Reconciliation Agent

I'm building an agent that reconciles revenue data across financial systems, for example invoices against payments, or processor payouts against bank deposits. It finds the records that don't match and explains each difference: how large it is, where it probably comes from, and what to check next.

Two principles shape the design:

- **The engine does the matching and the arithmetic.** An LLM never computes a sum or decides a match on its own. It proposes column mappings, reads reference documents, judges ambiguous candidates, and writes the explanations.
- **The data decides when an issue is resolved.** Source files are never edited. When a corrected file arrives, the affected records are matched again, and an exception closes only when the new data reconciles.

## License

MIT. See [LICENSE](LICENSE).
