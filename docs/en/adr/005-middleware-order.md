# ADR-005: Middleware Order and Privacy-by-Design

**Date:** 2026-06-06  
**Status:** Accepted

## Context

The ASGI middleware stack has three layers: Auth, Sanitize, Audit. We needed to determine their execution order given two competing requirements:

1. **Privacy-by-design:** Audit must never see unsanitized data. This is especially important for v3, where full request/response logging is planned — it must log already-cleaned text.
2. **Audit of blocked requests:** Requests blocked by the sanitizer must also be audited with `status=blocked` (a compliance requirement — the fact that a data transmission was attempted must be recorded).

## Options Considered

**A. Auth → Audit → Sanitize → Handler**  
Audit wraps Sanitize — it runs for all authenticated requests, including blocked ones. But Audit sees the request before sanitization. Violates privacy-by-design.

**B. Auth → Sanitize → Handler → Audit (post-handler)**  
Audit is written after the Handler via response middleware. But if Sanitize blocks a request with an early `return` (without calling `call_next`), the entire inner chain is aborted and Audit never runs. Violates audit-of-blocked.

**C. Auth → Sanitize → Audit → Handler, where Sanitize does not break the chain on block**  
When blocking, Sanitize stores the error in `request.state.blocked_error` and calls `call_next` anyway. The Handler checks the flag and returns a 400 on its own. Audit runs as a wrapper around the Handler and only sees sanitized data.

## Decision

**C. Auth → Sanitize → Audit → Handler.**

## Rationale

Option C satisfies both requirements:

- Audit comes after Sanitize in the chain → sees only sanitized data ✓  
- Sanitize does not break the chain on block → Audit runs for blocked requests ✓  
- Auth performs an early `return` → Audit does not run for unauthenticated requests ✓  

Key insight: in `BaseHTTPMiddleware`, an early `return` without calling `call_next` aborts the **entire** inner chain, not just the current middleware. This means Sanitize cannot both abort the request and allow Audit to run if they are ordered as Sanitize → Audit. The solution is to move responsibility for returning the 400 from Sanitize into the Handler.

## Consequences

- `SanitizeMiddleware` does not return an HTTP response directly when blocking. Instead, it writes to `request.state.blocked_error` and calls `call_next`.
- `GatewayHandler` must check `request.state.blocked_error` first, before parsing the request body.
- The order of `add_middleware()` calls in Starlette is the **reverse** of the processing order. To achieve Auth → Sanitize → Audit → Handler, register them as: `add_middleware(Audit)`, then `add_middleware(Sanitize)`, then `add_middleware(Auth)`.
- Any new middleware added to the stack must explicitly decide: does it break the chain (early `return`) or pass control forward — and how that affects Audit.
