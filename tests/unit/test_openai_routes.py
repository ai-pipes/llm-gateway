import json as _json
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


def _streaming_service(chunks=None, error=None):
    """Service mock whose complete_stream() yields given chunks or raises error."""
    s = AsyncMock()
    s.complete = AsyncMock(return_value=ChatResponse(
        content="hello", model="gpt-mock",
        usage={"prompt_tokens": 5, "completion_tokens": 3},
    ))

    async def _complete_stream(**kwargs):
        if error is not None:
            raise error
        for chunk in (chunks or ["Hello", " World"]):
            yield chunk

    s.complete_stream = _complete_stream
    return s


def _collect_sse(response) -> list[dict]:
    """Parse SSE response body, return list of data payloads (excluding [DONE])."""
    results = []
    for line in response.text.splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            results.append(_json.loads(line[6:]))
    return results


def _collect_all_sse(response) -> list[dict]:
    """Parse all SSE data lines including error events (excluding [DONE])."""
    results = []
    for line in response.text.splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            results.append(_json.loads(line[6:]))
    return results


def test_stream_returns_200_with_sse_content_type():
    client = TestClient(_app(_streaming_service(chunks=["Hello", " World"])))
    resp = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": "gpt-mock",
        "stream": True,
    })
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]


def test_stream_yields_content_chunks_as_sse():
    client = TestClient(_app(_streaming_service(chunks=["Hello", " World"])))
    resp = client.post("/v1/chat/completions", json={
        "messages": [], "model": "x", "stream": True,
    })
    events = _collect_sse(resp)
    contents = [e["choices"][0]["delta"].get("content", "") for e in events
                if e.get("choices") and e["choices"][0].get("delta", {}).get("content")]
    assert contents == ["Hello", " World"]


def test_stream_final_chunk_has_finish_reason_stop():
    client = TestClient(_app(_streaming_service(chunks=["Hi"])))
    resp = client.post("/v1/chat/completions", json={
        "messages": [], "model": "x", "stream": True,
    })
    events = _collect_sse(resp)
    finish_reasons = [e["choices"][0].get("finish_reason") for e in events if e.get("choices")]
    assert "stop" in finish_reasons


def test_stream_ends_with_done_sentinel():
    client = TestClient(_app(_streaming_service(chunks=["Hi"])))
    resp = client.post("/v1/chat/completions", json={
        "messages": [], "model": "x", "stream": True,
    })
    assert "data: [DONE]" in resp.text


def test_stream_blocked_sends_error_sse_event():
    client = TestClient(_app(_streaming_service(error=SanitizerBlockedError("PII"))))
    resp = client.post("/v1/chat/completions", json={"messages": [], "model": "x", "stream": True})
    assert resp.status_code == 200
    events = _collect_all_sse(resp)
    error_events = [e for e in events if "error" in e]
    assert len(error_events) == 1
    assert error_events[0]["error"]["code"] == "sanitizer_blocked"
    assert "data: [DONE]" in resp.text  # stream always ends with [DONE]


def test_stream_adapter_not_found_sends_error_sse_event():
    client = TestClient(_app(_streaming_service(error=AdapterNotFoundError("bad"))))
    resp = client.post("/v1/chat/completions", json={"messages": [], "model": "x", "stream": True})
    assert resp.status_code == 200
    events = _collect_all_sse(resp)
    error_events = [e for e in events if "error" in e]
    assert len(error_events) == 1
    assert error_events[0]["error"]["code"] == "adapter_not_found"
    assert "data: [DONE]" in resp.text  # stream always ends with [DONE]


def test_stream_upstream_timeout_sends_error_sse_event():
    client = TestClient(_app(_streaming_service(error=UpstreamTimeoutError())))
    resp = client.post("/v1/chat/completions", json={"messages": [], "model": "x", "stream": True})
    assert resp.status_code == 200
    events = _collect_all_sse(resp)
    error_events = [e for e in events if "error" in e]
    assert len(error_events) == 1
    assert error_events[0]["error"]["code"] == "upstream_timeout"
    assert "data: [DONE]" in resp.text  # stream always ends with [DONE]


def test_stream_upstream_error_sends_error_sse_event():
    client = TestClient(_app(_streaming_service(error=UpstreamError("fail"))))
    resp = client.post("/v1/chat/completions", json={"messages": [], "model": "x", "stream": True})
    assert resp.status_code == 200
    events = _collect_all_sse(resp)
    error_events = [e for e in events if "error" in e]
    assert len(error_events) == 1
    assert error_events[0]["error"]["code"] == "upstream_error"
    assert "data: [DONE]" in resp.text  # stream always ends with [DONE]


def test_stream_always_ends_with_done_sentinel_on_error():
    client = TestClient(_app(_streaming_service(error=UpstreamTimeoutError())))
    resp = client.post("/v1/chat/completions", json={"messages": [], "model": "x", "stream": True})
    assert "data: [DONE]" in resp.text


def test_non_streaming_route_unaffected_by_streaming_changes():
    client = TestClient(_app(_streaming_service()))
    resp = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "model": "gpt-mock",
    })
    assert resp.status_code == 200
    assert resp.json()["object"] == "chat.completion"
