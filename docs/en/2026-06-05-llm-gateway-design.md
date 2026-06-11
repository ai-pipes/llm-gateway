# LLM Gateway — Design Document

**Date:** 2026-06-05  
**Status:** Approved  
**Author:** Alexander Melnik

---

## Context and Purpose

LLM Gateway is the first "building block" in a series of educational projects on enterprise AI engineering. The goal: give corporations a ready-made framework they can download, connect to their own LLM, and deploy within their internal infrastructure. The gateway itself contains no business logic — only contracts (abstract classes) and reference implementations for a quick start.

A parallel goal is an educational project with documentation of all architectural decisions through ADRs and a DEVLOG.

---

## Scope v1

**In scope:**
- HTTP server with `POST /v1/chat/completions` (OpenAI-compatible)
- ASGI middleware stack: Auth → Input Sanitize → LLM → Output Sanitize → Audit
- Three abstract contracts: `BaseAuthMiddleware`, `BaseSanitizer`, `BaseLLMAdapter`
- Reference implementations of each contract (for quick start and as a template)
- Audit trail: synchronous write, stdout backend (JSON lines)
- Config: YAML + env vars
- Docker + docker-compose
- 4 ADRs + DEVLOG

**Explicitly out of scope for v1:**
- Anthropic Messages API (`/v1/messages`) — Claude Code proxy
- PII/NLP sanitization (framework only)
- Rate limiting
- Observability / metrics endpoint
- PostgreSQL / file audit backends
- Full request/response logging
- Streaming (`stream: true`) — v1 accepts the field in the request but ignores it and returns a full response

---

## Architecture

### Stack

| Layer | Technology | Rationale |
|---|---|---|
| HTTP server | FastAPI + Uvicorn | ASGI, async, production-ready, rich ecosystem |
| Middleware | Starlette BaseHTTPMiddleware | standard ASGI middleware contract |
| Config | PyYAML + pydantic | schema validation + env var interpolation |
| Dependencies | pyproject.toml (PEP 517) | modern Python package standard |

### Request Flow

```
HTTP Request  POST /v1/chat/completions
    │
    ▼
AuthMiddleware          → 401 if authenticate() returned None
    │                      audit is NOT written (not authenticated)
    ▼
SanitizeMiddleware      → if sanitizer blocked:
    │                      sets request.state.audit_status = "blocked"
    │                      passes control forward (does not break the chain)
    ▼
AuditLogMiddleware      → wraps handler + all subsequent steps
    │                      writes a record AFTER the handler returns a response
    │                   → 500 if write failed (response is NOT returned)
    ▼
GatewayHandler          → 400 if sanitizer blocked (from request.state)
    │                   → 400 adapter not found
    │                   → 502 LLM returned an error
    │                   → 504 LLM timeout
    ▼
HTTP Response
```

**Middleware registration order** in Starlette `add_middleware()` is the reverse of processing order: the last registered = the outermost. To get the chain Auth → Sanitize → Audit → Handler, we register in reverse order: first Audit, then Sanitize, then Auth.

**Why Audit is placed AFTER Sanitize (but wraps the Handler):**

Two competing requirements:
1. **Privacy-by-design** — Audit must never see unsanitized data. If full request logging is added in v3, it must log already-cleaned text.
2. **Audit of blocked requests** — blocked requests (where the sanitizer blocked) must also be audited with `status=blocked`.

The naive solution — placing Audit outside Sanitize — violates requirement (1). Placing Audit inside Sanitize and doing an early `return` from Sanitize — violates requirement (2), because `return` without `call_next` cuts the entire inner chain and Audit does not run.

**Solution:** SanitizeMiddleware does not do an early return when blocking. Instead, it saves the error in `request.state.blocked_error` and still calls `call_next`. GatewayHandler checks the flag first and returns 400. Audit runs as a wrapper around Handler and only sees sanitized data.

**Key audit decision:** writes are synchronous and blocking. If the audit is not written — the client gets a 500 and the LLM response is not returned. This is a compliance guarantee: the fact of data transmission does not exist without its record. Trade-off: +latency on every request (see ADR-004).

---

## Three Extension Contracts

All gateway extensibility is built around three abstract classes. The gateway only implements reference examples. A corporate deployment replaces them with its own implementations via `gateway.yaml`.

### BaseAuthMiddleware

```python
class BaseAuthMiddleware(ABC):
    @abstractmethod
    async def authenticate(self, request: Request) -> AuthContext | None:
        """Return AuthContext — allow. None — 401."""
        ...

@dataclass
class AuthContext:
    key_id: str        # key hash — goes into audit
    user_id: str | None
    team_id: str | None
```

Reference implementation: `StaticKeyAuthMiddleware` — reads keys from `gateway.yaml`. For quick start only, not for production.

Examples of corporate implementations (not in the repository): JWT validation, LDAP lookup, Okta/Entra SSO, API keys from a database.

### BaseSanitizer + SanitizerChain

```python
class BaseSanitizer(ABC):
    @abstractmethod
    async def sanitize(self, text: str) -> SanitizeResult:
        ...

@dataclass
class SanitizeResult:
    text: str
    actions: list[str]   # ["replaced:EMAIL"] — goes into audit
    blocked: bool = False
    block_reason: str = ""
```

`SanitizerChain` passes text through a chain of sanitizers sequentially. On the first `blocked=True` — the chain is interrupted.

In v1 the chain is empty. The middleware and chain exist but do nothing. Adding PII regex in v2 does not require changes to the middleware.

Reference implementation: `PassthroughSanitizer` — no-op, for tests and as a template.

### BaseLLMAdapter

```python
class BaseLLMAdapter(ABC):
    name: str

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResponse:
        ...
```

Two levels of extensibility:
1. **Config-only** (`type: openai_compatible`): for any OpenAI-compatible endpoint — no code, only YAML.
2. **Plugin** (`type: plugin`, `module: ...`): a Python class implementing `BaseLLMAdapter` — for non-standard APIs.

Reference implementation: `OpenAICompatibleAdapter`.

---

## Data Schema

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
    api_key_id: str        # hash — not the key itself
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
    error: str | None      # on status=error: exception message or upstream HTTP status
```

**Important:** prompt and response content is absent from `AuditRecord`. Full request/response logging is a separate v3 feature with an explicit opt-in in the config.

---

## Configuration

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
    default: true    # used if the client did not specify an adapter explicitly

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

## Error Handling

Unified error format — OpenAI-compatible:

```json
{
  "error": {
    "type": "invalid_request_error",
    "message": "...",
    "code": "sanitizer_blocked"
  }
}
```

| Layer | Situation | HTTP | Audit |
|---|---|---|---|
| AuthMiddleware | invalid/missing key | 401 | no |
| InputSanitize | sanitizer blocked | 400 | yes, status=blocked |
| InputSanitize | sanitizer crashed | 500 | yes, status=error |
| GatewayHandler | adapter not found | 400 | yes, status=error |
| GatewayHandler | LLM returned an error | 502 | yes, status=error |
| GatewayHandler | LLM timeout | 504 | yes, status=error |
| AuditMiddleware | write failed | 500 | — |

Rule: everything that passes Auth → goes into audit. Exception — AuditMiddleware itself (cannot log its own failure through itself).

---

## Project Structure

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
│   │   ├── registry.py             # loading adapters from config
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
│   └── custom_adapter.py           # template for the user
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

## Testing

**Principle:** the real LLM is never called in tests. The mock adapter returns a fixed response.

- **Unit tests:** pure functions without I/O — `BaseSanitizer` contract, `SanitizerChain` ordering, config parsing.
- **Integration tests:** FastAPI `TestClient` — full flow through the middleware stack with a mock adapter. We verify: 401 without a key, 400 with a blocked sanitizer, audit record written on success and on error.

---

## Backlog (outside v1)

| Version | Feature |
|---|---|
| v2 | `PiiRegexSanitizer` (email, phone, card) |
| v2 | `FileBackend` + `PostgresBackend` for audit |
| v2 | `/metrics` endpoint (Prometheus) |
| v3 | Full request/response logging (opt-in) |
| v3 | Rate limiting per key / per team |
| v3 | Anthropic Messages API (`/v1/messages`) — Claude Code proxy |
| v3 | NLP sanitization (spaCy / Microsoft Presidio) |
