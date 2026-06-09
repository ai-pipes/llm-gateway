import pytest
from pathlib import Path
from gateway.domain.sanitizers.base import BaseSanitizer, SanitizerChain, SanitizeResult
from gateway.infrastructure.adapters.registry import AdapterRegistry
from gateway.app import create_app_from_components
from gateway.infrastructure.auth.static_key import StaticKeyAuthProvider
from gateway.domain.models import AuditRecord
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
    from gateway.infrastructure.sanitizers.pii_regex import PiiRegexSanitizer
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
            "module": "gateway.infrastructure.auth.static_key.StaticKeyAuthProvider",
            "config": {"keys": {"sk-test": {"user_id": "u", "team_id": "t"}}},
        },
        "adapters": [],
        "sanitizers": {
            "input": [
                {"module": "gateway.sanitizers.nonexistent.FakeSanitizer", "config": {}}
            ],
            "output": [],
        },
        "audit": {"type": "stdout"},
    }
    config_file = tmp_path / "gateway.yaml"
    config_file.write_text(yaml.dump(config))

    with pytest.raises(ValueError, match="Cannot load sanitizer"):
        create_app(str(config_file))


def test_audit_writes_to_file(tmp_path):
    """Full stack request with FileAuditBackend — JSON line appears in the file."""
    import json
    from gateway.infrastructure.audit.file_backend import FileAuditBackend

    audit_path = str(tmp_path / "audit.jsonl")
    backend = FileAuditBackend(path=audit_path)

    from tests.conftest import MockLLMAdapter
    registry = AdapterRegistry()
    registry.register(MockLLMAdapter(), default=True)

    app = create_app_from_components(
        auth_provider=StaticKeyAuthProvider({"test-key": {"user_id": "u", "team_id": "t"}}),
        input_chain=SanitizerChain([]),
        output_chain=SanitizerChain([]),
        audit_backend=backend,
        registry=registry,
    )
    c = TestClient(app, raise_server_exceptions=False)
    response = c.post(
        "/v1/chat/completions",
        json=VALID_REQUEST,
        headers={"x-api-key": "test-key"},
    )
    assert response.status_code == 200

    lines = Path(audit_path).read_text().strip().split("\n")
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["status"] == "success"
    assert record["user_id"] == "u"


def test_create_app_wires_file_audit_backend(tmp_path, monkeypatch):
    """create_app() correctly wires FileAuditBackend when config has type: file."""
    import json
    import yaml
    from gateway.app import create_app

    audit_path = str(tmp_path / "audit.jsonl")
    config = {
        "gateway": {"host": "0.0.0.0", "port": 8080},
        "auth": {
            "module": "gateway.infrastructure.auth.static_key.StaticKeyAuthProvider",
            "config": {"keys": {"sk-test": {"user_id": "u", "team_id": "t"}}},
        },
        "adapters": [],
        "sanitizers": {"input": [], "output": []},
        "audit": {"type": "file", "path": audit_path},
    }
    config_file = tmp_path / "gateway.yaml"
    config_file.write_text(yaml.dump(config))

    app = create_app(str(config_file))
    from fastapi.testclient import TestClient
    c = TestClient(app, raise_server_exceptions=False)
    response = c.post(
        "/v1/chat/completions",
        json=VALID_REQUEST,
        headers={"x-api-key": "sk-test"},
    )
    assert response.status_code == 400  # no adapter registered → 400

    lines = Path(audit_path).read_text().strip().split("\n")
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["user_id"] == "u"


def test_body_logging_writes_messages_and_completion(tmp_path):
    """With log_body=True, AuditRecord contains sanitized messages and completion text."""
    from tests.conftest import MockLLMAdapter, CapturingAuditBackend

    audit = CapturingAuditBackend()
    registry = AdapterRegistry()
    registry.register(MockLLMAdapter(), default=True)

    app = create_app_from_components(
        auth_provider=StaticKeyAuthProvider({"test-key": {"user_id": "u", "team_id": "t"}}),
        input_chain=SanitizerChain([]),
        output_chain=SanitizerChain([]),
        audit_backend=audit,
        registry=registry,
        log_body=True,
    )
    c = TestClient(app, raise_server_exceptions=False)
    c.post(
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]},
        headers={"x-api-key": "test-key"},
    )

    record = audit.last()
    assert record.status == "success"
    assert record.messages == [{"role": "user", "content": "Hello"}]
    assert record.completion == "Hello from mock!"


def test_body_logging_disabled_by_default(client, audit_capture):
    """Default: messages and completion are None in audit record."""
    client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]},
        headers={"x-api-key": "test-key"},
    )
    record = audit_capture.last()
    assert record.messages is None
    assert record.completion is None
