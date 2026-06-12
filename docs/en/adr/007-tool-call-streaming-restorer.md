# ADR-007: Per-Index StreamingRestorer for Tool Call Argument Streaming

**Date:** 2026-06-12  
**Status:** Accepted

## Context

The gateway restores PII placeholders in `tool_call` arguments before returning them to the client. In the non-streaming path this is trivial: serialize `tool_calls` to JSON, call `context.restore()`, parse back.

In the streaming path, `function.arguments` arrives as a partial JSON string split across many SSE chunks — often one or a few characters per event. A placeholder like `[EMAIL_ADDRESS_3f2a1b0c]` may be delivered as `[`, `EMAIL`, `_ADDRESS_3`, `f2a1b0c]` across four separate events. A per-chunk `str.replace()` won't find it.

## Options Considered

**A. Buffer all tool_call deltas, emit one consolidated chunk at end**  
Accumulate all argument fragments per `tool_call index` during the stream. After the stream ends, run `context.restore()` on the assembled string, yield one delta with the complete restored arguments.

Simple to implement. But: all tool_call argument chunks are withheld until the stream ends. The client receives no tool_call streaming — it sees one chunk arrive at the very end, after the LLM has finished generating. For text content the client gets real streaming; for tool_calls it gets a single late burst.

**B. Per-chunk restore with raw string replacement**  
Apply `context.restore()` to each delta chunk as it arrives. Works only if the entire placeholder arrives in a single chunk. Fails silently when the placeholder is split — the client receives the raw placeholder instead of the original value.

Not viable for production: correctness depends on LLM tokenizer behaviour, which is not guaranteed.

**C. Per-index StreamingRestorer — same look-ahead buffer as text (chosen)**  
Create one `StreamingRestorer` instance per `tool_call index`. As each argument fragment arrives, feed it into the corresponding restorer. The restorer emits safe bytes immediately and buffers only when it detects the start of a potential placeholder (`[`). When a complete placeholder is matched, the original value is emitted in one chunk. When the buffer cannot match any registered placeholder, `[` is flushed immediately.

## Decision

**C. Per-index StreamingRestorer.**

Option A was implemented first and then replaced. The consolidated-chunk approach worked correctly but broke tool_call streaming when PII was present — defeating the purpose of streaming in the first place.

## Rationale

`StreamingRestorer` (v3.3) already solves the identical problem for text content. Applying the same mechanism per `tool_call index` reuses the proven implementation with no new logic. The maximum hold-back is bounded by the longest registered placeholder (~26 chars) — a delay invisible to the client.

The key insight that makes option C correct: OpenAI streaming clients must buffer `function.arguments` fragments themselves before parsing the JSON anyway. A one-fragment hold-back during placeholder detection is undetectable in practice.

Option A degrades streaming to batch delivery for any request where PII is present. For short tool calls this may be acceptable; for tool calls with large argument sets it introduces a visible delay. The degradation is also invisible — there is no signal to the client that streaming has been suspended.

## Consequences

- Argument fragments arrive at the client in real time; hold-back is bounded by placeholder length only
- Chunks may carry empty `arguments` strings during the buffering window — clients already handle empty argument fragments correctly per the OpenAI streaming spec
- `finalize()` is called on each per-index restorer after the stream loop to flush any remaining buffer (e.g. a trailing `[` at stream end)
- A literal `[` in tool_call arguments causes a one-event delay; it is emitted immediately when the next character shows the buffer cannot match any registered placeholder
- False positive risk (LLM generates a string identical to a registered placeholder) is negligible: placeholders use a 4-byte random hex suffix (`secrets.token_hex(4)`), giving 2³² possible values per entity type
