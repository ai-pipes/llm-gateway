# ADR-004: Synchronous Audit Trail Writes

**Date:** 2026-06-05  
**Status:** Accepted

## Context

The audit trail is the top logging priority. The question is whether to write it synchronously (blocking the client response) or asynchronously (fire-and-forget, no impact on latency).

## Options Considered

**A. Synchronous write**  
The response is only sent to the client after the audit record has been successfully written. If the write fails, the client receives a 500.

**B. Asynchronous write (fire-and-forget)**  
The response is sent immediately; the audit is written in the background. Requires a durable backend (Redis Streams, Kafka) for at-least-once guarantees.

## Decision

**A. Synchronous write.**

## Rationale

Compliance requirement: if data was delivered to the client, that fact must be recorded. Violating this invariant creates legal exposure in financial and healthcare sectors.

Option B is only reliable with a durable event bus. Without one, it provides at-most-once semantics. With one, it introduces significant infrastructure complexity (Redis/Kafka as a mandatory v1 dependency).

The latency overhead is justified: real LLM calls take 500–5000ms. A synchronous write to stdout adds less than 5ms — under 1% of total request time.

## Consequences

- A slow audit backend directly slows down clients — this is an important consideration when choosing a production backend
- Recommended high-throughput backend: stdout → fluentd/logstash (local socket, <1ms)
- For observability metrics (non-compliance) — a separate async `MetricsMiddleware` without this constraint will be added in v2
- `BaseAuditBackend` abstracts the implementation: stdout (v1), file (v2), postgres (v2)
