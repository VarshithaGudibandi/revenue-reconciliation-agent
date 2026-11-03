# 0011. A mapping agent with tools was measured, and is not the default

## Context

Column mapping combines hand-written rules (header words, thresholds, cross-file measurements) with a model that maps one file at a time. The rules carry most of the cross-file reasoning: the amount column is the one whose values equal the other file's amounts, the payment reference is the column that names invoice ids. The question was whether a model given both files and tools to take those same measurements could replace the rules, so that the code measures and the model decides.

## Decision

**Built, measured, kept off by default.** `mapping/agent.py` gives the model both file descriptions and six measurement tools, and checks its answer against the data. On a sample of 45 cases from data the prompt was not written against, with `gemini-3.1-flash-lite`, it read 77% / 60% / 63% of files exactly (development / held out / stress) against 77% / 70% / 63% for the rules with the single-file model, and was confidently wrong on 3 files where the others were wrong on none. It used close to six times the input tokens. The rules with the single-file model stay the default; the agent is reachable from the evaluation (`--mapper agent`).

**Measured once on fresh data.** The first version did worse on a first sample. What that run showed was fixed (a missing instruction about value dates, a tool to compare two dates, settled formats), and the second version was measured on a sample from a different seed, so that the result is not tuned to the cases it was scored on.

## Consequences

- The tool-calling layer (`llm/tools.py`) works for Gemini, Anthropic and OpenAI-compatible servers and is recorded for replay; it is there for the next use of tools, a case investigation.
- The confident errors are the reason for the decision more than the averages: a wrong column that looks settled is the one failure the setup screen cannot catch.
- One small model was measured. A stronger model may close the gap; that is the next measurement, and it needs a quota larger than the free tier allows (one evaluation takes about 130 requests).
