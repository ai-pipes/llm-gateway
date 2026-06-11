# Layered Architecture + Body Logging — Design Document

**Date:** 2026-06-09
**Status:** Draft
**Author:** Alexander Melnik
**Version:** v3.0

---

## Context and Purpose

v2.2 of the gateway implements all logic through an ASGI middleware stack. This creates two related defects:

1. **Implicit contract via `request.state`** — `SanitizeMiddleware` writes `state.input_actions`, `GatewayHandler` writes `state.prompt_tokens`, `AuditLogMiddleware` reads all of it. There is no typing, no explicit connection, and fields are easy to lose.

2. **Layer mixing** — `SanitizeMiddleware` simultaneously manipulates the ASGI request body (`request._receive`) and contains business rule logic (chain sanitizers). `AuditLogMiddleware` simultaneously acts as an ASGI wrapper and orchestrates writing compliance data.

Consequence: adding body logging (opt-in recording of prompt/response for compliance) would require threading additional data through `request.state` — which would exacerbate both defects.

v3.0 addresses this in two steps:

1. **Layered Architecture** — explicit layers `api / application / domain / infrastructure`. `ChatService` in the application layer orchestrates the full request lifecycle without HTTP concepts.

2. **Body Logging** — opt-in recording of the full `messages` array (sanitized) and `completion` into `AuditRecord`, enabled via `audit.body_logging.enabled: true`.

Side effect: the architecture is ready for adding the Anthropic Messages API (`POST /v1/messages`) in the future without changes to the application and domain layers.

---

## Scope

**In scope:**
- Restructure: `api/openai/`, `application/`, `domain/`, `infrastructure/`
- `ChatService` — application service, replaces `SanitizeMiddleware` + `AuditLogMiddleware`
- Thin route handler in `api/openai/routes.py`
- Domain exceptions: `SanitizerBlockedError`, `AdapterNotFoundError`, `UpstreamTimeoutError`, `UpstreamError`
- `AuditRecord` extension: fields `messages: list[dict] | None`, `completion: str | None`
- `BodyLoggingConfig`: `body_logging: {enabled: bool}` in each `AuditConfig` variant
- Update `gateway.yaml.example`
- Migrate tests to the new structure
- Update `docs/2026-06-05-llm-gateway-design.md` and `docs/DEVLOG.md`

**Explicitly out of scope:**
- `api/anthropic/` — v4, only the directory structure is conceptually reserved
- Changing public contracts (`BaseLLMAdapter`, `BaseSanitizer`, `BaseAuditBackend`, `BaseAuthMiddleware`)
- Output sanitization (remains no-op)
- Streaming

---

## Architecture

### Layers

```
┌─────────────────────────────────────────────────────┐
│  api/                      Presentation              │
│  HTTP: request parsing, response formatting,         │
│  exception → HTTP code mapping                       │
├─────────────────────────────────────────────────────┤
│  application/              Application               │
│  ChatService: request lifecycle orchestration,       │
│  no HTTP concepts, no knowledge of formats           │
├─────────────────────────────────────────────────────┤
│  domain/                   Domain                    │
│  ChatMessage, ChatRequest, ChatResponse, AuditRecord │
│  BaseSanitizer, SanitizerChain, BaseAuditBackend     │
│  domain exceptions                                   │
├─────────────────────────────────────────────────────┤
│  infrastructure/           Infrastructure            │
│  OpenAICompatibleAdapter, FileAuditBackend,          │
│  StdoutAuditBackend, PiiRegexSanitizer,              │
│  StaticKeyAuthMiddleware, AdapterRegistry            │
└─────────────────────────────────────────────────────┘
```

**Dependency rule:** each layer only knows about the layers below it. `api` depends on `application`, `application` depends on `domain`, `infrastructure` depends on `domain`. `domain` has no dependencies.

`AuthMiddleware` remains an ASGI middleware — it is a genuine cross-cutting concern: it must fire before any code, does not participate in the request body, and must cover all routes automatically. The result (`AuthContext`) is passed to `ChatService` as an explicit parameter.

### Directory Structure

```
gateway/
├── app.py                          # FastAPI setup, middleware registration
├── config.py                       # YAML + env loading, Pydantic models
│
├── api/
│   └── openai/
│       ├── __init__.py
│       ├── routes.py               # POST /v1/chat/completions (thin)
│       └── schemas.py              # parse_request(), format_response()
│
├── application/
│   ├── __init__.py
│   └── chat_service.py             # ChatService
│
├── domain/
│   ├── __init__.py
│   ├── models.py                   # ChatMessage, ChatRequest, ChatResponse, AuditRecord
│   ├── exceptions.py               # SanitizerBlockedError, AdapterNotFoundError, UpstreamError, UpstreamTimeoutError
│   ├── auth/
│   │   ├── __init__.py
│   │   └── base.py                 # BaseAuthProvider (contract), AuthContext (value object)
│   ├── sanitizers/
│   │   ├── __init__.py
│   │   └── base.py                 # BaseSanitizer, SanitizerChain, SanitizeResult
│   └── audit/
│       ├── __init__.py
│       └── base.py                 # BaseAuditBackend
│
└── infrastructure/
    ├── __init__.py
    ├── adapters/
    │   ├── __init__.py
    │   ├── base.py                 # BaseLLMAdapter (contract for plugins)
    │   ├── registry.py             # AdapterRegistry
    │   └── openai_compatible.py
    ├── audit/
    │   ├── __init__.py
    │   ├── record.py               # → moved to domain/models.py
    │   ├── stdout_backend.py
    │   └── file_backend.py
    ├── auth/
    │   ├── __init__.py
    │   └── static_key.py           # StaticKeyAuthMiddleware
    └── sanitizers/
        ├── __init__.py
        ├── passthrough.py
        └── pii_regex.py
```

### ChatService

The central element of v3.0. Owns the full request lifecycle, has no knowledge of HTTP.

```python
# gateway/application/chat_service.py

class ChatService:
    def __init__(
        self,
        input_chain: SanitizerChain,
        output_chain: SanitizerChain,
        registry: AdapterRegistry,
        audit: BaseAuditBackend,
        log_body: bool,
    ):
        self._input = input_chain
        self._output = output_chain
        self._registry = registry
        self._audit = audit
        self._log_body = log_body

    async def complete(
        self,
        raw_messages: list[dict],
        model: str,
        auth: AuthContext,
        request_id: str,
        adapter_name: str | None = None,
    ) -> ChatResponse:
        start = time.monotonic()
        input_actions: list[str] = []
        sanitized_messages: list[dict] = []

        # 1. Input sanitization
        for msg in raw_messages:
            if isinstance(msg.get("content"), str):
                result = await self._input.run(msg["content"])
                input_actions.extend(result.actions)
                if result.blocked:
                    await self._audit.write(AuditRecord(
                        request_id=request_id,
                        timestamp=datetime.now(timezone.utc),
                        api_key_id=auth.key_id,
                        user_id=auth.user_id,
                        team_id=auth.team_id,
                        adapter="unknown",
                        model=model,
                        prompt_tokens=0,
                        completion_tokens=0,
                        latency_ms=_elapsed(start),
                        input_actions=input_actions,
                        output_actions=[],
                        status="blocked",
                        messages=sanitized_messages if self._log_body else None,
                        completion=None,
                    ))
                    raise SanitizerBlockedError(result.block_reason)
                sanitized_messages.append({**msg, "content": result.text})
            else:
                sanitized_messages.append(msg)

        # 2. Adapter resolution
        try:
            adapter = self._registry.get(adapter_name)
        except KeyError:
            await self._audit.write(AuditRecord(
                ..., status="error", error=f"adapter_not_found:{adapter_name}"
            ))
            raise AdapterNotFoundError(adapter_name)

        # 3. LLM call
        chat_request = ChatRequest(
            model=model,
            messages=[ChatMessage(**m) for m in sanitized_messages],
        )
        try:
            response = await adapter.chat(chat_request)
        except httpx.TimeoutException:
            await self._audit.write(AuditRecord(..., status="error", error="upstream_timeout"))
            raise UpstreamTimeoutError()
        except Exception as exc:
            await self._audit.write(AuditRecord(..., status="error", error=str(exc)))
            raise UpstreamError(str(exc))

        # 4. Output sanitization (no-op, v3)
        output_actions: list[str] = []
        completion = response.content

        # 5. Audit
        await self._audit.write(AuditRecord(
            request_id=request_id,
            timestamp=datetime.now(timezone.utc),
            api_key_id=auth.key_id,
            user_id=auth.user_id,
            team_id=auth.team_id,
            adapter=adapter.name,
            model=response.model,
            prompt_tokens=response.usage.get("prompt_tokens", 0),
            completion_tokens=response.usage.get("completion_tokens", 0),
            latency_ms=_elapsed(start),
            input_actions=input_actions,
            output_actions=output_actions,
            status="success",
            messages=sanitized_messages if self._log_body else None,
            completion=completion if self._log_body else None,
        ))

        return response
```

### API Layer (thin route)

```python
# gateway/api/openai/routes.py

@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    request_id = str(uuid.uuid4())

    try:
        response = await chat_service.complete(
            raw_messages=body.get("messages", []),
            model=body.get("model", ""),
            auth=request.state.auth,
            request_id=request_id,
            adapter_name=body.get("adapter"),
        )
    except SanitizerBlockedError as e:
        return JSONResponse(_blocked_error(str(e)), status_code=400)
    except AdapterNotFoundError as e:
        return JSONResponse(_adapter_error(str(e)), status_code=400)
    except UpstreamTimeoutError:
        return JSONResponse(_timeout_error(), status_code=504)
    except UpstreamError as e:
        return JSONResponse(_upstream_error(str(e)), status_code=502)

    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "model": response.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": response.content}, "finish_reason": "stop"}],
        "usage": response.usage,
    }
```

---

## Body Logging

### Config

```python
# gateway/config.py

class BodyLoggingConfig(BaseModel):
    enabled: bool = False

class StdoutAuditConfig(BaseModel):
    type: Literal["stdout"] = "stdout"
    body_logging: BodyLoggingConfig = BodyLoggingConfig()

class FileAuditConfig(BaseModel):
    type: Literal["file"]
    path: str = Field(min_length=1)
    body_logging: BodyLoggingConfig = BodyLoggingConfig()

class PluginAuditConfig(BaseModel):
    type: Literal["plugin"]
    module: str
    config: dict = Field(default_factory=dict)
    body_logging: BodyLoggingConfig = BodyLoggingConfig()
```

```yaml
# gateway.yaml.example
audit:
  type: file
  path: "/var/log/gateway/audit.jsonl"
  body_logging:
    enabled: true   # compliance mode
```

### AuditRecord

```python
@dataclass
class AuditRecord:
    # ... all existing fields unchanged ...
    error: str | None = None

    # body logging — None when disabled
    messages: list[dict] | None = None    # sanitized incoming messages
    completion: str | None = None         # LLM response (content string)
```

### What Gets Logged

| Situation | `messages` | `completion` |
|----------|-----------|--------------|
| `status: success`, `log_body: true` | full sanitized array | full response text |
| `status: blocked`, `log_body: true` | messages up to the point of blocking | `null` |
| `status: error`, `log_body: true` | sanitized messages (if reached) | `null` |
| `log_body: false` (default) | `null` | `null` |

**Privacy guarantee is preserved:** only sanitized data ever reaches the log — `ChatService` runs `SanitizerChain` before writing to audit.

### Example JSON Line with `log_body: true`

```json
{
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "timestamp": "2026-06-09T14:32:01.123456+00:00",
  "api_key_id": "sha256:abc123",
  "user_id": "dev",
  "team_id": "engineering",
  "adapter": "openai",
  "model": "gpt-4o",
  "prompt_tokens": 412,
  "completion_tokens": 87,
  "latency_ms": 1243,
  "input_actions": ["replaced:EMAIL"],
  "output_actions": [],
  "status": "success",
  "error": null,
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Summarize the contract for [EMAIL REDACTED]"}
  ],
  "completion": "The contract covers three key areas: delivery terms, payment schedule, and liability caps."
}
```

---

## Domain Exceptions

```python
# gateway/domain/exceptions.py

class GatewayError(Exception):
    pass

class SanitizerBlockedError(GatewayError):
    def __init__(self, reason: str):
        self.reason = reason

class AdapterNotFoundError(GatewayError):
    def __init__(self, name: str | None):
        self.name = name

class UpstreamTimeoutError(GatewayError):
    pass

class UpstreamError(GatewayError):
    pass
```

---

## Error Handling

| Exception | HTTP | AuditRecord.status |
|-----------|------|-------------------|
| `SanitizerBlockedError` | 400 | `blocked` |
| `AdapterNotFoundError` | 400 | `error` |
| `UpstreamTimeoutError` | 504 | `error` |
| `UpstreamError` | 502 | `error` |
| `BaseAuditBackend.write()` throws | 500 | — (cannot log itself) |

Last case: `ChatService` does not catch exceptions from `audit.write()`. They propagate to the route handler and become a 500. The compliance guarantee is preserved: if audit fails — the client does not receive a response.

---

## Extensibility: Future Anthropic API

`ChatService.complete()` accepts `raw_messages: list[dict]` — a protocol-agnostic format. Adding `POST /v1/messages`:

1. Create `api/anthropic/routes.py` — parse the Anthropic format (system as a separate field, content blocks) into `list[dict]`
2. Call the same `chat_service.complete()`
3. Format `ChatResponse` into the Anthropic response format

`ChatService`, `domain/`, `infrastructure/` — unchanged.

---

## Testing

### Unit — `tests/unit/test_chat_service.py`

| Test | What it verifies |
|------|---------------|
| `test_success_no_body_logging` | success flow, `messages=None`, `completion=None` in the record |
| `test_success_with_body_logging` | `log_body=True` → messages and completion in the record |
| `test_blocked_writes_audit` | sanitizer blocked → audit written, `SanitizerBlockedError` raised |
| `test_blocked_body_logging_partial` | blocked on the third message → messages contain only the first two |
| `test_adapter_not_found` | KeyError from registry → `AdapterNotFoundError`, audit written |
| `test_upstream_timeout` | `httpx.TimeoutException` → `UpstreamTimeoutError`, audit written |
| `test_upstream_error` | generic Exception → `UpstreamError`, audit written |
| `test_audit_failure_propagates` | `audit.write()` throws → exception is not caught |

### Unit — `tests/unit/test_openai_routes.py`

| Test | What it verifies |
|------|---------------|
| `test_success_response_format` | correct OpenAI response format |
| `test_blocked_returns_400` | `SanitizerBlockedError` → 400 with correct error code |
| `test_adapter_not_found_returns_400` | `AdapterNotFoundError` → 400 |
| `test_timeout_returns_504` | `UpstreamTimeoutError` → 504 |
| `test_upstream_error_returns_502` | `UpstreamError` → 502 |

### Integration — `tests/integration/test_full_stack.py`

Full stack via FastAPI `TestClient` with mock LLM adapter.

| Test | What it verifies |
|------|---------------|
| `test_success_audit_written` | audit record appeared, `status=success` |
| `test_body_logging_enabled` | `log_body=True` → messages and completion in the record |
| `test_body_logging_disabled` | `log_body=False` → messages=None, completion=None |
| `test_blocked_audit_written` | blocked request → audit record `status=blocked` |
| `test_no_auth_returns_401` | missing key → 401, audit NOT written |

---

## Migration from v2.2

### Delete

| File | Reason |
|------|---------|
| `gateway/middleware/sanitize.py` | logic moves to `ChatService` |
| `gateway/middleware/audit.py` | logic moves to `ChatService` |

### Move

| From | To |
|--------|------|
| `gateway/middleware/auth.py` → `AuthMiddleware` + `StaticKeyAuthProvider` | `gateway/infrastructure/auth/static_key.py` |
| `gateway/middleware/auth.py` → `BaseAuthProvider` + `AuthContext` | `gateway/domain/auth/base.py` |
| `gateway/audit/record.py` | `gateway/domain/models.py` (merge with ChatRequest/ChatResponse) |
| `gateway/audit/base.py` | `gateway/domain/audit/base.py` |
| `gateway/audit/stdout_backend.py` | `gateway/infrastructure/audit/stdout_backend.py` |
| `gateway/audit/file_backend.py` | `gateway/infrastructure/audit/file_backend.py` |
| `gateway/adapters/base.py` | `gateway/infrastructure/adapters/base.py` (BaseLLMAdapter — contract for plugins) |
| `gateway/adapters/registry.py` | `gateway/infrastructure/adapters/registry.py` |
| `gateway/adapters/openai_compatible.py` | `gateway/infrastructure/adapters/openai_compatible.py` |
| `gateway/sanitizers/base.py` | `gateway/domain/sanitizers/base.py` |
| `gateway/sanitizers/passthrough.py` | `gateway/infrastructure/sanitizers/passthrough.py` |
| `gateway/sanitizers/pii_regex.py` | `gateway/infrastructure/sanitizers/pii_regex.py` |
| `gateway/routes.py` | `gateway/api/openai/routes.py` (simplified) |

### Modify

| File | What changes |
|------|-------------|
| `gateway/config.py` | add `BodyLoggingConfig` to each `AuditConfig` variant |
| `gateway/app.py` | remove `SanitizeMiddleware` and `AuditLogMiddleware`, create `ChatService`, pass to router |
| `gateway/domain/models.py` | add `messages` and `completion` to `AuditRecord` |
| `gateway.yaml.example` | add `body_logging` example |

### Breaking Change for Plugin Authors

Base classes (`BaseLLMAdapter`, `BaseSanitizer`, `BaseAuditBackend`) change their import paths:

```python
# before
from gateway.adapters.base import BaseLLMAdapter
from gateway.sanitizers.base import BaseSanitizer
from gateway.audit.base import BaseAuditBackend

# after
from gateway.infrastructure.adapters.base import BaseLLMAdapter
from gateway.domain.sanitizers.base import BaseSanitizer
from gateway.domain.audit.base import BaseAuditBackend
```

Documented in DEVLOG as a breaking change in v3.0.

---

## Files

| Action | File |
|----------|------|
| Create | `gateway/api/openai/__init__.py` |
| Create | `gateway/api/openai/routes.py` |
| Create | `gateway/api/openai/schemas.py` |
| Create | `gateway/application/__init__.py` |
| Create | `gateway/application/chat_service.py` |
| Create | `gateway/domain/__init__.py` |
| Create | `gateway/domain/models.py` |
| Create | `gateway/domain/exceptions.py` |
| Create | `gateway/domain/auth/__init__.py` |
| Create | `gateway/domain/auth/base.py` |
| Create | `gateway/domain/sanitizers/__init__.py` |
| Create | `gateway/domain/sanitizers/base.py` |
| Create | `gateway/domain/audit/__init__.py` |
| Create | `gateway/domain/audit/base.py` |
| Create | `gateway/infrastructure/__init__.py` |
| Create | `gateway/infrastructure/adapters/__init__.py` |
| Create | `gateway/infrastructure/adapters/base.py` |
| Create | `gateway/infrastructure/adapters/registry.py` |
| Create | `gateway/infrastructure/adapters/openai_compatible.py` |
| Create | `gateway/infrastructure/audit/__init__.py` |
| Create | `gateway/infrastructure/audit/stdout_backend.py` |
| Create | `gateway/infrastructure/audit/file_backend.py` |
| Create | `gateway/infrastructure/auth/__init__.py` |
| Create | `gateway/infrastructure/auth/static_key.py` |
| Create | `gateway/infrastructure/sanitizers/__init__.py` |
| Create | `gateway/infrastructure/sanitizers/passthrough.py` |
| Create | `gateway/infrastructure/sanitizers/pii_regex.py` |
| Modify | `gateway/app.py` |
| Modify | `gateway/config.py` |
| Delete | `gateway/middleware/sanitize.py` |
| Delete | `gateway/middleware/audit.py` |
| Delete | `gateway/middleware/__init__.py` (if empty after deletion) |
| Delete | `gateway/audit/` (moved) |
| Delete | `gateway/adapters/` (moved) |
| Delete | `gateway/sanitizers/` (moved) |
| Delete | `gateway/routes.py` (moved) |
| Create | `tests/unit/test_chat_service.py` |
| Create | `tests/unit/test_openai_routes.py` |
| Modify | `tests/unit/test_audit.py` → update imports |
| Modify | `tests/unit/test_config.py` → add `BodyLoggingConfig` tests |
| Modify | `tests/integration/test_middleware_stack.py` → update for new structure |
| Modify | `gateway.yaml.example` |
| Modify | `docs/2026-06-05-llm-gateway-design.md` |
| Modify | `docs/DEVLOG.md` |
