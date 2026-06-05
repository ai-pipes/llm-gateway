import pytest
from fastapi.testclient import TestClient
from gateway.adapters.base import BaseLLMAdapter, ChatRequest, ChatResponse
from gateway.adapters.registry import AdapterRegistry
from gateway.audit.base import BaseAuditBackend
from gateway.audit.record import AuditRecord
from gateway.app import create_app_from_components
from gateway.middleware.auth import StaticKeyAuthProvider
from gateway.sanitizers.base import SanitizerChain


class MockLLMAdapter(BaseLLMAdapter):
    name = "mock"

    async def chat(self, request: ChatRequest) -> ChatResponse:
        return ChatResponse(
            content="Hello from mock!",
            model=request.model or "mock-model",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )


class CapturingAuditBackend(BaseAuditBackend):
    def __init__(self):
        self.records: list[AuditRecord] = []

    async def write(self, record: AuditRecord) -> None:
        self.records.append(record)

    def last(self) -> AuditRecord:
        assert self.records, "No audit records captured"
        return self.records[-1]


@pytest.fixture
def audit_capture():
    return CapturingAuditBackend()


@pytest.fixture
def client(audit_capture):
    registry = AdapterRegistry()
    registry.register(MockLLMAdapter(), default=True)

    app = create_app_from_components(
        auth_provider=StaticKeyAuthProvider(
            {"test-key": {"user_id": "test-user", "team_id": "test-team"}}
        ),
        input_chain=SanitizerChain([]),
        output_chain=SanitizerChain([]),
        audit_backend=audit_capture,
        registry=registry,
    )
    return TestClient(app, raise_server_exceptions=False)
