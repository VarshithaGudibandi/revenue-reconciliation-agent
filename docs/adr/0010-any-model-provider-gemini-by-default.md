# 0010. Any model provider, with Gemini's free tier by default

## Context

Every model stage (mapping, adjudication, explanations) was written against one provider's SDK, and none of them had been run against a real model: the evaluations were built, the answers were not. A person trying the project should be able to measure the model stages with a key they can get for free, and someone who already pays for another provider should be able to use that one instead.

## Decision

**One interface, three clients.** Every stage talks to `JsonModel`: a system prompt, a user message and a JSON schema in, schema-constrained JSON out (`backend/src/recon/llm/`). `RECON_LLM_PROVIDER` picks the client: `gemini` (the default), `anthropic`, or `openai`, which also reaches any server that copies the OpenAI chat-completions protocol through `RECON_LLM_BASE_URL`. Each reads its key from the variable its own tools use, or from `RECON_LLM_API_KEY`. The Gemini and OpenAI clients are a page of `httpx` each, so no SDK was added; Anthropic keeps its SDK because the effort and refusal-fallback options are in it.

**Flash-Lite, not Flash, as the default.** On the free tier, `gemini-3.5-flash` allowed 20 requests a day, and the first evaluation used them up before it was a fifth done. `gemini-3.1-flash-lite` answered the same mapping prompt correctly in a third of the time, and its allowance is counted separately. A model's name is not a promise either: asked for `gemini-3.5-flash`, the API answered as `gemini-3.6-flash`, so a recorded answer stores the model the response reports.

**A used-up quota stops, it does not wait.** A 429 that asks for a wait of a minute or less is retried after that wait. One that asks for longer is a daily quota, and fails at once as `QuotaExhausted`. In the application that failure is one unanswered question and the rules stand. In an evaluation it ends the run, because a run in which the last third of the calls failed would otherwise be scored as if the model had answered badly. Answers recorded before the stop are kept, and the next run carries on from them.

## Consequences

- Google's free tier may use what is sent to improve its products, and human reviewers may read it. That is acceptable for the generated data in this repository and not for anyone's real records; the README and `.env.example` say so.
- The free allowance is small and Google does not publish it per model, so an evaluation is sized to fit a day and recorded, and anyone can replay the recording without a key.
- The OpenAI-compatible and Anthropic clients are tested against stand-ins for their APIs, not against the services.
