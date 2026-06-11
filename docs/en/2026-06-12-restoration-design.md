# PII Restoration — Design Document

**Date:** 2026-06-12
**Status:** Implemented
**Author:** Alexander Melnik
**Version:** v3.2 / v3.3

---

## Context and Purpose

The gateway's input sanitizer masks PII before it reaches the LLM — emails, phone numbers, card numbers become tokens like `[EMAIL_ADDRESS]`. The problem: the LLM response also contains those tokens. The client sends a real email address and gets a placeholder back.

Goal: restore original values in the LLM response so the client never sees placeholders, without ever sending the real data to the LLM.

v3.2 covers non-streaming (`complete()`). v3.3 adds streaming via a look-ahead buffer in `StreamingRestorer`.

---

## Key Design Decision: RestorationContext

The core question was where to store the `placeholder → original` mapping created during sanitization, and how to pass it to the restoration step after the response.

### Two Options

**Option A: Map on the request object**
Store the map in `request.state` (FastAPI) or some shared dict. Simple to implement, but: the mapping is tied to the request lifecycle, harder to test in isolation, mixes concerns between the HTTP layer and the application layer.

**Option B: Separate `RestorationContext` object (chosen)**
Create a `RestorationContext` at the start of `ChatService.complete()`, pass it down into the sanitizer chain, collect the mapping there, and restore the response at the end.

Chose B. The context object has a clear lifecycle (one per `complete()` call), is trivially testable, and keeps the HTTP and application layers decoupled.

---

## How It Works

```
complete(raw_messages)
    │
    ├─ context = RestorationContext()          # empty map
    │
    ├─ _sanitize_input(messages, context)
    │       │
    │       └─ for each message content:
    │               sanitizer.sanitize(text, context)
    │                   → replaces PII with unique placeholders
    │                   → registers each: context.register(original, entity_type)
    │
    ├─ if context.has_replacements():
    │       inject system instruction into messages
    │       "preserve placeholders — do not modify them"
    │
    ├─ adapter.chat(sanitized_messages)        # LLM sees only placeholders
    │       → response.content may contain placeholders
    │
    ├─ sanitized_content = response.content    # save for audit BEFORE restoring
    │
    ├─ if context.has_replacements():
    │       response = replace(response, content=context.restore(response.content))
    │
    └─ audit.write(completion=sanitized_content)   # audit never logs raw PII
```

---

## RestorationContext

```python
class RestorationContext:
    def register(self, original: str, entity_type: str) -> str:
        # dedup: same original → same placeholder
        if original in self._reverse:
            return self._reverse[original]
        placeholder = f"[{entity_type.upper()}_{secrets.token_hex(4)}]"
        self._map[placeholder] = original
        self._reverse[original] = placeholder
        return placeholder

    def restore(self, text: str) -> str:
        for placeholder, original in self._map.items():
            text = text.replace(placeholder, original)
        return text
```

**Deduplication:** if the same email appears twice in a message, it gets the same placeholder — the LLM receives clean, non-redundant context and the mapping stays compact.

**Uniqueness:** `secrets.token_hex(4)` gives 8 hex characters (4 bytes = 2³² possibilities). Unguessable. Two different values always get different placeholders even for the same entity type.

**Placeholder format:** `[EMAIL_ADDRESS_3f2a1b0c]`. Uppercase entity type, lowercase hex suffix, square brackets. Presidio entity type names are used as-is (`EMAIL_ADDRESS`, `PHONE_NUMBER`, `CREDIT_CARD`), regex sanitizer labels are uppercased (`EMAIL`, `PHONE`, `CARD`).

---

## System Instruction Injection

When at least one replacement is registered, a system instruction is prepended to the messages (or appended to an existing system message):

> IMPORTANT: Some values in this message have been replaced with opaque placeholder tokens matching the pattern [TYPENAME_HEXCHARS] (e.g. [EMAIL_ADDRESS_3f2a1b0c], [PHONE_NUMBER_9d4e7a12]). These are NOT real values — do not invent, guess, or reconstruct the originals. Preserve every such token exactly as written — do not modify, translate, paraphrase, or remove them.

**Key constraint:** the examples in the instruction must look clearly fake. During testing, the LLM (gpt-4o-mini) copied a real-looking placeholder from the example verbatim into its response — using it as a generic template instead of the registered one. Examples like `[EMAIL_ADDRESS_3f2a1b0c]` with a note "These are NOT real values" resolved this.

**Injection logic:**
```python
def _inject_system_instruction(messages, instruction):
    for i, msg in enumerate(messages):
        if msg.get("role") == "system" and isinstance(msg.get("content"), str):
            # append to existing system message
            updated = {**msg, "content": msg["content"] + "\n\n" + instruction}
            return messages[:i] + [updated] + messages[i + 1:]
    # no system message — prepend a new one
    return [{"role": "system", "content": instruction}] + messages
```

---

## Audit Security

The audit record must not contain raw PII — that would defeat the purpose of sanitization.

The sequence in `complete()`:
```python
sanitized_content = response.content          # 1. save placeholder version
if context.has_replacements():
    response = replace(response, content=context.restore(response.content))
                                              # 2. restore for the client
await audit.write(..., completion=sanitized_content)
                                              # 3. audit gets placeholder version
```

The order is critical. `sanitized_content` is captured before `context.restore()` runs. The audit always receives the placeholder version, even if the client receives the restored one.

---

## Sanitizer Changes

### `BaseSanitizer` and `SanitizerChain`

Added an optional `context: RestorationContext | None = None` parameter to `sanitize()` and `SanitizerChain.run()`. Default is `None` — backward compatible. Existing sanitizers that ignore context continue to work unchanged.

The import uses `TYPE_CHECKING` to avoid a circular dependency:
```python
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from gateway.domain.sanitizers.restoration import RestorationContext
```

### `PiiRegexSanitizer`

Uses `re.sub` with a callback instead of a fixed replacement string:

```python
def replacer(m, _label=label):   # default arg to avoid late-binding closure bug
    if context:
        return context.register(m.group(), _label)
    return f"[{_label}]"
new_text = pattern.sub(replacer, text)
```

The `_label=label` default argument is necessary: without it, all closures in the loop would capture the same final value of `label` (Python late-binding).

### `PresidioSanitizer`

When `context` is provided, bypasses `AnonymizerEngine.anonymize()` and performs manual right-to-left span replacement. Right-to-left preserves earlier indices: when a span is replaced with a longer placeholder, subsequent spans at smaller positions are unaffected.

**Problem discovered:** Presidio sometimes returns overlapping spans. For example, `sarah@techcorp.io` is detected as both `EMAIL_ADDRESS` (score 1.0) and `URL` for `techcorp.io` (score 0.5). Right-to-left replacement processes `URL` first (higher start index), inserts `[URL_xxxxxxxx]` into the string, then tries to replace `EMAIL_ADDRESS` at the now-shifted original indices — producing garbage like `[EMAIL_ADDRESS_xxxxxxxx]a1]`.

**Fix:** `_resolve_conflicts()` removes overlapping spans before replacement, keeping the highest-confidence (then longest) span per conflict:

```python
def _resolve_conflicts(results):
    by_priority = sorted(results, key=lambda r: (r.score, r.end - r.start), reverse=True)
    kept = []
    for r in by_priority:
        if not any(max(r.start, k.start) < min(r.end, k.end) for k in kept):
            kept.append(r)
    return kept
```

`EMAIL_ADDRESS` (score 1.0) beats `URL` (score 0.5) → only one replacement, no corruption.

---

## Streaming Restoration — `StreamingRestorer`

Chunk-by-chunk string replacement is not feasible: `[EMAIL_ADDRESS_3f2a1b0c]` is 26 characters and may arrive split across multiple SSE chunks. Buffering the full response before restoring would destroy TTFF.

### Look-Ahead Buffer Algorithm

`StreamingRestorer` maintains a small sliding buffer (`_buffer: str`) at the tail of the stream. The invariant: everything before the buffer has already been forwarded to the client.

```
incoming chunks → _buffer → _drain() → client
```

`_drain()` loop:

1. Scan `_buffer` for `[`. Everything before it is safe — forward immediately.
2. From `[` onward: call `_probe()`.
3. `_probe()` checks:
   - Does `_buffer` start with a known placeholder? → restore, consume, loop.
   - Is `_buffer` a valid prefix of any registered placeholder? → stop, wait for next chunk.
   - Otherwise → flush `[`, advance by one character, loop.

```python
def _probe(self) -> str | None:
    buf = self._buffer
    for placeholder, original in self._map.items():
        if buf.startswith(placeholder):          # complete match
            self._buffer = buf[len(placeholder):]
            return original
    if any(p.startswith(buf) for p in self._map):  # still a valid prefix
        if len(buf) > self._max_len:               # safety guard
            self._buffer = buf[1:]; return "["
        return None                                # need more data
    self._buffer = buf[1:]; return "["             # not a placeholder
```

`finalize()` is called after the stream ends and flushes whatever remains in the buffer as-is (a partial placeholder that was never completed arrives unchanged — better than swallowing it).

### Max Hold-Back

The buffer grows only while `_buffer` remains a valid prefix of a known placeholder. The longest possible hold-back equals the length of the longest registered placeholder — typically 26 characters (`[EMAIL_ADDRESS_xxxxxxxx]`). Everything else passes through immediately.

### Integration in `complete_stream()`

`complete_stream()` now mirrors `complete()`:

```python
context = RestorationContext()
sanitized_messages, input_actions = await self._sanitize_input(..., context=context)
if context.has_replacements():
    sanitized_messages = _inject_system_instruction(sanitized_messages, ...)

restorer = StreamingRestorer(context) if context.has_replacements() else None

async for chunk in adapter.stream_chat(...):
    chunks.append(chunk)               # raw, for audit (placeholder version)
    if restorer:
        safe = restorer.feed(chunk)
        if safe:
            yield safe
    else:
        yield chunk

if restorer:
    tail = restorer.finalize()
    if tail:
        yield tail
```

`chunks` stores raw (placeholder) content for the audit body log — the restored email never reaches the audit record.

---

## What Is Not Restored

**Output sanitizer:** the output sanitizer was never in the critical path for `complete()` (pre-existing gap). Restoration only applies to `response.content` from the adapter.

**Blocked requests:** if the input sanitizer blocks the request, no LLM call is made and there is nothing to restore.

---

## Testing

### `tests/unit/test_restoration_context.py` (new)

| Test | What it verifies |
|---|---|
| `test_register_returns_placeholder_with_correct_format` | format `[TYPE_xxxxxxxx]` |
| `test_register_same_value_returns_same_placeholder` | deduplication |
| `test_register_different_values_return_different_placeholders` | uniqueness |
| `test_restore_replaces_placeholder_with_original` | basic restore |
| `test_restore_multiple_values` | multiple values in one string |
| `test_restore_no_replacements_returns_text_unchanged` | noop when empty |
| `test_has_replacements_false_initially` | empty context |
| `test_has_replacements_true_after_register` | after registration |
| `test_build_system_instruction_is_non_empty_string` | instruction exists |
| `test_build_system_instruction_describes_pattern` | contains "placeholder" |

### `tests/unit/test_pii_sanitizer.py` — new context-aware tests

| Test | What it verifies |
|---|---|
| `test_with_context_registers_replacement` | context is populated on match |
| `test_with_context_placeholder_in_result` | placeholder appears in output |
| `test_with_context_restore_roundtrip` | sanitize → restore = original |
| `test_with_context_duplicate_value_same_placeholder` | dedup via context |
| `test_without_context_uses_fixed_label` | backward compat — `[EMAIL]` format |

### `tests/unit/test_presidio_sanitizer.py` — new context-aware and conflict tests

| Test | What it verifies |
|---|---|
| `test_with_context_registers_span_and_replaces` | span replaced with placeholder |
| `test_with_context_restore_roundtrip` | restore after sanitize |
| `test_with_context_two_spans_replaced_correctly` | two non-overlapping spans |
| `test_with_context_overlapping_spans_keeps_higher_confidence` | email wins over url |

### `tests/unit/test_chat_service.py` — new restoration tests

| Test | What it verifies |
|---|---|
| `test_complete_restores_pii_in_response` | placeholder → original in response |
| `test_complete_audit_receives_sanitized_not_restored` | audit gets placeholder version |
| `test_complete_no_restoration_when_no_replacements` | noop when context empty |
| `test_complete_injects_system_instruction_when_replacements` | instruction injected |
| `test_complete_no_system_instruction_when_no_replacements` | no injection if no PII |
| `test_complete_stream_restores_pii_in_output` | placeholder restored across split chunks |
| `test_complete_stream_audit_receives_placeholder_not_original` | audit gets placeholder in stream |
| `test_complete_stream_injects_system_instruction_when_replacements` | instruction injected in stream |

### `tests/unit/test_restoration_context.py` — StreamingRestorer tests

| Test | What it verifies |
|---|---|
| `test_streaming_restorer_passthrough_no_brackets` | non-PII text forwarded immediately |
| `test_streaming_restorer_complete_placeholder_in_one_chunk` | restore in single chunk |
| `test_streaming_restorer_holds_partial_then_restores` | hold-back then restore on completion |
| `test_streaming_restorer_char_by_char` | placeholder split into single characters |
| `test_streaming_restorer_non_matching_bracket_flushed_immediately` | `[not a placeholder]` passes through |
| `test_streaming_restorer_multiple_placeholders_in_stream` | two placeholders in one stream |
| `test_streaming_restorer_finalize_flushes_incomplete_buffer` | partial at end flushed as-is |
| `test_streaming_restorer_empty_context_is_noop` | no registrations → no buffering |
