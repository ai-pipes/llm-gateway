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

```
HTTP Request
    │
    ▼
AuthMiddleware          → 401 if auth fails (no audit written)
    │
    ▼
AuditLogMiddleware      → wraps the rest; writes audit on every response
    │
    ▼
SanitizeMiddleware      → 400 if input blocked (audit written with status=blocked)
    │
    ▼
Route Handler           → calls LLM adapter
    │
    ▼
HTTP Response
```

**Compliance guarantee:** If audit write fails, client receives 500 — the LLM response is NOT returned. No response without a record.

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

(This section already has `default: true` ✓)

### Custom Auth Provider

```python
from gateway.middleware.auth import BaseAuthProvider, AuthContext

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
from gateway.sanitizers.base import BaseSanitizer, SanitizeResult

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

> **Note (v1):** The sanitizers config section is not yet wired in v1. Input/output chains are always empty in the current release. This will be implemented in v2.

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

## Roadmap

| Version | Feature |
|---------|---------|
| v2 | PII regex sanitizer (email, phone, card) |
| v2 | PostgreSQL + file audit backends |
| v2 | Prometheus metrics endpoint |
| v3 | Anthropic Messages API (`/v1/messages`) for Claude Code proxy |
| v3 | Rate limiting per key / team |
| v3 | Full request/response logging (opt-in) |

## License

MIT
