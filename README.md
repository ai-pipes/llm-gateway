# LLM Gateway

Corporate LLM Gateway — OpenAI-compatible proxy with audit trail, input/output sanitization, and pluggable adapters. Deploy in your corporate environment, connect any LLM.

## Quick Start

```bash
# 1. Copy and edit config
cp gateway.yaml.example gateway.yaml

# 2. Set your LLM API key
export OPENAI_API_KEY=sk-...

# 3. Run with Docker
docker-compose up

# 4. Test
curl http://localhost:8080/v1/chat/completions \
  -H "x-api-key: sk-change-me" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Architecture

[![Architecture diagram](docs/architecture-preview.png)](docs/architecture.html)

> Open `docs/architecture.html` for the full interactive version with all request flows.

Four layers — dependencies flow downward only:

```
api/openai/        →  routes.py: StreamingResponse or JSONResponse
application/       →  ChatService: sanitize → adapter → audit (in finally)
domain/            →  BaseLLMAdapter, BaseSanitizer, BaseAuditBackend, models
infrastructure/    →  OpenAICompatibleAdapter, PresidioSanitizer, StdoutAuditBackend, …
```

`AuthMiddleware` is the only middleware — everything else lives in `ChatService`. Audit is written unconditionally via `finally`, covering success, error, and client disconnect (`status="cancelled"`).

## Extension Points

Three abstract contracts to implement:

### Custom LLM Adapter (most common)

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

### Custom Auth Provider

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
  config: {}
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

## Development

```bash
# Install
pip install -e ".[dev]"

# Run tests
pytest -v

# Run locally (without Docker)
# Requires gateway.yaml (copy from example if not done already)
cp gateway.yaml.example gateway.yaml
OPENAI_API_KEY=sk-... uvicorn gateway.app:create_app --factory --port 8080
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

Note: prompt and response content are **never** stored in the audit record.

## License

MIT
