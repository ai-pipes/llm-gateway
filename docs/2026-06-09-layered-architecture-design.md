# Layered Architecture + Body Logging — Design Document

**Date:** 2026-06-09
**Status:** Draft
**Author:** Alexander Melnik
**Version:** v3.0

---

## Контекст и цель

v2.2 gateway реализует всю логику через ASGI middleware stack. Это создаёт два связанных дефекта:

1. **Неявный контракт через `request.state`** — `SanitizeMiddleware` пишет `state.input_actions`, `GatewayHandler` пишет `state.prompt_tokens`, `AuditLogMiddleware` читает всё это. Нет типизации, нет явной связи, легко потерять поле.

2. **Смешение слоёв** — `SanitizeMiddleware` одновременно манипулирует ASGI-телом запроса (`request._receive`) и содержит логику бизнес-правил (chain sanitizers). `AuditLogMiddleware` одновременно является ASGI-обёрткой и оркестрирует запись compliance-данных.

Следствие: добавление body logging (opt-in запись prompt/response для compliance) потребовало бы threading дополнительных данных через `request.state` — что усугубляет оба дефекта.

v3.0 решает это двумя шагами:

1. **Layered Architecture** — явные слои `api / application / domain / infrastructure`. `ChatService` в application-слое оркестрирует весь lifecycle запроса без HTTP-концепций.

2. **Body Logging** — opt-in запись полного массива `messages` (санитайзированных) и `completion` в `AuditRecord`, включаемая через `audit.body_logging.enabled: true`.

Побочный эффект: архитектура готова к добавлению Anthropic Messages API (`POST /v1/messages`) в будущем без изменений в application и domain слоях.

---

## Скоуп

**В scope:**
- Restructure: `api/openai/`, `application/`, `domain/`, `infrastructure/`
- `ChatService` — application service, заменяет `SanitizeMiddleware` + `AuditLogMiddleware`
- Тонкий route handler в `api/openai/routes.py`
- Доменные исключения: `SanitizerBlockedError`, `AdapterNotFoundError`, `UpstreamTimeoutError`, `UpstreamError`
- Расширение `AuditRecord`: поля `messages: list[dict] | None`, `completion: str | None`
- `BodyLoggingConfig`: `body_logging: {enabled: bool}` в каждом `AuditConfig`-варианте
- Обновление `gateway.yaml.example`
- Перенос тестов под новую структуру
- Обновление `docs/2026-06-05-llm-gateway-design.md` и `docs/DEVLOG.md`

**Явно вне scope:**
- `api/anthropic/` — v4, только структура директории зарезервирована концептуально
- Изменение публичных контрактов (`BaseLLMAdapter`, `BaseSanitizer`, `BaseAuditBackend`, `BaseAuthMiddleware`)
- Output sanitization (остаётся no-op)
- Streaming

---

## Архитектура

### Слои

```
┌─────────────────────────────────────────────────────┐
│  api/                      Presentation              │
│  HTTP: парсинг запроса, форматирование ответа,       │
│  маппинг исключений → HTTP-коды                      │
├─────────────────────────────────────────────────────┤
│  application/              Application               │
│  ChatService: оркестрация lifecycle запроса,         │
│  без HTTP-концепций, без знания о форматах           │
├─────────────────────────────────────────────────────┤
│  domain/                   Domain                    │
│  ChatMessage, ChatRequest, ChatResponse, AuditRecord │
│  BaseSanitizer, SanitizerChain, BaseAuditBackend     │
│  доменные исключения                                 │
├─────────────────────────────────────────────────────┤
│  infrastructure/           Infrastructure            │
│  OpenAICompatibleAdapter, FileAuditBackend,          │
│  StdoutAuditBackend, PiiRegexSanitizer,              │
│  StaticKeyAuthMiddleware, AdapterRegistry            │
└─────────────────────────────────────────────────────┘
```

**Правило зависимостей:** каждый слой знает только о слоях ниже себя. `api` зависит от `application`, `application` зависит от `domain`, `infrastructure` зависит от `domain`. `domain` ни от чего не зависит.

`AuthMiddleware` остаётся ASGI middleware — это настоящий cross-cutting concern: должен срабатывать до любого кода, не участвует в теле запроса, должен покрывать все маршруты автоматически. Результат (`AuthContext`) передаётся в `ChatService` явным параметром.

### Структура директорий

```
gateway/
├── app.py                          # FastAPI setup, middleware registration
├── config.py                       # YAML + env loading, Pydantic models
│
├── api/
│   └── openai/
│       ├── __init__.py
│       ├── routes.py               # POST /v1/chat/completions (тонкий)
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
│   │   └── base.py                 # BaseAuthProvider (контракт), AuthContext (value object)
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
    │   ├── base.py                 # BaseLLMAdapter (контракт для плагинов)
    │   ├── registry.py             # AdapterRegistry
    │   └── openai_compatible.py
    ├── audit/
    │   ├── __init__.py
    │   ├── record.py               # → перенесено в domain/models.py
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

Центральный элемент v3.0. Владеет полным lifecycle запроса, не знает про HTTP.

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

### API Layer (тонкий route)

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
    # ... все существующие поля без изменений ...
    error: str | None = None

    # body logging — None когда disabled
    messages: list[dict] | None = None    # санитайзированные входящие сообщения
    completion: str | None = None         # ответ LLM (content string)
```

### Что попадает в лог

| Ситуация | `messages` | `completion` |
|----------|-----------|--------------|
| `status: success`, `log_body: true` | полный санитайзированный массив | полный текст ответа |
| `status: blocked`, `log_body: true` | сообщения до момента блокировки | `null` |
| `status: error`, `log_body: true` | санитайзированные сообщения (если дошли) | `null` |
| `log_body: false` (default) | `null` | `null` |

**Privacy guarantee сохраняется:** в лог всегда попадают только санитайзированные данные — `ChatService` запускает `SanitizerChain` до записи в аудит.

### Пример JSON line при `log_body: true`

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

## Доменные исключения

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

## Обработка ошибок

| Исключение | HTTP | AuditRecord.status |
|-----------|------|-------------------|
| `SanitizerBlockedError` | 400 | `blocked` |
| `AdapterNotFoundError` | 400 | `error` |
| `UpstreamTimeoutError` | 504 | `error` |
| `UpstreamError` | 502 | `error` |
| `BaseAuditBackend.write()` throws | 500 | — (не может логировать себя) |

Последний случай: `ChatService` не перехватывает исключения из `audit.write()`. Они поднимаются в route handler и превращаются в 500. Compliance-гарантия сохраняется: если аудит упал — клиент не получает ответ.

---

## Расширяемость: будущий Anthropic API

`ChatService.complete()` принимает `raw_messages: list[dict]` — протокол-агностичный формат. Добавление `POST /v1/messages`:

1. Создать `api/anthropic/routes.py` — парсит Anthropic-формат (system как отдельное поле, content blocks) в `list[dict]`
2. Вызвать тот же `chat_service.complete()`
3. Форматировать `ChatResponse` в Anthropic-формат ответа

`ChatService`, `domain/`, `infrastructure/` — не меняются.

---

## Тестирование

### Unit — `tests/unit/test_chat_service.py`

| Тест | Что проверяет |
|------|---------------|
| `test_success_no_body_logging` | success flow, `messages=None`, `completion=None` в записи |
| `test_success_with_body_logging` | `log_body=True` → messages и completion в записи |
| `test_blocked_writes_audit` | sanitizer blocked → аудит записан, `SanitizerBlockedError` поднят |
| `test_blocked_body_logging_partial` | blocked на третьем сообщении → messages содержат только первые два |
| `test_adapter_not_found` | KeyError из registry → `AdapterNotFoundError`, аудит записан |
| `test_upstream_timeout` | `httpx.TimeoutException` → `UpstreamTimeoutError`, аудит записан |
| `test_upstream_error` | generic Exception → `UpstreamError`, аудит записан |
| `test_audit_failure_propagates` | `audit.write()` throws → исключение не перехватывается |

### Unit — `tests/unit/test_openai_routes.py`

| Тест | Что проверяет |
|------|---------------|
| `test_success_response_format` | правильный OpenAI-формат ответа |
| `test_blocked_returns_400` | `SanitizerBlockedError` → 400 с правильным кодом ошибки |
| `test_adapter_not_found_returns_400` | `AdapterNotFoundError` → 400 |
| `test_timeout_returns_504` | `UpstreamTimeoutError` → 504 |
| `test_upstream_error_returns_502` | `UpstreamError` → 502 |

### Integration — `tests/integration/test_full_stack.py`

Полный стек через FastAPI `TestClient` с mock LLM adapter.

| Тест | Что проверяет |
|------|---------------|
| `test_success_audit_written` | аудит-запись появилась, `status=success` |
| `test_body_logging_enabled` | `log_body=True` → messages и completion в записи |
| `test_body_logging_disabled` | `log_body=False` → messages=None, completion=None |
| `test_blocked_audit_written` | blocked запрос → аудит-запись `status=blocked` |
| `test_no_auth_returns_401` | отсутствие ключа → 401, аудит НЕ пишется |

---

## Миграция из v2.2

### Удалить

| Файл | Причина |
|------|---------|
| `gateway/middleware/sanitize.py` | логика переезжает в `ChatService` |
| `gateway/middleware/audit.py` | логика переезжает в `ChatService` |

### Переместить

| Откуда | Куда |
|--------|------|
| `gateway/middleware/auth.py` → `AuthMiddleware` + `StaticKeyAuthProvider` | `gateway/infrastructure/auth/static_key.py` |
| `gateway/middleware/auth.py` → `BaseAuthProvider` + `AuthContext` | `gateway/domain/auth/base.py` |
| `gateway/audit/record.py` | `gateway/domain/models.py` (объединить с ChatRequest/ChatResponse) |
| `gateway/audit/base.py` | `gateway/domain/audit/base.py` |
| `gateway/audit/stdout_backend.py` | `gateway/infrastructure/audit/stdout_backend.py` |
| `gateway/audit/file_backend.py` | `gateway/infrastructure/audit/file_backend.py` |
| `gateway/adapters/base.py` | `gateway/infrastructure/adapters/base.py` (BaseLLMAdapter — контракт для плагинов) |
| `gateway/adapters/registry.py` | `gateway/infrastructure/adapters/registry.py` |
| `gateway/adapters/openai_compatible.py` | `gateway/infrastructure/adapters/openai_compatible.py` |
| `gateway/sanitizers/base.py` | `gateway/domain/sanitizers/base.py` |
| `gateway/sanitizers/passthrough.py` | `gateway/infrastructure/sanitizers/passthrough.py` |
| `gateway/sanitizers/pii_regex.py` | `gateway/infrastructure/sanitizers/pii_regex.py` |
| `gateway/routes.py` | `gateway/api/openai/routes.py` (упрощается) |

### Изменить

| Файл | Что меняется |
|------|-------------|
| `gateway/config.py` | добавить `BodyLoggingConfig` в каждый `AuditConfig`-вариант |
| `gateway/app.py` | убрать `SanitizeMiddleware` и `AuditLogMiddleware`, создать `ChatService`, передать в router |
| `gateway/domain/models.py` | добавить `messages` и `completion` в `AuditRecord` |
| `gateway.yaml.example` | добавить `body_logging` пример |

### Breaking change для plugin-авторов

Базовые классы (`BaseLLMAdapter`, `BaseSanitizer`, `BaseAuditBackend`) меняют import path:

```python
# было
from gateway.adapters.base import BaseLLMAdapter
from gateway.sanitizers.base import BaseSanitizer
from gateway.audit.base import BaseAuditBackend

# стало
from gateway.infrastructure.adapters.base import BaseLLMAdapter
from gateway.domain.sanitizers.base import BaseSanitizer
from gateway.domain.audit.base import BaseAuditBackend
```

Описывается в DEVLOG как breaking change v3.0.

---

## Файлы

| Действие | Файл |
|----------|------|
| Создать | `gateway/api/openai/__init__.py` |
| Создать | `gateway/api/openai/routes.py` |
| Создать | `gateway/api/openai/schemas.py` |
| Создать | `gateway/application/__init__.py` |
| Создать | `gateway/application/chat_service.py` |
| Создать | `gateway/domain/__init__.py` |
| Создать | `gateway/domain/models.py` |
| Создать | `gateway/domain/exceptions.py` |
| Создать | `gateway/domain/auth/__init__.py` |
| Создать | `gateway/domain/auth/base.py` |
| Создать | `gateway/domain/sanitizers/__init__.py` |
| Создать | `gateway/domain/sanitizers/base.py` |
| Создать | `gateway/domain/audit/__init__.py` |
| Создать | `gateway/domain/audit/base.py` |
| Создать | `gateway/infrastructure/__init__.py` |
| Создать | `gateway/infrastructure/adapters/__init__.py` |
| Создать | `gateway/infrastructure/adapters/base.py` |
| Создать | `gateway/infrastructure/adapters/registry.py` |
| Создать | `gateway/infrastructure/adapters/openai_compatible.py` |
| Создать | `gateway/infrastructure/audit/__init__.py` |
| Создать | `gateway/infrastructure/audit/stdout_backend.py` |
| Создать | `gateway/infrastructure/audit/file_backend.py` |
| Создать | `gateway/infrastructure/auth/__init__.py` |
| Создать | `gateway/infrastructure/auth/static_key.py` |
| Создать | `gateway/infrastructure/sanitizers/__init__.py` |
| Создать | `gateway/infrastructure/sanitizers/passthrough.py` |
| Создать | `gateway/infrastructure/sanitizers/pii_regex.py` |
| Изменить | `gateway/app.py` |
| Изменить | `gateway/config.py` |
| Удалить | `gateway/middleware/sanitize.py` |
| Удалить | `gateway/middleware/audit.py` |
| Удалить | `gateway/middleware/__init__.py` (если пуст после удаления) |
| Удалить | `gateway/audit/` (перенесено) |
| Удалить | `gateway/adapters/` (перенесено) |
| Удалить | `gateway/sanitizers/` (перенесено) |
| Удалить | `gateway/routes.py` (перенесено) |
| Создать | `tests/unit/test_chat_service.py` |
| Создать | `tests/unit/test_openai_routes.py` |
| Изменить | `tests/unit/test_audit.py` → обновить imports |
| Изменить | `tests/unit/test_config.py` → добавить тесты `BodyLoggingConfig` |
| Изменить | `tests/integration/test_middleware_stack.py` → обновить под новую структуру |
| Изменить | `gateway.yaml.example` |
| Изменить | `docs/2026-06-05-llm-gateway-design.md` |
| Изменить | `docs/DEVLOG.md` |
