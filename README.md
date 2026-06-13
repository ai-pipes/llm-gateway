# LLM Gateway

[Русский](README.ru.md)

Corporate LLM Gateway — OpenAI-compatible proxy with audit trail, input sanitization, and pluggable adapters. Deploy in your corporate environment, connect any LLM.

## Quick Start

### Docker (recommended)

```bash
# 1. Copy config and env files
cp gateway.yaml.example gateway.yaml
cp .env.example .env

# 2. Set your LLM API key in .env
#    OPENAI_API_KEY=sk-...

# 3. Start
docker-compose up

# 4. Test
curl http://localhost:8080/v1/chat/completions \
  -H "x-api-key: sk-change-me" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Hello"}]}'
```

The default API key `sk-change-me` is defined in `gateway.yaml` under `auth.config.keys`.

### Local (without Docker)

```bash
# 1. Install (Python 3.11+)
pip install -e .

# 2. Copy config and set API key
cp gateway.yaml.example gateway.yaml
export OPENAI_API_KEY=sk-...

# 3. Start
uvicorn gateway.app:create_app --factory --port 8080
```

The default config uses `PresidioSanitizer` (NLP-based PII detection). Install its dependencies before first run:

```bash
pip install -e ".[presidio]"
python -m spacy download en_core_web_lg   # ~750 MB, one-time
```

To skip this and use regex-based detection instead, swap the sanitizer in `gateway.yaml` — see the commented option inside `gateway.yaml.example`.

## Architecture

[![Architecture diagram](docs/en/architecture-preview.png)](docs/en/architecture.html)

> Open [`docs/en/architecture.html`](docs/en/architecture.html) for the full interactive version with all request flows.

Four layers — dependencies flow downward only:

```
api/openai/        →  routes.py: StreamingResponse or JSONResponse
application/       →  ChatService: sanitize → adapter → audit (in finally)
domain/            →  BaseLLMAdapter, BaseSanitizer, BaseAuditBackend, models
infrastructure/    →  OpenAICompatibleAdapter, PresidioSanitizer, StdoutAuditBackend, …
```

`AuthMiddleware` is the only middleware — everything else lives in `ChatService`. Audit is written unconditionally via `finally`, covering success, error, and client disconnect (`status="cancelled"`).

## Extension Points

Three abstract contracts to implement. Working examples in `examples/`:

### Custom LLM Adapter — [`examples/custom_adapter.py`](examples/custom_adapter.py)

Option 1 — OpenAI-compatible endpoint (no code needed):
```yaml
adapters:
  - name: my-llm
    type: openai_compatible
    base_url: "https://your-llm.internal/v1"
    auth:
      token_env: MY_LLM_API_KEY
    default: true
```

Option 2 — custom adapter (Python):
```bash
cp examples/custom_adapter.py my_adapters/my_llm.py
# implement chat() method
```

```yaml
adapters:
  - name: my-llm
    type: plugin
    module: "my_adapters.my_llm.MyLLMAdapter"
    default: true
```

### Custom Auth Provider — [`examples/custom_auth_provider.py`](examples/custom_auth_provider.py)

```python
from gateway.infrastructure.auth.base import BaseAuthProvider
from gateway.domain.models import AuthContext

class JWTAuthProvider(BaseAuthProvider):
    async def authenticate(self, request) -> AuthContext | None:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        claims = verify_jwt(token)  # your JWT library
        if not claims:
            return None
        return AuthContext(
            key_id=hashlib.sha256(token.encode()).hexdigest()[:16],
            user_id=claims["sub"],
            team_id=claims.get("team"),
        )
```

```yaml
auth:
  module: "my_auth.jwt_provider.JWTAuthProvider"
  config:
    jwks_url: "https://your-idp.internal/.well-known/jwks.json"
```

### Custom Sanitizer

```python
from gateway.domain.sanitizers.base import BaseSanitizer, SanitizeResult

class PiiSanitizer(BaseSanitizer):
    async def sanitize(self, text: str) -> SanitizeResult:
        cleaned, found = redact_pii(text)  # your PII library
        return SanitizeResult(
            text=cleaned,
            actions=[f"redacted:{t}" for t in found],
        )
```

```yaml
sanitizers:
  input:
    - module: "my_sanitizers.pii.PiiSanitizer"
  output: []
```

### Custom Audit Backend — [`examples/custom_audit_backend.py`](examples/custom_audit_backend.py)

```yaml
audit:
  type: plugin
  module: "my_backends.http_audit.HttpAuditBackend"
  config:
    endpoint: "https://audit.corp.internal/v1/events"
    token_env: AUDIT_TOKEN
```

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest -v

# Run locally with auto-reload
uvicorn gateway.app:create_app --factory --port 8080 --reload
```

## Audit Trail

Every authenticated request generates an audit record (JSON to stdout → pipe to your log aggregator):

```json
{
  "request_id": "uuid",
  "timestamp": "2026-06-05T10:00:00+00:00",
  "api_key_id": "sha256-first-16-chars",
  "user_id": "alice",
  "team_id": "engineering",
  "adapter": "openai",
  "model": "gpt-4o",
  "prompt_tokens": 150,
  "completion_tokens": 80,
  "latency_ms": 1234,
  "input_actions": [],
  "output_actions": [],
  "status": "success",
  "error": null
}
```

Note: prompt and response content are **never** stored in the audit record unless `audit.body_logging.enabled: true`.

## Documentation

| Document | Description |
|---|---|
| [DEVLOG](docs/en/DEVLOG.md) | Development journal — decisions, bugs, insights per version |
| [Architecture](docs/en/architecture.html) | Interactive diagrams: layer map, request flows, SSE wire format |
| [ADR-001: Python stack](docs/en/adr/001-python-stack.md) | Why Python over Go/Rust |
| [ADR-002: ASGI middleware](docs/en/adr/002-asgi-middleware.md) | Why ASGI middleware over event bus for audit |
| [ADR-003: OpenAI-first contract](docs/en/adr/003-openai-contract-first.md) | Why OpenAI API as the primary interface |
| [ADR-004: Sync audit](docs/en/adr/004-sync-audit.md) | Why audit is synchronous and in-request |
| [ADR-005: Middleware order](docs/en/adr/005-middleware-order.md) | Auth → Sanitize → Audit and why order matters |

## License

MIT
