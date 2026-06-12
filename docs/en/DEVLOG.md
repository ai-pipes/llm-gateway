# DEVLOG — LLM Gateway

A chronological development journal. This is where the thought process, dead ends, and insights live. Architectural decisions with rationale are in `adr/`.

---

## v3.4 — Tools Passthrough + PII Restoration in Tool Calls (2026-06-12)

**No breaking changes.** Clients can now pass `tools` in any request; the gateway proxies them transparently to the upstream LLM and returns `tool_calls` to the client. The client owns the agentic loop.

### Why client-owned tools

The alternative was a gateway-side tool registry: the gateway knows about Sentry MCP, internal APIs, etc., and injects the right schemas based on the client. Rejected for the same reason we rejected hardcoding adapters: every new tool would require a gateway deploy, and tool schemas drift from the actual implementations over time. The gateway is a proxy, not a service mesh. Clients send what they need.

### PII restoration in tool_call arguments

The input sanitizer replaces `john@example.com` with `[EMAIL_ADDRESS_3f2a1b0c]` before the LLM sees it. The LLM may echo that placeholder into a `send_email(to="[EMAIL_ADDRESS_3f2a1b0c]")` tool call argument. Without restoration, the client receives the placeholder — the tool call is broken.

**Non-streaming fix:** serialize `response.tool_calls` to JSON, run `context.restore()`, parse back. The placeholder is a contiguous string within the serialized JSON, so simple string replace works.

**Streaming fix:** tool_call arguments arrive character by character across many SSE chunks. `[EMAIL_ADDRESS_3f2a1b0c]` may arrive as `[`, `EMAIL`, `_ADDRESS_3`, `f2a1b0c]` — four separate events. A per-chunk `str.replace()` won't find it.

Solution: a separate `StreamingRestorer` per `tool_call index`. Same look-ahead buffer logic used for text content (v3.3), applied to the argument stream for each tool call. Characters before `[` are forwarded immediately. The buffer only holds bytes that could be the start of a placeholder. When a complete placeholder is matched, the original value is emitted in one chunk. Hold-back is bounded by the placeholder length (~26 chars).

This gives true argument streaming — fragments appear at the client in real time, with a delay only around the placeholder region itself. The consolidated-buffer approach (buffer all argument deltas, emit one chunk at the end) was implemented first but reverted: it was simpler but broke streaming for tool calls when PII was present.

### Behaviour of `[` and `]` in normal text

The restorer's `_probe()` only buffers when the buffer is a prefix of a registered placeholder. `[category]` triggers a one-chunk hold on `[`; as soon as `c` arrives, the prefix check fails and `[` is emitted immediately. No content is dropped. A false positive (LLM generates a string identical to a registered placeholder) requires guessing a 4-byte random hex suffix — 2³² possibilities, negligible in practice.

### finish_reason in streaming

The upstream sends `finish_reason: "stop"` in the final SSE chunk whether or not tool calls were made. We can't trust it. The fix: track `has_tool_calls` during the stream, override `finish_reason` to `"tool_calls"` if any tool_call delta was seen.

---

## v3.3 — Streaming PII Restoration (2026-06-12)

**No breaking changes.** `complete_stream()` now restores PII with true TTFF — max hold-back is the length of the longest registered placeholder (~26 chars).

### The problem with streaming

`[EMAIL_ADDRESS_3f2a1b0c]` is 26 characters. The LLM tokenizer splits text at arbitrary boundaries — a placeholder may arrive as `[EMAIL`, `_ADDRESS_`, `3f2a`, `1b0c]` across four separate SSE chunks. A naive `str.replace()` on each chunk misses cross-chunk matches.

The obvious fix — buffer the entire response, restore, then re-stream — trades the real TTFF for a fake one. The client still waits for the full response before receiving the first token.

### Why a look-ahead buffer is the right shape

The structure of placeholders is known in advance: they start with `[` and end with `]`. Only bytes starting from `[` need to be held. Everything before `[` can be forwarded immediately.

This is essentially a streaming string search: hold just enough state to match or reject, then release. The maximum hold-back is bounded by the longest placeholder, which in practice is always under 30 characters.

### Implementation: `StreamingRestorer._probe()`

The key insight is that we know the exact set of strings we're looking for (the registered placeholders). Instead of a general automaton, we just do prefix matching against the known set:

1. Does the buffer start with a complete registered placeholder? → restore and release.
2. Is the buffer a valid prefix of any registered placeholder? → wait.
3. Neither? → flush `[` and keep going.

The `any(p.startswith(buf) for p in self._map)` call in step 2 does a linear scan across all registered placeholders. For the typical case of 1–10 placeholders per request, this is negligible. A trie would save a constant factor; it is not worth the complexity.

### Audit in streaming

The raw (placeholder) chunks go into `chunks[]` for audit body logging. The restorer transforms only what is yielded to the client. This mirrors the non-streaming audit design: audit sees placeholders, client sees originals.

### `finalize()` for stream end

When the stream ends, the buffer might still hold a partial placeholder prefix (e.g., `[EMAIL_ADDR` if the stream was cut short). `finalize()` flushes it as-is. This is safer than swallowing it: the client sees the partial token and knows something is incomplete, rather than silently losing characters.

---

## v3.2 — PII Restoration (2026-06-12)

**No breaking changes.** Backward-compatible: `context` parameter defaults to `None`, existing sanitizers unchanged.

### What was done

Added `RestorationContext` — a per-request object that tracks `placeholder → original` mappings. When the input sanitizer replaces PII, it now registers each replacement in the context. After the LLM responds, the gateway restores the original values in the response content before returning it to the client. The client sends a real email and gets a real email back — the LLM only ever sees the placeholder.

Only `complete()` (non-streaming). Streaming restoration is a separate future task.

### The approach: why a context object

The alternative was to store the mapping in `request.state`. We rejected it for the same reason the sanitizer chain was decoupled from the HTTP layer: `request.state` mixes HTTP and application concerns, makes the chat service harder to test in isolation, and binds the mapping to FastAPI's lifecycle. A `RestorationContext` created at the top of `complete()` has an obvious scope, passes cleanly through the sanitizer chain, and is trivially unit-testable.

### Audit security

The audit record stores the **sanitized** (placeholder) version of the LLM response, not the restored version. The sequence matters:

```python
sanitized_content = response.content              # capture before restore
response = replace(response, content=ctx.restore(response.content))
await audit.write(..., completion=sanitized_content)   # placeholder, not PII
```

If the order were reversed, the audit would log raw PII — defeating the point.

### Two bugs found during live testing

**1. LLM copies the example placeholder literally**

The system instruction said: *"tokens like [EMAIL_a3f7c2b1]"*. gpt-4o-mini saw this and wrote `[EMAIL_a3f7c2b1]` directly into the letter signature — as if it were a generic template variable. The LLM did not use the registered placeholder at all.

The fix: replace the concrete-looking example with one that is obviously not a real value: `[EMAIL_ADDRESS_3f2a1b0c]`, plus the note *"These are NOT real values — do not invent, guess, or reconstruct the originals."* The test case had to be more specific too: the example placeholder and the system instruction together must not suggest to the model that it should invent similar tokens.

Lesson: anything in the system prompt that looks like a template variable *will* be treated as one by the model.

**2. Overlapping Presidio spans corrupt the replacement**

Presidio detected `sarah@techcorp.io` as both `EMAIL_ADDRESS` (positions 29–46, score 1.0) and as `URL` for `techcorp.io` (positions 35–46, score 0.5). Right-to-left replacement processed `URL` first (higher start index), inserted `[URL_xxxxxxxx]`, then tried to replace `EMAIL_ADDRESS` at the original indices — producing `[EMAIL_ADDRESS_xxxxxxxx]a1]`.

The fix: `_resolve_conflicts()` removes overlapping spans before replacement, keeping the highest-confidence span per conflict. This mirrors what `AnonymizerEngine.anonymize()` does internally but in the context-aware path we bypass `anonymize()` entirely.

The bug would not have appeared with the standard (no-context) path because `AnonymizerEngine` handles conflicts internally. The custom right-to-left loop had to replicate that logic explicitly.

---

## v3.1 — SSE Streaming (2026-06-10)

**No breaking changes.** Fully backward-compatible: requests without `"stream": true` follow the old path unchanged.

**New feature:**
- `POST /v1/chat/completions` with `"stream": true` returns `StreamingResponse(media_type="text/event-stream")` in OpenAI streaming format (`chat.completion.chunk` chunks + `data: [DONE]`).
- True end-to-end streaming: tokens are sent to the client immediately, without buffering at the gateway.
- `complete_stream()` in `ChatService` — async generator with `try/finally` guaranteeing audit writes on success, error, and client disconnect.
- `stream_chat()` in `OpenAICompatibleAdapter` — httpx streaming with usage capture from the trailing chunk.
- Output sanitizer removed from the critical path of the client response; for audit body logging it runs after the fact on the full text in the `finally` block.
- `status="cancelled"` in audit on connection drop (via `stream_complete` flag + `GeneratorExit`).
- Errors in stream are serialized as SSE error events (`data: {"error": {...}}`); `data: [DONE]` is guaranteed via `finally`.

**Architecture changes:**
- `BaseLLMAdapter.stream_chat()` — new abstract method with `usage_out: dict | None = None` parameter.
- `ChatService._sanitize_input()` and `_resolve_adapter()` — extracted into private methods, reused in `complete()` and `complete_stream()`.
- `OpenAICompatibleAdapter._build_payload()` — extracted from `chat()`, reused in `stream_chat()`.

---

## v3.0 — Layered Architecture + Body Logging (2026-06-09)

**Breaking changes:**
- Import paths for all base classes and implementations have changed. Old paths (`gateway.middleware.auth`, `gateway.audit.base`, `gateway.adapters.base`, `gateway.sanitizers.base`) are removed. New paths:
  - `gateway.infrastructure.auth.static_key.StaticKeyAuthProvider`
  - `gateway.infrastructure.auth.static_key.AuthMiddleware`
  - `gateway.infrastructure.auth.base.BaseAuthProvider`
  - `gateway.domain.audit.base.BaseAuditBackend`
  - `gateway.domain.adapters.base.BaseLLMAdapter`
  - `gateway.domain.sanitizers.base.BaseSanitizer`, `SanitizerChain`
  - `gateway.infrastructure.audit.stdout_backend.StdoutAuditBackend`
  - `gateway.infrastructure.audit.file_backend.FileAuditBackend`
  - `gateway.infrastructure.adapters.openai_compatible.OpenAICompatibleAdapter`
  - `gateway.infrastructure.sanitizers.pii_regex.PiiRegexSanitizer`

**Architecture changes:**
- `SanitizeMiddleware` and `AuditLogMiddleware` removed. Logic consolidated into `ChatService` (application layer).
- New layer structure: `api/openai/` → `application/` → `domain/` → `infrastructure/`.
- `request.state` no longer used as an implicit data channel between layers.

**New feature:**
- `AuditRecord` now has optional `messages: list[dict] | None` and `completion: str | None` fields.
- Opt-in body logging: `audit.body_logging.enabled: true` logs full sanitized messages and LLM completion in every audit record.

---

## 2026-06-05 — Project Start and Landscape Research

### Why this project

Starting a series of educational projects on enterprise AI engineering. LLM Gateway is the first building block. The idea: corporations want to use LLMs but can't route traffic directly to Anthropic/OpenAI without oversight. They need a proxy inside the corporate perimeter with logging, sanitization, and the ability to plug in any LLM.

### What I looked at in the existing space

Surveyed the entire LLM gateway market: LiteLLM (40K stars, Python), Portkey (TypeScript, enterprise), Helicone (Rust, observability), Kong AI Gateway (Lua, enterprise plugins), Bifrost (Go, 11µs overhead), TensorZero (Rust, ML-driven routing).

**Key insight from the research:** all existing solutions are either too developer-tool-oriented (LiteLLM) or too enterprise-locked (Kong). There's no solution that is simultaneously easy to understand AND properly designed for a corporate context.

**Unexpected finding:** in March 2026 LiteLLM suffered a supply chain attack via PyPI — compromised packages were stealing API keys. This reinforced the choice to build a project with minimal dependencies.

### Key decisions made today

**Python vs Go/Rust:** chose Python despite lower performance. Reason: ecosystem, readability for learning purposes, rich NLP libraries for future sanitizers. For an educational project, code clarity matters more than 11µs latency.

**ASGI Middleware vs Event Bus:** considered two approaches. An event bus would give better latency for logging, but for an audit trail you need a guarantee that the record is written BEFORE the response is sent to the client. ASGI middleware provides this out of the box. The tradeoff: slightly slower, but compliance-correct by default.

**Important clarification on Auth:** initially considered built-in auth logic (API keys in config). But that's wrong for a corporate tool — every company has its own system (LDAP, Okta, JWT). Made `BaseAuthMiddleware` abstract. The gateway is the framework; the logic is left to the user. This is the pattern for all three extension points: Auth, Sanitizer, Adapter.

**On Claude Code integration:** discovered that Claude Code supports `ANTHROPIC_BASE_URL` for redirecting traffic through a corporate gateway. This changes priorities — in the future we'll need Anthropic Messages API support (`/v1/messages`), not just OpenAI-compatible. For v1 we keep `/v1/chat/completions`; Claude proxy goes to the backlog.

### What wasn't obvious

The OpenAI API has become a de facto standard not just for OpenAI — virtually all new LLM providers (DeepSeek, local models via vLLM/Ollama) implement an OpenAI-compatible endpoint. This means `OpenAICompatibleAdapter` via config will cover ~90% of real use cases without a single line of Python code from the user.

---

## 2026-06-06 — Middleware Ordering Bug and Data Privacy

### What happened

During implementation I ran into a non-trivial conflict between two requirements:

1. **Audit must come AFTER sanitize** — so Audit never sees raw data (privacy-by-design principle)
2. **Blocked requests must be audited** — the design doc explicitly requires a record with `status=blocked`

With `BaseHTTPMiddleware` these requirements conflict. If the order is Auth → Sanitize → Audit → Route, then when Sanitize blocks a request and returns 400 without calling `call_next`, Audit never runs.

### The first "fix" was wrong

Integration tests caught that `test_blocked_request_writes_audit_with_status_blocked` was failing. The quick fix: change the order to Auth → Audit → Sanitize → Route. The test passed. But this created another problem: Audit now wraps Sanitize and sees the request BEFORE sanitization.

This was caught in review: if v3 adds full request logging to AuditMiddleware, private data will end up in the log unsanitized.

### The correct solution

Changed the interaction architecture between Sanitize and Route:

- **SanitizeMiddleware on block** — does not return 400 directly. Instead: saves the error in `request.state.blocked_error`, sets `request.state.audit_status = "blocked"`, and **still calls `call_next`**.
- **Route handler** — first checks `request.state.blocked_error` and returns 400 if set.
- **Order remains** Auth → Sanitize → Audit → Route.

Result:
- Audit always runs for authenticated requests (including blocked ones) ✓
- Audit sees the request only AFTER sanitization ✓
- 401s are not audited (Auth short-circuits before Sanitize and Audit) ✓

### Lesson learned

`BaseHTTPMiddleware` is a stack of nested wrappers. The middleware added last via `add_middleware()` becomes the outermost layer. An early `return` without `call_next` doesn't just "respond to the client" — it prevents all inner middleware from running. This is important to keep in mind when designing the stack: order is not only logical ("who processes the request first") but also determines which middleware will run at all in different scenarios.

---

## 2026-06-07 — v2.2: Audit Backends

### What was done

Replaced the hardcoded `StdoutAuditBackend()` in `create_app()` with a configurable audit backend via discriminated union in `AuditConfig`.

Three options:
- `type: stdout` — JSON lines to stdout (existed before, now via config)
- `type: file` — JSON lines to a file (new `FileAuditBackend`)
- `type: plugin` — custom class via `module:` (same pattern as adapters and sanitizers)

### Key decision: discriminated union vs module/config

Considered two approaches for `AuditConfig`:
1. **module/config** — a single `{module: "...", config: {...}}` like sanitizers. Flexible, but we lose Pydantic validation of `file` backend parameters at startup.
2. **Discriminated union** — `type: stdout | file | plugin`, each type is a separate Pydantic model.

Chose discriminated union. Reason: `FileAuditConfig` requires a mandatory `path` — with discriminated union Pydantic validates this at application startup, before the first request. `type: file` without `path` → `ValidationError` immediately, not at runtime.

**Breaking change:** `audit: {backend: "stdout"}` → `audit: {type: stdout}`. Documented.

### FileAuditBackend — intentional limitations

Synchronous `write` + `flush` without `close()`. Process-oriented lifecycle: the OS closes the FD on exit. Explicit lifecycle hooks (`startup`/`shutdown`) are out of scope for v2.2. No rotation, no buffering.

The tradeoff is intentional: this is a reference implementation for learning purposes, not a production-hardened solution.

---

## 2026-06-10 — Streaming: Design and Tradeoffs

### Why streaming matters

Most real clients — OpenAI SDK, LangChain, agent frameworks — send `"stream": true` by default. Without support they either fail with an error or wait for the full response. For long generations this is unacceptable: the client times out before receiving any output at all.

### The main question: what to do with the output sanitizer

It became clear immediately that chunk-by-chunk sanitization is impossible. An NER model (Presidio) builds entities from the full context — "John" and "Smith" from different chunks won't be assembled into a `PERSON`. This isn't a tradeoff, it's an architectural impossibility.

Three options were considered:

**Option A (pseudo-streaming):** the gateway buffers the entire LLM response, sanitizes it, then sends it to the client as SSE. The client doesn't time out, but there's no real TTFF — the client still waits.

**Option B (upstream streaming + buffer):** stream from LLM to ourselves, buffer, sanitize, then send. Same problems as A — TTFF equals full response time.

**Option C (true end-to-end):** stream from LLM directly to the client, remove output sanitizer from the critical path. For audit body logging — run it after the fact on the full text in `finally`.

Chose C. The key argument: an output sanitizer on the response only makes sense if the **gateway knows more than the client** — for example in RAG, where the gateway injects data from other users. In our scenario, the client already knows everything the gateway knows. Sanitizing the response from the client for the client itself is pointless.

Additionally, it turned out that the output sanitizer in `complete()` was wired up but never actually called (the unsanitized `result.content` was going into the response, not the sanitized version). This was caught in review. In `complete_stream()` this was made explicit: the output sanitizer is called only for audit body logging.

### Audit during streaming: try/finally

Streaming makes auditing tricky: the client can disconnect at any moment. Python raises `GeneratorExit` (a BaseException, not Exception) when a generator is closed via `aclose()`. A `try/except Exception` won't catch it.

The solution: `try/except/finally` around the streaming loop. `finally` always runs — on success, on exception, and on `GeneratorExit`. In `finally` we write the audit unconditionally.

To distinguish "completed normally" from "client disconnected" — a `stream_complete = False` flag that is only set to `True` after exiting the `async for`. In `finally`: `if not stream_complete and status == "success": status = "cancelled"`.

### Usage tokens in streaming

OpenAI delivers usage not in the last content chunk but in a separate chunk **after** `[DONE]`. This means a naive implementation that `break`s on `[DONE]` will lose usage data.

The solution: a `stream_done = False` flag. When `[DONE]` is seen, set `stream_done = True` but continue iterating. Content after `[DONE]` is not yielded (`if stream_done: continue`), but the usage chunk is still processed. This guarantees that `usage_out` will be populated even if the provider sends usage after `[DONE]`.

### Errors as SSE events

The problem: after the first byte of the response, the HTTP status has already been sent (200). You can't return a 400 or 500. The solution is to serialize errors as SSE events with an `error` field:

```
data: {"error": {"type": "gateway_error", "code": "upstream_timeout", "message": "..."}}

data: [DONE]
```

`data: [DONE]` is guaranteed via `finally` in `_sse_stream()` — even after error events. OpenAI clients expect `[DONE]` to terminate the stream.

### What turned out to be surprising

At first I thought streaming was simply "pass chunks to the client." It turned out the real complexity lies in handling edge cases correctly: usage-after-DONE, client disconnect in finally, error serialization without changing the HTTP status, guards against `choices[0] = null` in streaming responses. The actual chunk forwarding is trivial.
