# LLM Gateway — Design Document

**Date:** 2026-06-05  
**Status:** Approved  
**Author:** Alexander Melnik

---

## Контекст и цель

LLM Gateway — первый "кирпичик" в серии образовательных проектов по enterprise AI engineering. Цель: дать корпорациям готовый каркас, который можно скачать, подключить к своей LLM и развернуть во внутреннем контуре. Сам по себе gateway не содержит никакой бизнес-логики — только контракты (абстрактные классы) и reference-реализации для быстрого старта.

Параллельная цель — обучающий проект с документированием всех архитектурных решений через ADR и DEVLOG.

---

## Скоуп v1

**В scope:**
- HTTP сервер с `POST /v1/chat/completions` (OpenAI-compatible)
- ASGI middleware stack: Auth → Input Sanitize → LLM → Output Sanitize → Audit
- Три абстрактных контракта: `BaseAuthMiddleware`, `BaseSanitizer`, `BaseLLMAdapter`
- Reference-реализации каждого контракта (для быстрого старта и как шаблон)
- Audit trail: синхронная запись, stdout backend (JSON lines)
- Конфиг: YAML + env vars
- Docker + docker-compose
- 4 ADR + DEVLOG

**Явно вне scope v1:**
- Anthropic Messages API (`/v1/messages`) — Claude Code proxy
- PII/NLP sanitization (только каркас)
- Rate limiting
- Observability / metrics endpoint
- PostgreSQL / file audit backends
- Full request/response logging
- Streaming (`stream: true`) — v1 принимает поле в запросе, но игнорирует и возвращает полный ответ

---

## Архитектура

### Стек

| Слой | Технология | Обоснование |
|---|---|---|
| HTTP сервер | FastAPI + Uvicorn | ASGI, async, production-ready, богатая экосистема |
| Middleware | Starlette BaseHTTPMiddleware | стандартный ASGI middleware contract |
| Конфиг | PyYAML + pydantic | валидация схемы + env var interpolation |
| Зависимости | pyproject.toml (PEP 517) | современный стандарт Python пакетов |

### Поток запроса

```
HTTP Request  POST /v1/chat/completions
    │
    ▼
AuthMiddleware          → 401 если authenticate() вернул None
    │                      аудит НЕ пишется (не аутентифицирован)
    ▼
SanitizeMiddleware      → если sanitizer заблокировал:
    │                      устанавливает request.state.audit_status = "blocked"
    │                      передаёт управление дальше (не прерывает цепочку)
    ▼
AuditLogMiddleware      → оборачивает handler + все последующие шаги
    │                      пишет запись ПОСЛЕ того как handler вернул ответ
    │                   → 500 если запись не удалась (ответ НЕ отдаётся)
    ▼
GatewayHandler          → 400 если sanitizer заблокировал (из request.state)
    │                   → 400 адаптер не найден
    │                   → 502 LLM вернул ошибку
    │                   → 504 LLM timeout
    ▼
HTTP Response
```

**Порядок регистрации middleware** в Starlette `add_middleware()` обратный порядку обработки: последний зарегистрированный = самый внешний. Чтобы получить цепочку Auth → Sanitize → Audit → Handler, регистрируем в обратном порядке: сначала Audit, потом Sanitize, потом Auth.

**Почему Audit стоит ПОСЛЕ Sanitize (но оборачивает Handler):**

Два конкурирующих требования:
1. **Privacy-by-design** — Audit никогда не должен видеть несанитайзированные данные. Если в v3 добавить full request logging, он должен логировать уже очищенный текст.
2. **Audit blocked-запросов** — blocked запросы (sanitizer заблокировал) тоже должны аудироваться со `status=blocked`.

Наивное решение — поставить Audit снаружи Sanitize — нарушает требование (1). Поставить Audit внутри Sanitize и делать ранний `return` из Sanitize — нарушает требование (2), потому что `return` без `call_next` обрывает всю внутреннюю цепочку и Audit не запускается.

**Решение:** SanitizeMiddleware при блокировке не делает ранний return. Вместо этого он сохраняет ошибку в `request.state.blocked_error` и всё равно вызывает `call_next`. GatewayHandler проверяет флаг первым делом и возвращает 400. Audit запускается как обёртка вокруг Handler и видит только санитайзированные данные.

**Ключевое решение по аудиту:** запись синхронная и блокирующая. Если аудит не записан — клиент получает 500 и ответ LLM не отдаётся. Это compliance-гарантия: факт передачи данных не существует без его записи. Трейдоф: +латентность на каждый запрос (см. ADR-004).

---

## Три контракта расширения

Вся extensibility gateway строится вокруг трёх абстрактных классов. Gateway реализует только reference-примеры. Корпоративный деплой подменяет их своими реализациями через `gateway.yaml`.

### BaseAuthMiddleware

```python
class BaseAuthMiddleware(ABC):
    @abstractmethod
    async def authenticate(self, request: Request) -> AuthContext | None:
        """Вернуть AuthContext — пустить. None — 401."""
        ...

@dataclass
class AuthContext:
    key_id: str        # хэш ключа — идёт в аудит
    user_id: str | None
    team_id: str | None
```

Reference-реализация: `StaticKeyAuthMiddleware` — читает ключи из `gateway.yaml`. Только для быстрого старта, не для production.

Примеры корпоративных реализаций (не в репозитории): JWT validation, LDAP lookup, Okta/Entra SSO, API keys из БД.

### BaseSanitizer + SanitizerChain

```python
class BaseSanitizer(ABC):
    @abstractmethod
    async def sanitize(self, text: str) -> SanitizeResult:
        ...

@dataclass
class SanitizeResult:
    text: str
    actions: list[str]   # ["replaced:EMAIL"] — в аудит
    blocked: bool = False
    block_reason: str = ""
```

`SanitizerChain` прогоняет текст через цепочку санитайзеров последовательно. При первом `blocked=True` — цепочка прерывается.

В v1 цепочка пустая. Middleware и chain существуют, но ничего не делают. Добавление PII regex в v2 не требует изменения middleware.

Reference-реализация: `PassthroughSanitizer` — no-op, для тестов и как шаблон.

### BaseLLMAdapter

```python
class BaseLLMAdapter(ABC):
    name: str

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResponse:
        ...
```

Два уровня extensibility:
1. **Config-only** (`type: openai_compatible`): для любого OpenAI-compatible endpoint — нет кода, только YAML.
2. **Plugin** (`type: plugin`, `module: ...`): Python-класс, реализующий `BaseLLMAdapter` — для нестандартных API.

Reference-реализация: `OpenAICompatibleAdapter`.

---

## Схема данных

### ChatRequest / ChatResponse

```python
@dataclass
class ChatMessage:
    role: str        # "system" | "user" | "assistant"
    content: str

@dataclass
class ChatRequest:
    model: str
    messages: list[ChatMessage]
    temperature: float = 0.7
    stream: bool = False

@dataclass
class ChatResponse:
    content: str
    model: str
    usage: dict   # {"prompt_tokens": N, "completion_tokens": N}
```

### AuditRecord

```python
@dataclass
class AuditRecord:
    request_id: str        # uuid4
    timestamp: datetime
    api_key_id: str        # хэш — не сам ключ
    user_id: str | None
    team_id: str | None
    adapter: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    input_actions: list[str]
    output_actions: list[str]
    status: str            # "success" | "error" | "blocked"
    error: str | None      # при status=error: сообщение исключения или HTTP статус upstream
```

**Важно:** содержимое prompt и response в `AuditRecord` отсутствует. Full request/response logging — отдельная фича v3 с явным opt-in в конфиге.

---

## Конфигурация

```yaml
gateway:
  host: "0.0.0.0"
  port: 8080

auth:
  module: "gateway.middleware.auth.StaticKeyAuthMiddleware"
  config:
    keys:
      "sk-dev-key":
        user_id: "dev"
        team_id: "engineering"

adapters:
  - name: openai
    type: openai_compatible
    base_url: "https://api.openai.com/v1"
    auth:
      token_env: OPENAI_API_KEY
    default: true    # используется если клиент не указал адаптер явно

  - name: corp-llm
    type: plugin
    module: "my_adapters.corp_llm.CorpLLMAdapter"

sanitizers:
  input: []
  output: []

audit:
  type: stdout   # stdout | file | plugin
```

---

## Обработка ошибок

Единый формат ошибок — OpenAI-совместимый:

```json
{
  "error": {
    "type": "invalid_request_error",
    "message": "...",
    "code": "sanitizer_blocked"
  }
}
```

| Слой | Ситуация | HTTP | Аудит |
|---|---|---|---|
| AuthMiddleware | неверный/отсутствующий ключ | 401 | нет |
| InputSanitize | sanitizer заблокировал | 400 | да, status=blocked |
| InputSanitize | sanitizer упал | 500 | да, status=error |
| GatewayHandler | адаптер не найден | 400 | да, status=error |
| GatewayHandler | LLM вернул ошибку | 502 | да, status=error |
| GatewayHandler | LLM timeout | 504 | да, status=error |
| AuditMiddleware | запись не удалась | 500 | — |

Правило: всё что прошло Auth → попадает в аудит. Исключение — сам AuditMiddleware (не может логировать собственный сбой через себя).

---

## Структура проекта

```
llm-gateway/
├── gateway/
│   ├── app.py                      # FastAPI app + middleware registration
│   ├── config.py                   # YAML + env loading, pydantic models
│   ├── middleware/
│   │   ├── auth.py                 # BaseAuthMiddleware + StaticKeyAuthMiddleware
│   │   ├── sanitize.py             # InputSanitizeMiddleware + OutputSanitizeMiddleware
│   │   └── audit.py                # AuditLogMiddleware
│   ├── adapters/
│   │   ├── base.py                 # BaseLLMAdapter, ChatRequest, ChatResponse
│   │   ├── registry.py             # загрузка адаптеров из конфига
│   │   └── openai_compatible.py    # reference implementation
│   ├── sanitizers/
│   │   ├── base.py                 # BaseSanitizer, SanitizerChain, SanitizeResult
│   │   └── passthrough.py          # reference implementation
│   └── audit/
│       ├── record.py               # AuditRecord dataclass
│       ├── base.py                 # BaseAuditBackend
│       ├── stdout_backend.py       # reference implementation
│       └── file_backend.py         # file backend (v2.2)
├── examples/
│   └── custom_adapter.py           # шаблон для пользователя
├── docs/
│   ├── adr/
│   │   ├── 001-python-stack.md
│   │   ├── 002-asgi-middleware.md
│   │   ├── 003-openai-contract-first.md
│   │   └── 004-sync-audit.md
│   └── DEVLOG.md
├── tests/
│   ├── unit/
│   │   ├── test_sanitizers.py
│   │   ├── test_adapters.py
│   │   └── test_config.py
│   └── integration/
│       ├── test_middleware_stack.py
│       └── test_audit_backend.py
├── gateway.yaml.example
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

---

## Тестирование

**Принцип:** реальный LLM не вызывается в тестах. Адаптер mock возвращает фиксированный ответ.

- **Unit тесты:** чистые функции без I/O — `BaseSanitizer` контракт, `SanitizerChain` порядок, config parsing.
- **Integration тесты:** FastAPI `TestClient` — полный поток через middleware stack с mock adapter. Проверяем: 401 без ключа, 400 при blocked sanitizer, audit record записан при успехе и при ошибке.

---

## Бэклог (вне v1)

| Версия | Фича |
|---|---|
| v2 | `PiiRegexSanitizer` (email, phone, card) |
| v2 | `FileBackend` + `PostgresBackend` для аудита |
| v2 | `/metrics` endpoint (Prometheus) |
| v3 | Full request/response logging (opt-in) |
| v3 | Rate limiting per key / per team |
| v3 | Anthropic Messages API (`/v1/messages`) — Claude Code proxy |
| v3 | NLP sanitization (spaCy / Microsoft Presidio) |
