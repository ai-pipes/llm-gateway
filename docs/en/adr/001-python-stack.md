# ADR-001: Python as the Primary Stack

**Date:** 2026-06-05  
**Status:** Accepted

## Context

We needed to choose a language/stack for the gateway. Go, TypeScript, Python, and Rust were all considered.

## Options Considered

| Option | Latency | Ecosystem | Complexity |
|---|---|---|---|
| Rust | <1ms P99 | minimal | high |
| Go | ~11µs | moderate | low |
| TypeScript | moderate | vast (npm) | low |
| Python | ~8ms P95 | AI/ML-rich | low |

## Decision

**Python.**

## Rationale

1. **Educational context.** The project teaches AI engineering — Python dominates that ecosystem. Readability and familiarity matter more than latency.
2. **Future sanitizers.** NLP libraries for PII detection (spaCy, Microsoft Presidio) are Python-first.
3. **Latency is acceptable.** An 8ms overhead is not critical for enterprise use cases where actual LLM calls take 500–5000ms.

## Consequences

- The GIL limits throughput for CPU-heavy operations (NLP sanitization)
- PyPI dependency risks — we minimize dependencies and use only well-known packages
- If scaling becomes necessary, we can run multiple uvicorn workers or migrate to Go in the future
