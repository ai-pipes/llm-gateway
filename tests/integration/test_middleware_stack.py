import pytest
from gateway.sanitizers.base import BaseSanitizer, SanitizerChain, SanitizeResult
from gateway.adapters.registry import AdapterRegistry
from gateway.app import create_app_from_components
from gateway.middleware.auth import StaticKeyAuthProvider
from gateway.audit.record import AuditRecord
from fastapi.testclient import TestClient


VALID_REQUEST = {
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello"}],
}


def test_missing_api_key_returns_401(client):
    response = client.post("/v1/chat/completions", json=VALID_REQUEST)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_invalid_api_key_returns_401(client):
    response = client.post(
        "/v1/chat/completions",
        json=VALID_REQUEST,
        headers={"x-api-key": "sk-wrong"},
    )
    assert response.status_code == 401


def test_valid_request_returns_200(client):
    response = client.post(
        "/v1/chat/completions",
        json=VALID_REQUEST,
        headers={"x-api-key": "test-key"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["choices"][0]["message"]["content"] == "Hello from mock!"
    assert data["object"] == "chat.completion"


def test_successful_request_writes_audit(client, audit_capture):
    client.post(
        "/v1/chat/completions",
        json=VALID_REQUEST,
        headers={"x-api-key": "test-key"},
    )
    record = audit_capture.last()
    assert record.status == "success"
    assert record.user_id == "test-user"
    assert record.team_id == "test-team"
    assert record.prompt_tokens == 10
    assert record.completion_tokens == 5
    assert record.latency_ms >= 0


def test_401_request_does_not_write_audit(client, audit_capture):
    client.post("/v1/chat/completions", json=VALID_REQUEST)
    assert len(audit_capture.records) == 0


def test_blocked_sanitizer_returns_400(audit_capture):
    class BlockEverythingSanitizer(BaseSanitizer):
        async def sanitize(self, text: str) -> SanitizeResult:
            return SanitizeResult(text=text, blocked=True, block_reason="test_block")

    from tests.conftest import MockLLMAdapter
    registry = AdapterRegistry()
    registry.register(MockLLMAdapter(), default=True)

    app = create_app_from_components(
        auth_provider=StaticKeyAuthProvider({"test-key": {"user_id": "u", "team_id": "t"}}),
        input_chain=SanitizerChain([BlockEverythingSanitizer()]),
        output_chain=SanitizerChain([]),
        audit_backend=audit_capture,
        registry=registry,
    )
    c = TestClient(app, raise_server_exceptions=False)
    response = c.post(
        "/v1/chat/completions",
        json=VALID_REQUEST,
        headers={"x-api-key": "test-key"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "sanitizer_blocked"


def test_blocked_request_writes_audit_with_status_blocked(audit_capture):
    class BlockEverythingSanitizer(BaseSanitizer):
        async def sanitize(self, text: str) -> SanitizeResult:
            return SanitizeResult(text=text, blocked=True, block_reason="pii_detected")

    from tests.conftest import MockLLMAdapter
    registry = AdapterRegistry()
    registry.register(MockLLMAdapter(), default=True)

    app = create_app_from_components(
        auth_provider=StaticKeyAuthProvider({"test-key": {"user_id": "u", "team_id": "t"}}),
        input_chain=SanitizerChain([BlockEverythingSanitizer()]),
        output_chain=SanitizerChain([]),
        audit_backend=audit_capture,
        registry=registry,
    )
    c = TestClient(app, raise_server_exceptions=False)
    response = c.post(
        "/v1/chat/completions",
        json=VALID_REQUEST,
        headers={"x-api-key": "test-key"},
    )
    assert response.status_code == 400
    record = audit_capture.last()
    assert record.status == "blocked"


def test_response_is_openai_compatible(client):
    response = client.post(
        "/v1/chat/completions",
        json=VALID_REQUEST,
        headers={"x-api-key": "test-key"},
    )
    data = response.json()
    assert "id" in data
    assert "choices" in data
    assert "usage" in data
    assert data["choices"][0]["message"]["role"] == "assistant"


def test_pii_replaced_in_request(audit_capture):
    """PII в теле запроса заменяется, ответ приходит успешно, аудит содержит replaced:EMAIL."""
    from gateway.sanitizers.pii_regex import PiiRegexSanitizer
    from tests.conftest import MockLLMAdapter

    registry = AdapterRegistry()
    registry.register(MockLLMAdapter(), default=True)

    app = create_app_from_components(
        auth_provider=StaticKeyAuthProvider({"test-key": {"user_id": "u", "team_id": "t"}}),
        input_chain=SanitizerChain([PiiRegexSanitizer(mode="replace")]),
        output_chain=SanitizerChain([]),
        audit_backend=audit_capture,
        registry=registry,
    )
    c = TestClient(app, raise_server_exceptions=False)
    response = c.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "My email is user@example.com, help me."}],
        },
        headers={"x-api-key": "test-key"},
    )

    assert response.status_code == 200
    record = audit_capture.last()
    assert record.status == "success"
    assert "replaced:EMAIL" in record.input_actions


def test_create_app_raises_on_bad_sanitizer_module(tmp_path):
    """create_app() raises ValueError with clear message if sanitizer module is invalid."""
    import yaml
    from gateway.app import create_app

    config = {
        "gateway": {"host": "0.0.0.0", "port": 8080},
        "auth": {
            "module": "gateway.middleware.auth.StaticKeyAuthProvider",
            "config": {"keys": {"sk-test": {"user_id": "u", "team_id": "t"}}},
        },
        "adapters": [],
        "sanitizers": {
            "input": [
                {"module": "gateway.sanitizers.nonexistent.FakeSanitizer", "config": {}}
            ],
            "output": [],
        },
        "audit": {"backend": "stdout"},
    }
    config_file = tmp_path / "gateway.yaml"
    config_file.write_text(yaml.dump(config))

    with pytest.raises(ValueError, match="Cannot load sanitizer"):
        create_app(str(config_file))
