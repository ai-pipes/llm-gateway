# tests/unit/test_chat_service.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from gateway.application.chat_service import ChatService
from gateway.domain.models import AuthContext, ChatResponse, AuditRecord
from gateway.domain.exceptions import (
    SanitizerBlockedError, AdapterNotFoundError, UpstreamTimeoutError, UpstreamError,
)
from gateway.domain.sanitizers.base import SanitizerChain, SanitizeResult
from gateway.domain.audit.base import BaseAuditBackend


def _auth():
    return AuthContext(key_id="k1", user_id="u1", team_id="t1")


def _chain(blocked=False, block_reason="", actions=None):
    chain = AsyncMock(spec=SanitizerChain)
    chain.run = AsyncMock(return_value=SanitizeResult(
        text="sanitized",
        actions=actions or [],
        blocked=blocked,
        block_reason=block_reason,
    ))
    return chain


def _adapter(content="mock response"):
    a = AsyncMock()
    a.name = "mock"
    a.chat = AsyncMock(return_value=ChatResponse(
        content=content,
        model="mock-model",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    ))
    return a


def _registry(adapter=None, missing=False):
    r = MagicMock()
    if missing:
        r.get = MagicMock(side_effect=KeyError("not found"))
    else:
        r.get = MagicMock(return_value=adapter or _adapter())
    return r


def _audit():
    b = AsyncMock(spec=BaseAuditBackend)
    b.write = AsyncMock()
    return b


def _service(input_chain=None, registry=None, audit=None, log_body=False):
    return ChatService(
        input_chain=input_chain or _chain(),
        output_chain=_chain(),
        registry=registry or _registry(),
        audit=audit or _audit(),
        log_body=log_body,
    )


@pytest.mark.asyncio
async def test_success_returns_response():
    response = await _service().complete(
        raw_messages=[{"role": "user", "content": "hello"}],
        model="gpt-mock",
        auth=_auth(),
        request_id="req-1",
    )
    assert response.content == "mock response"


@pytest.mark.asyncio
async def test_success_writes_audit_status_success():
    audit = _audit()
    await _service(audit=audit).complete(
        raw_messages=[{"role": "user", "content": "hello"}],
        model="gpt-mock",
        auth=_auth(),
        request_id="req-1",
    )
    audit.write.assert_called_once()
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.status == "success"
    assert record.request_id == "req-1"
    assert record.api_key_id == "k1"


@pytest.mark.asyncio
async def test_success_no_body_logging_when_disabled():
    audit = _audit()
    await _service(audit=audit, log_body=False).complete(
        raw_messages=[{"role": "user", "content": "hello"}],
        model="gpt-mock",
        auth=_auth(),
        request_id="req-1",
    )
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.messages is None
    assert record.completion is None


@pytest.mark.asyncio
async def test_success_body_logging_when_enabled():
    audit = _audit()
    await _service(audit=audit, log_body=True).complete(
        raw_messages=[{"role": "user", "content": "hello"}],
        model="gpt-mock",
        auth=_auth(),
        request_id="req-1",
    )
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.messages == [{"role": "user", "content": "sanitized"}]
    assert record.completion == "mock response"


@pytest.mark.asyncio
async def test_blocked_raises_sanitizer_blocked_error():
    with pytest.raises(SanitizerBlockedError) as exc_info:
        await _service(input_chain=_chain(blocked=True, block_reason="PII")).complete(
            raw_messages=[{"role": "user", "content": "my ssn is 123"}],
            model="gpt-mock",
            auth=_auth(),
            request_id="req-2",
        )
    assert exc_info.value.reason == "PII"


@pytest.mark.asyncio
async def test_blocked_writes_audit_with_status_blocked():
    audit = _audit()
    with pytest.raises(SanitizerBlockedError):
        await _service(input_chain=_chain(blocked=True, block_reason="PII"), audit=audit).complete(
            raw_messages=[{"role": "user", "content": "bad content"}],
            model="gpt-mock",
            auth=_auth(),
            request_id="req-2",
        )
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.status == "blocked"


@pytest.mark.asyncio
async def test_adapter_not_found_raises():
    with pytest.raises(AdapterNotFoundError) as exc_info:
        await _service(registry=_registry(missing=True)).complete(
            raw_messages=[{"role": "user", "content": "hello"}],
            model="gpt-mock",
            auth=_auth(),
            request_id="req-3",
            adapter_name="nonexistent",
        )
    assert exc_info.value.name == "nonexistent"


@pytest.mark.asyncio
async def test_adapter_not_found_writes_audit_status_error():
    audit = _audit()
    with pytest.raises(AdapterNotFoundError):
        await _service(registry=_registry(missing=True), audit=audit).complete(
            raw_messages=[{"role": "user", "content": "hello"}],
            model="gpt-mock",
            auth=_auth(),
            request_id="req-3",
        )
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.status == "error"
    assert "adapter_not_found" in record.error


@pytest.mark.asyncio
async def test_upstream_timeout_raises():
    import httpx
    a = AsyncMock()
    a.name = "mock"
    a.chat = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    with pytest.raises(UpstreamTimeoutError):
        await _service(registry=_registry(adapter=a)).complete(
            raw_messages=[{"role": "user", "content": "hello"}],
            model="gpt-mock",
            auth=_auth(),
            request_id="req-4",
        )


@pytest.mark.asyncio
async def test_upstream_timeout_writes_audit_status_error():
    import httpx
    a = AsyncMock()
    a.name = "mock"
    a.chat = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    audit = _audit()
    with pytest.raises(UpstreamTimeoutError):
        await _service(registry=_registry(adapter=a), audit=audit).complete(
            raw_messages=[{"role": "user", "content": "hello"}],
            model="gpt-mock",
            auth=_auth(),
            request_id="req-4",
        )
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.status == "error"
    assert record.error == "upstream_timeout"


@pytest.mark.asyncio
async def test_upstream_error_raises():
    a = AsyncMock()
    a.name = "mock"
    a.chat = AsyncMock(side_effect=RuntimeError("LLM down"))
    with pytest.raises(UpstreamError):
        await _service(registry=_registry(adapter=a)).complete(
            raw_messages=[{"role": "user", "content": "hello"}],
            model="gpt-mock",
            auth=_auth(),
            request_id="req-5",
        )


@pytest.mark.asyncio
async def test_audit_failure_propagates():
    audit = AsyncMock()
    audit.write = AsyncMock(side_effect=IOError("disk full"))
    with pytest.raises(IOError, match="disk full"):
        await _service(audit=audit).complete(
            raw_messages=[{"role": "user", "content": "hello"}],
            model="gpt-mock",
            auth=_auth(),
            request_id="req-6",
        )
