# ADR-002: ASGI Middleware Stack as the Architectural Pattern

**Date:** 2026-06-05  
**Status:** Accepted

## Context

We needed to organize the request processing pipeline: auth, sanitization, routing, logging. Two options were considered: ASGI Middleware Stack vs. Chain of Responsibility + Async Event Bus.

## Options Considered

**A. ASGI Middleware Stack**  
Each concern is a separate ASGI middleware. Everything is synchronous and strictly sequential. This is the standard pattern in the Python web ecosystem (Django, FastAPI).

**B. Chain of Responsibility + Async Event Bus**  
A synchronous chain for blocking operations (sanitization), and an asynchronous event bus for logging/metrics. Better latency.

## Decision

**A. ASGI Middleware Stack.**

## Rationale

The central question is what happens to the audit trail on failure.

With option B, the response is sent to the client before the audit is written. If the event bus crashes or the queue overflows, the record is lost — but the data was already delivered. For compliance-sensitive domains (finance, healthcare) this is unacceptable.

With option A, the audit is written synchronously. If the write fails, the client receives a 500 and the LLM response is not delivered. The guarantee holds: no record means no response.

Making option B reliable would require a durable event bus (Redis Streams, Kafka) — a significant increase in complexity for v1 in exchange for latency that is already acceptable.

## Consequences

- Higher latency: each synchronous middleware adds time
- Audit overhead directly affects client response time
- If async logging is needed, it can be added in v2 as a separate `ObservabilityMiddleware` with fire-and-forget semantics (metrics do not require compliance guarantees)
