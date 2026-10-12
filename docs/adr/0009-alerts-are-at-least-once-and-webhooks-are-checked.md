# 0009. Alerts are at-least-once, and webhook targets are checked

## Context

A message that says something needs attention is worth little if it comes twice, or never, or if setting up a channel lets someone make the server call an address on its own network.

## Decision

**One delivery row per channel and event.** The key is the run (or the day, for a digest). A delivery is claimed before it is sent and marked afterwards, so a retried job sends nothing twice. If a worker dies between the send and the mark, the claim expires after two minutes and the message is sent again. That is at-least-once, and I chose it over at-most-once because a lost alert is the worse failure. Exactly-once is not possible with a chat service that has no idempotency key.

**Retry what can pass.** A timeout, a refused connection, a 429 and a 5xx are retried with backoff on the job queue. A 4xx (a revoked webhook) is recorded and not retried.

**A webhook must be https and must not resolve to an internal address.** The check is made when the channel is created and again when sending, and redirects are not followed. Without it a channel is a way to make the server request anything it can reach. Two things remain: the name is resolved by the check and again by the client, so a hostile DNS server could answer differently the second time, and the URL, which is a secret, is stored unencrypted and shown masked.

## Consequences

- The checks can be relaxed for a local setup (`RECON_ALERTS_ALLOW_HTTP`, `RECON_ALERTS_ALLOW_PRIVATE`), and the tests rely on that.
- The Slack and Teams payloads were tested against a local server, not the services. The Teams format follows Microsoft's description of the Workflows webhook.
