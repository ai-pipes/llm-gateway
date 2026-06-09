import pytest
from unittest.mock import AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gateway.api.openai.routes import create_router
from gateway.domain.models import ChatResponse, AuthContext
from gateway.domain.exceptions import (
    SanitizerBlockedError, AdapterNotFoundError, UpstreamTimeoutError, UpstreamError,
)


def _app(service):
    app = FastAPI()

    @app.middleware("http")
    async def inject_auth(request, call_next):
        request.state.auth = AuthContext(key_id="k1", user_id="u1", team_id="t1")
        return await call_next(request)

    app.include_router(create_router(service))
    return app


def _ok_service():
    s = AsyncMock()
    s.complete = AsyncMock(return_value=ChatResponse(
        content="hello", model="gpt-mock",
        usage={"prompt_tokens": 5, "completion_tokens": 3},
    ))
    return s


def _fail_service(exc):
    s = AsyncMock()
    s.complete = AsyncMock(side_effect=exc)
    return s


def test_success_returns_200_openai_format():
    client = TestClient(_app(_ok_service()))
    resp = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "model": "gpt-mock"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["message"]["content"] == "hello"
    assert "id" in data
    assert "usage" in data


def test_blocked_returns_400_with_code():
    client = TestClient(_app(_fail_service(SanitizerBlockedError("PII"))))
    resp = client.post("/v1/chat/completions", json={"messages": [], "model": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "sanitizer_blocked"


def test_adapter_not_found_returns_400():
    client = TestClient(_app(_fail_service(AdapterNotFoundError("bad"))))
    resp = client.post("/v1/chat/completions", json={"messages": [], "model": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "adapter_not_found"


def test_timeout_returns_504():
    client = TestClient(_app(_fail_service(UpstreamTimeoutError())))
    resp = client.post("/v1/chat/completions", json={"messages": [], "model": "x"})
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "upstream_timeout"


def test_upstream_error_returns_502():
    client = TestClient(_app(_fail_service(UpstreamError("fail"))))
    resp = client.post("/v1/chat/completions", json={"messages": [], "model": "x"})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_error"
