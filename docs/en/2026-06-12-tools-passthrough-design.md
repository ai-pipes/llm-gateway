# Tools Passthrough — Design Document

**Date:** 2026-06-12
**Status:** Implemented
**Author:** Alexander Melnik
**Version:** v3.4

---

## Context and Purpose

The gateway proxies chat requests to upstream LLMs. Before this version, it only supported plain text messages (`role: user/assistant/system`). Many clients — agent frameworks, MCP adapters, coding assistants — rely on the OpenAI `tools` API for structured function calling.

Goal: make the gateway transparent to `tools` so clients can declare their own tool schemas and handle tool execution themselves. The gateway does not run an agentic loop.

---

## Architecture Decision: Client-Owned Tool Logic

The key question was who should know about tools and their schemas.

**Option A: Gateway registers tools**
The gateway holds a registry of known tools (e.g. Sentry MCP, internal APIs) and injects them based on client identity or config. Simple from the client side, but: the gateway becomes a monolith, every new tool requires a gateway deploy, tool schemas drift from the actual implementations.

**Option B: Client sends tool schemas with each request (chosen)**
The client passes the full OpenAI-compatible `tools` array in every request. The gateway proxies it to the LLM without inspection. The LLM returns `tool_calls`; the gateway returns them to the client. The client executes the tool and sends the next request with a `role: "tool"` message.

Chose B. The gateway stays a thin proxy. Clients own their tools. Adding a new MCP adapter requires zero gateway changes.

---

## Data Flow

### Non-streaming request with tools

```
Client → POST /v1/chat/completions  { tools: [...], messages: [...] }
         ↓
routes.py        extracts tools, passes to chat_service.complete(tools=...)
         ↓
chat_service     sanitizes messages, assembles ChatRequest(tools=tools)
         ↓
adapter          adds tools to upstream payload, parses tool_calls from response
         ↓
routes.py        if response.tool_calls:
                   finish_reason = "tool_calls"
                   message = {role: "assistant", content: null, tool_calls: [...]}
         ↓
Client ← { choices: [{ message: {...}, finish_reason: "tool_calls" }] }
```

### Client-side agentic loop

The gateway is stateless. After receiving `tool_calls`, the client:
1. Executes the tool locally
2. Sends a new request with the full message history plus:
   - `{"role": "assistant", "tool_calls": [...]}`
   - `{"role": "tool", "tool_call_id": "...", "content": "<result>"}`

The gateway sees this as a normal multi-message request.

### Streaming with tool_calls

The OpenAI streaming protocol sends `tool_calls` via `delta.tool_calls` instead of `delta.content`. The adapter yields two chunk types:

- `str` — text content (existing behaviour)
- `dict` — `{"tool_calls": [...]}` delta when the model is calling a tool

`routes.py` handles both. It also tracks whether any tool_call delta was seen to set the final `finish_reason` correctly (`"tool_calls"` vs `"stop"`).

---

## PII Restoration in Tool Call Arguments

### The Problem

The input sanitizer replaces PII before the LLM sees it:
```
"Send email to john@example.com" → "Send email to [EMAIL_ADDRESS_3f2a1b0c]"
```

The LLM may echo the placeholder into a tool call argument:
```json
{"function": {"name": "send_email", "arguments": "{\"to\": \"[EMAIL_ADDRESS_3f2a1b0c]\"}"}}
```

Without restoration, the client receives the placeholder instead of the original value, which breaks the tool call.

### Non-streaming fix

After receiving the adapter response, if `context.has_replacements()`:

```python
if response.tool_calls is not None:
    restored = json.loads(context.restore(json.dumps(response.tool_calls)))
    response = dataclasses.replace(response, tool_calls=restored)
```

Serialize to JSON string, run `context.restore()` (simple string replace), parse back. Works because placeholders are contiguous strings within the serialized JSON.

### Streaming fix: per-index StreamingRestorer

In streaming, the LLM sends `arguments` character by character across many SSE chunks:

```
arguments: "{"    arguments: "\"to\": \""    arguments: "["
arguments: "EMAIL"    arguments: "_ADDRESS_3f2a1b0c]"    arguments: "\""
```

The placeholder is split across chunks. A per-chunk `str.replace()` won't find it.

**Solution:** apply the same `StreamingRestorer` logic used for text content, but per `tool_call index`. Each index gets its own `StreamingRestorer` instance that buffers only around `[PLACEHOLDER]` boundaries:

- Characters before `[` → forwarded immediately
- After `[` → buffered while checking if it could be a placeholder prefix
- When a complete placeholder is matched → emit the original value in one chunk
- When the prefix can't match → emit `[` immediately and continue

This gives true streaming: argument fragments appear at the client in real time, with a hold-back bounded by the placeholder length (~26 chars) only around the placeholder itself.

```python
tc_restorers: dict[int, StreamingRestorer] = {}

for tc in chunk.get("tool_calls", []):
    idx = tc.get("index", 0)
    if "arguments" in tc.get("function", {}):
        if idx not in tc_restorers:
            tc_restorers[idx] = StreamingRestorer(context)
        safe = tc_restorers[idx].feed(func["arguments"])
        # yield chunk with safe fragment — may be empty while buffering
```

After the stream loop, `finalize()` is called on each restorer to flush any remaining buffer (e.g. a trailing `[` that arrived at stream end).

### What about `[` and `]` in normal text?

The `StreamingRestorer._probe()` only buffers when the buffer is a **prefix** of a registered placeholder. `[category]` triggers buffering on `[`, then when `c` arrives it's clear this can't match `[EMAIL_ADDRESS_...]` → `[` is emitted immediately. One-chunk delay on `[`, nothing lost.

A false positive (LLM generating a string identical to a registered placeholder) is theoretically possible but astronomically unlikely: placeholders use `secrets.token_hex(4)` — a 4-byte random suffix, 2³² variants.

---

## Domain Model Changes

```python
@dataclass
class ChatMessage:
    role: str
    content: str | None = None        # None for assistant messages with only tool_calls
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None

@dataclass
class ChatRequest:
    model: str
    messages: list[ChatMessage]
    temperature: float = 0.7
    stream: bool = False
    tools: list[dict] | None = None   # opaque passthrough

@dataclass
class ChatResponse:
    model: str
    usage: dict
    content: str | None = None
    tool_calls: list[dict] | None = None
```

All new fields are optional with `None` defaults — no existing callers break.

---

## Explicitly Out of Scope

- Gateway-side tool execution (no agentic loop)
- Sanitizing `function.arguments` for PII on input (client-sent tool schemas)
- Validating tool schemas (treated as opaque blobs)
- Logging `tool_calls` in `AuditRecord`
- `tool_choice` parameter passthrough (trivial to add, not needed yet)
