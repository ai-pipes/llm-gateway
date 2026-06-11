# Streaming Support — Design Document

**Date:** 2026-06-10
**Status:** Implemented
**Author:** Alexander Melnik
**Version:** v3.1

---

## Context and Purpose

v3.0 supports only the request/response model: `POST /v1/chat/completions` → full JSON response. Most real-world clients (SDKs, chat UIs, agent frameworks) send `{"stream": true}` by default — without streaming support they either fail or wait for the full response, which causes timeouts on long generations.

Goal: add SSE streaming (`data: {...}\n\n`) compatible with the OpenAI API, without touching the sanitizer and audit.

---

## Key Design Decision: What to Do with the Output Sanitizer

The central question with streaming is how to reconcile it with the output sanitizer and audit, which operate on the full text.

### Three Options

**Option A: Buffer → SSE (pseudo-streaming)**
- Call the adapter without streaming, get the full response
- Run the output sanitizer as usual
- Deliver to the client in SSE format, split into chunks
- The client does not time out, but there is no real TTFF

**Option B: Upstream streaming + buffer → SSE**
- Stream from the LLM, buffer chunks on our side
- After the full buffer — output sanitizer on the full text, audit
- Client receives SSE, but TTFF = full LLM response time

**Option C: True end-to-end streaming (chosen)**
- Stream from LLM → deliver to client immediately
- Remove output sanitizer from the critical path
- Write audit after the fact in a `finally` block after the stream completes
- True TTFF

### Why the Output Sanitizer Is Not Needed on the Response

Chunk-by-chunk sanitization is unreliable: a PII detector (NER model) requires full context — "John" and "Smith" may arrive in different chunks and the entity will not be found. This is not a technical compromise but an architectural impossibility.

More importantly: the output sanitizer protects against leaking PII to the client. It makes sense when the **gateway knows more than the client** — for example, a RAG system with data from all users. If that scenario does not apply, an output sanitizer on the response provides little value.

For **audit logs**, the output sanitizer remains useful — to prevent PII from entering compliance logs. The solution: run it after the fact on the full collected text, only when `log_body: true`.

### Summary

```
Input sanitizer:  full messages (as always) → unchanged
Output sanitizer: only for audit body logging, after the fact
Audit:            written in finally after the stream completes
Client:           receives tokens immediately — true TTFF
```

---

## Architecture

### Data Flow

**Non-streaming (unchanged):**
```
Request → input sanitize → adapter.chat() → audit.write() → JSON response
```

**Streaming:**
```
Request → input sanitize → adapter.stream_chat()
                                   │
                             chunk → yield SSE → client  (real-time TTFF)
                             chunk → buffer[]
                                   │
                           [DONE] / disconnect
                                   │
                         join(buffer) → output sanitize (if log_body=True)
                                   │
                         audit.write()  ← guaranteed via finally
```

### What Changed Per Layer

#### `domain/adapters/base.py`

A `usage_out: dict | None = None` parameter was added to `stream_chat()`. The adapter populates it with usage data from the last SSE chunk. The default implementation is a fallback via `chat()`.

```python
async def stream_chat(
    self, request: ChatRequest, usage_out: dict | None = None
) -> AsyncGenerator[str, None]:
    response = await self.chat(request)
    yield response.content
```

#### `infrastructure/adapters/openai_compatible.py`

Real `stream_chat()` implementation via `httpx.AsyncClient.stream()`:
- Parses SSE lines (`data: ...` prefix, `[DONE]` sentinel)
- Yields only non-empty `delta.content`
- After `[DONE]`, continues iterating to capture the trailing usage chunk (OpenAI sends usage after `[DONE]` when `stream_options: {include_usage: true}`)
- Guard against `choices[0]` being None
- Shared `_build_payload()` for both `chat()` and `stream_chat()` — no duplication

```python
async def stream_chat(self, request, usage_out=None):
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", url, json=self._build_payload(request, stream=True)) as resp:
            resp.raise_for_status()
            stream_done = False
            async for line in resp.aiter_lines():
                if not line.startswith("data: "): continue
                payload = line[6:]
                if payload == "[DONE]":
                    stream_done = True
                    continue   # continue — usage may come after [DONE]
                data = json.loads(payload)
                if usage_out is not None and data.get("usage"):
                    usage_out.update(data["usage"])
                if stream_done: continue
                content = data["choices"][0].get("delta", {}).get("content", "")
                if content: yield content
```

#### `application/chat_service.py`

New method `complete_stream()` — async generator with `try/finally`:

- Phase 1 (before the first yield): input sanitization + adapter lookup. Errors here (`SanitizerBlockedError`, `AdapterNotFoundError`) write audit and raise an exception before the stream starts — the client receives HTTP 400 before headers are committed.
- Phase 2 (streaming): `try/except/finally` around `async for chunk in adapter.stream_chat()`. Each chunk is immediately yielded to the client and simultaneously appended to the buffer.
- Phase 3 (`finally`): guaranteed to execute on success, error, and client disconnect (GeneratorExit). Assembles the full text, runs the output sanitizer (if `log_body=True`), writes audit.

**Audit statuses during streaming:**

| Situation | `status` |
|---|---|
| Stream completed normally | `success` |
| Client disconnected before completion | `cancelled` |
| Upstream timeout | `error` (error: "upstream_timeout") |
| Other upstream error | `error` |

`cancelled` is determined via a `stream_complete = False` flag that is set to `True` only after the `async for` loop completes normally.

Shared sanitization and adapter lookup code is extracted into `_sanitize_input()` and `_resolve_adapter()` — used by both `complete()` and `complete_stream()`.

#### `api/openai/routes.py`

When `body.get("stream")` is truthy, returns `StreamingResponse(media_type="text/event-stream")`.

The `_sse_stream()` generator formats chunks into the OpenAI chunk format:
```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"...","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"...","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

Key details:
- **`[DONE]` is guaranteed via `finally`** — sent even on error
- **Errors as SSE events**: HTTP 200 has already been sent after the first byte, the status cannot be changed. Errors are serialized as `data: {"error": {...}}\n\n` before `[DONE]`
- **`delta: {}`** in the final chunk (not `{"content": ""}`) — per the OpenAI spec
- **`model`** field in every chunk

---

## Usage Tokens in Streaming

Known limitation: `complete_stream()` writes `prompt_tokens` and `completion_tokens` to audit from `usage_out`. If the provider does not support `stream_options: {include_usage: true}` or does not return usage — the audit will contain zeros.

This is an accepted trade-off: most production providers (OpenAI, Anthropic, vLLM) return usage in the last chunk.

---

## Trade-offs and Known Limitations

| Aspect | Decision | Trade-off |
|---|---|---|
| Output sanitizer on the response | Removed from critical path | Response to client is not sanitized; only audit body logging is sanitized |
| Audit | After the fact in `finally` | Audit does not block the first token; if audit fails — the client has already received the response |
| Usage tokens | Best-effort from the last chunk | Zeros if the provider does not support it |
| Model in audit | Requested model, not resolved | vLLM/proxy may map aliases — there is no response object in streaming |
| `temperature` and other parameters | Not forwarded from HTTP body | Pre-existing gap, not added in this release |

---

## Error Handling

### Pre-stream Errors (before the first yield)
Occur during input sanitization and adapter lookup. The generator has not yet started yielding — FastAPI catches the exception and returns HTTP 400/404 as a regular JSON response.

### In-stream Errors (after the first yield)
HTTP 200 has already been sent. Errors are serialized as SSE error events:

```
data: {"error":{"type":"gateway_error","code":"upstream_timeout","message":"LLM request timed out"}}

data: [DONE]
```

| Exception | SSE code |
|---|---|
| `SanitizerBlockedError` | `sanitizer_blocked` |
| `AdapterNotFoundError` | `adapter_not_found` |
| `UpstreamTimeoutError` | `upstream_timeout` |
| `UpstreamError` | `upstream_error` |
| Any other | `internal_error` |

---

## Testing

### `tests/unit/test_adapters.py` — new tests

| Test | What it verifies |
|---|---|
| `test_stream_chat_yields_content_chunks` | chunks are yielded correctly |
| `test_stream_chat_populates_usage_out` | usage is populated from the chunk containing usage |
| `test_stream_chat_usage_after_done_sentinel` | usage is captured even when it comes after `[DONE]` |
| `test_stream_chat_skips_empty_delta` | role-only and empty deltas are not yielded |
| `test_stream_chat_raises_on_http_error` | HTTP 4xx/5xx raises an exception |

### `tests/unit/test_chat_service.py` — new tests

| Test | What it verifies |
|---|---|
| `test_complete_stream_yields_chunks` | chunks reach the caller |
| `test_complete_stream_writes_audit_after_stream` | audit is written after the stream |
| `test_complete_stream_no_body_logging_by_default` | `completion=None` without `log_body` |
| `test_complete_stream_body_logging_sanitizes_output` | output sanitizer runs on the full text for audit |
| `test_complete_stream_blocked_input_raises_before_yielding` | blocked → audit + exception before yield |
| `test_complete_stream_adapter_not_found_raises_before_yielding` | not found → audit + exception before yield |
| `test_complete_stream_upstream_timeout_writes_audit` | timeout → `error` in audit |
| `test_complete_stream_upstream_error_writes_audit` | generic error → `error` in audit |
| `test_complete_stream_usage_from_adapter` | usage_out is forwarded to audit |
| `test_complete_stream_cancelled_on_disconnect` | `aclose()` → `status=cancelled` in audit |

### `tests/unit/test_openai_routes.py` — new tests

| Test | What it verifies |
|---|---|
| `test_stream_returns_200_with_sse_content_type` | correct content-type |
| `test_stream_yields_content_chunks_as_sse` | chunks in OpenAI chunk format |
| `test_stream_final_chunk_has_finish_reason_stop` | final chunk with `finish_reason=stop` |
| `test_stream_ends_with_done_sentinel` | `data: [DONE]` at the end |
| `test_stream_blocked_sends_error_sse_event` | errors as SSE events with `error.code` |
| `test_stream_always_ends_with_done_sentinel_on_error` | `[DONE]` even on error |
| `test_non_streaming_route_unaffected_by_streaming_changes` | non-streaming path is not broken |
