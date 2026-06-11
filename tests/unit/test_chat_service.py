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
import asyncio


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


# ---------------------------------------------------------------------------
# complete_stream() tests
# ---------------------------------------------------------------------------

async def _collect_stream(gen):
    """Drain an async generator, return list of yielded strings."""
    chunks = []
    async for chunk in gen:
        chunks.append(chunk)
    return chunks


def _streaming_adapter(chunks=None, usage=None, error=None):
    """Returns adapter mock whose stream_chat() is an async generator yielding given chunks."""
    import httpx as _httpx

    a = MagicMock()
    a.name = "mock"
    a.chat = AsyncMock(return_value=ChatResponse(content="", model="mock-model", usage={}))

    async def _stream_chat(request, usage_out=None):
        if error == "timeout":
            raise _httpx.TimeoutException("timeout")
        if error == "runtime":
            raise RuntimeError("LLM down")
        for chunk in (chunks or ["hello", " world"]):
            yield chunk
        if usage_out is not None and usage:
            usage_out.update(usage)

    a.stream_chat = _stream_chat
    return a


@pytest.mark.asyncio
async def test_complete_stream_yields_chunks():
    adapter = _streaming_adapter(chunks=["Hello", " World"])
    svc = _service(registry=_registry(adapter=adapter))
    chunks = await _collect_stream(svc.complete_stream(
        raw_messages=[{"role": "user", "content": "hi"}],
        model="gpt-mock", auth=_auth(), request_id="s-1",
    ))
    assert chunks == ["Hello", " World"]


@pytest.mark.asyncio
async def test_complete_stream_writes_audit_after_stream():
    audit = _audit()
    adapter = _streaming_adapter(chunks=["Hello"])
    svc = _service(registry=_registry(adapter=adapter), audit=audit)
    await _collect_stream(svc.complete_stream(
        raw_messages=[{"role": "user", "content": "hi"}],
        model="gpt-mock", auth=_auth(), request_id="s-2",
    ))
    audit.write.assert_called_once()
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.status == "success"
    assert record.request_id == "s-2"


@pytest.mark.asyncio
async def test_complete_stream_no_body_logging_by_default():
    audit = _audit()
    adapter = _streaming_adapter(chunks=["Hi"])
    svc = _service(registry=_registry(adapter=adapter), audit=audit, log_body=False)
    await _collect_stream(svc.complete_stream(
        raw_messages=[{"role": "user", "content": "hi"}],
        model="gpt-mock", auth=_auth(), request_id="s-3",
    ))
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.completion is None
    assert record.messages is None


@pytest.mark.asyncio
async def test_complete_stream_body_logging_sanitizes_output():
    audit = _audit()
    output_chain = _chain(actions=["replaced:EMAIL"])
    adapter = _streaming_adapter(chunks=["call me ", "john@example.com"])
    svc = ChatService(
        input_chain=_chain(),
        output_chain=output_chain,
        registry=_registry(adapter=adapter),
        audit=audit,
        log_body=True,
    )
    await _collect_stream(svc.complete_stream(
        raw_messages=[{"role": "user", "content": "hi"}],
        model="gpt-mock", auth=_auth(), request_id="s-4",
    ))
    record: AuditRecord = audit.write.call_args[0][0]
    # output_chain.run returns "sanitized" (from _chain() mock)
    assert record.completion == "sanitized"
    assert "replaced:EMAIL" in record.output_actions


@pytest.mark.asyncio
async def test_complete_stream_blocked_input_raises_before_yielding():
    audit = _audit()
    svc = _service(input_chain=_chain(blocked=True, block_reason="PII"), audit=audit)
    with pytest.raises(SanitizerBlockedError) as exc_info:
        await _collect_stream(svc.complete_stream(
            raw_messages=[{"role": "user", "content": "my ssn"}],
            model="gpt-mock", auth=_auth(), request_id="s-5",
        ))
    assert exc_info.value.reason == "PII"
    audit.write.assert_called_once()
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.status == "blocked"


@pytest.mark.asyncio
async def test_complete_stream_adapter_not_found_raises_before_yielding():
    audit = _audit()
    svc = _service(registry=_registry(missing=True), audit=audit)
    with pytest.raises(AdapterNotFoundError):
        await _collect_stream(svc.complete_stream(
            raw_messages=[{"role": "user", "content": "hi"}],
            model="gpt-mock", auth=_auth(), request_id="s-6",
            adapter_name="nonexistent",
        ))
    audit.write.assert_called_once()
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.status == "error"


@pytest.mark.asyncio
async def test_complete_stream_upstream_timeout_writes_audit():
    audit = _audit()
    adapter = _streaming_adapter(error="timeout")
    svc = _service(registry=_registry(adapter=adapter), audit=audit)
    with pytest.raises(UpstreamTimeoutError):
        await _collect_stream(svc.complete_stream(
            raw_messages=[{"role": "user", "content": "hi"}],
            model="gpt-mock", auth=_auth(), request_id="s-7",
        ))
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.status == "error"
    assert record.error == "upstream_timeout"


@pytest.mark.asyncio
async def test_complete_stream_upstream_error_writes_audit():
    audit = _audit()
    adapter = _streaming_adapter(error="runtime")
    svc = _service(registry=_registry(adapter=adapter), audit=audit)
    with pytest.raises(UpstreamError):
        await _collect_stream(svc.complete_stream(
            raw_messages=[{"role": "user", "content": "hi"}],
            model="gpt-mock", auth=_auth(), request_id="s-8",
        ))
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.status == "error"
    assert record.error == "LLM down"


@pytest.mark.asyncio
async def test_complete_stream_usage_from_adapter():
    audit = _audit()
    adapter = _streaming_adapter(
        chunks=["hi"],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    )
    svc = _service(registry=_registry(adapter=adapter), audit=audit)
    await _collect_stream(svc.complete_stream(
        raw_messages=[{"role": "user", "content": "hi"}],
        model="gpt-mock", auth=_auth(), request_id="s-9",
    ))
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.prompt_tokens == 7
    assert record.completion_tokens == 3


@pytest.mark.asyncio
async def test_complete_stream_cancelled_on_disconnect():
    """GeneratorExit (client disconnect) must write status='cancelled', not 'success'."""
    audit = _audit()
    adapter = _streaming_adapter(chunks=["a", "b", "c", "d", "e"])
    svc = _service(registry=_registry(adapter=adapter), audit=audit)

    gen = svc.complete_stream(
        raw_messages=[{"role": "user", "content": "hi"}],
        model="gpt-mock", auth=_auth(), request_id="s-cancel",
    )
    # Consume only the first chunk, then close the generator
    await gen.__anext__()
    await gen.aclose()

    audit.write.assert_called_once()
    record: AuditRecord = audit.write.call_args[0][0]
    assert record.status == "cancelled"


# ---------------------------------------------------------------------------
# Restoration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_restores_pii_in_response():
    """LLM receives sanitized input; client gets response with original PII restored."""
    from gateway.infrastructure.sanitizers.pii_regex import PiiRegexSanitizer
    from gateway.domain.sanitizers.base import SanitizerChain

    received_content: list[str] = []

    async def _chat(req):
        user_msg = next(m.content for m in req.messages if m.role == "user")
        received_content.append(user_msg)
        return ChatResponse(content=user_msg, model="mock", usage={})

    a = MagicMock()
    a.name = "mock"
    a.chat = _chat

    svc = ChatService(
        input_chain=SanitizerChain([PiiRegexSanitizer(mode="replace")]),
        output_chain=SanitizerChain([]),
        registry=_registry(adapter=a),
        audit=_audit(),
        log_body=False,
    )

    response = await svc.complete(
        raw_messages=[{"role": "user", "content": "My email is john@example.com"}],
        model="gpt-mock", auth=_auth(), request_id="req-restore",
    )

    assert "john@example.com" not in received_content[0]
    assert "john@example.com" in response.content
    assert "[EMAIL_" not in response.content


@pytest.mark.asyncio
async def test_complete_injects_system_instruction_when_pii_present():
    """A system message with placeholder instructions is prepended when PII is sanitized."""
    from gateway.infrastructure.sanitizers.pii_regex import PiiRegexSanitizer
    from gateway.domain.sanitizers.base import SanitizerChain

    received_messages: list = []

    async def _chat(req):
        received_messages.extend(req.messages)
        return ChatResponse(content="ok", model="mock", usage={})

    a = MagicMock()
    a.name = "mock"
    a.chat = _chat

    svc = ChatService(
        input_chain=SanitizerChain([PiiRegexSanitizer(mode="replace")]),
        output_chain=SanitizerChain([]),
        registry=_registry(adapter=a),
        audit=_audit(),
        log_body=False,
    )

    await svc.complete(
        raw_messages=[{"role": "user", "content": "My email is john@example.com"}],
        model="gpt-mock", auth=_auth(), request_id="req-sys",
    )

    system_msgs = [m for m in received_messages if m.role == "system"]
    assert len(system_msgs) == 1
    assert "placeholder" in system_msgs[0].content.lower() or "TYPE_" in system_msgs[0].content


@pytest.mark.asyncio
async def test_complete_no_system_injection_when_no_pii():
    """No system message is added if no PII was detected."""
    from gateway.infrastructure.sanitizers.pii_regex import PiiRegexSanitizer
    from gateway.domain.sanitizers.base import SanitizerChain

    received_messages: list = []

    async def _chat(req):
        received_messages.extend(req.messages)
        return ChatResponse(content="ok", model="mock", usage={})

    a = MagicMock()
    a.name = "mock"
    a.chat = _chat

    svc = ChatService(
        input_chain=SanitizerChain([PiiRegexSanitizer(mode="replace")]),
        output_chain=SanitizerChain([]),
        registry=_registry(adapter=a),
        audit=_audit(),
        log_body=False,
    )

    await svc.complete(
        raw_messages=[{"role": "user", "content": "Hello, how are you?"}],
        model="gpt-mock", auth=_auth(), request_id="req-no-pii",
    )

    system_msgs = [m for m in received_messages if m.role == "system"]
    assert len(system_msgs) == 0


@pytest.mark.asyncio
async def test_complete_appends_to_existing_system_message():
    """System instruction is appended to an existing system message, not creating a new one."""
    from gateway.infrastructure.sanitizers.pii_regex import PiiRegexSanitizer
    from gateway.domain.sanitizers.base import SanitizerChain

    received_messages: list = []

    async def _chat(req):
        received_messages.extend(req.messages)
        return ChatResponse(content="ok", model="mock", usage={})

    a = MagicMock()
    a.name = "mock"
    a.chat = _chat

    svc = ChatService(
        input_chain=SanitizerChain([PiiRegexSanitizer(mode="replace")]),
        output_chain=SanitizerChain([]),
        registry=_registry(adapter=a),
        audit=_audit(),
        log_body=False,
    )

    await svc.complete(
        raw_messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "My email is john@example.com"},
        ],
        model="gpt-mock", auth=_auth(), request_id="req-sys-append",
    )

    system_msgs = [m for m in received_messages if m.role == "system"]
    assert len(system_msgs) == 1
    assert "You are a helpful assistant." in system_msgs[0].content
    assert "TYPE_" in system_msgs[0].content or "placeholder" in system_msgs[0].content.lower()


@pytest.mark.asyncio
async def test_complete_audit_logs_sanitized_content_not_restored():
    """Audit log must store sanitized (placeholder) content, not original PII."""
    from gateway.infrastructure.sanitizers.pii_regex import PiiRegexSanitizer
    from gateway.domain.sanitizers.base import SanitizerChain

    async def _chat(req):
        user_msg = next(m.content for m in req.messages if m.role == "user")
        return ChatResponse(content=user_msg, model="mock", usage={})

    a = MagicMock()
    a.name = "mock"
    a.chat = _chat
    audit = _audit()

    svc = ChatService(
        input_chain=SanitizerChain([PiiRegexSanitizer(mode="replace")]),
        output_chain=SanitizerChain([]),
        registry=_registry(adapter=a),
        audit=audit,
        log_body=True,
    )

    await svc.complete(
        raw_messages=[{"role": "user", "content": "My email is john@example.com"}],
        model="gpt-mock", auth=_auth(), request_id="req-audit",
    )

    record: AuditRecord = audit.write.call_args[0][0]
    assert "john@example.com" not in (record.completion or "")
    assert "[EMAIL_" in (record.completion or "")


# ---------------------------------------------------------------------------
# complete_stream — PII restoration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_stream_restores_pii_in_output():
    """Placeholders in streamed chunks are restored before yielding to caller."""
    from unittest.mock import patch
    from gateway.domain.sanitizers.base import BaseSanitizer, SanitizerChain

    with patch("gateway.domain.sanitizers.restoration.secrets.token_hex", return_value="aabbccdd"):
        placeholder = "[EMAIL_ADDRESS_aabbccdd]"

        class RestoringSanitizer(BaseSanitizer):
            async def sanitize(self, text: str, context=None) -> SanitizeResult:
                if context is not None:
                    ph = context.register("john@example.com", "EMAIL_ADDRESS")
                    return SanitizeResult(text=ph, actions=["replaced:EMAIL_ADDRESS"])
                return SanitizeResult(text=text)

        # Adapter yields the known placeholder split across two chunks
        adapter = _streaming_adapter(chunks=[placeholder[:10], placeholder[10:] + " confirmed"])

        svc = ChatService(
            input_chain=SanitizerChain([RestoringSanitizer()]),
            output_chain=SanitizerChain([]),
            registry=_registry(adapter=adapter),
            audit=_audit(),
            log_body=False,
        )
        chunks = await _collect_stream(svc.complete_stream(
            raw_messages=[{"role": "user", "content": "my email john@example.com"}],
            model="gpt-mock", auth=_auth(), request_id="sr-1",
        ))
        result = "".join(chunks)
        assert "john@example.com" in result
        assert placeholder not in result


@pytest.mark.asyncio
async def test_complete_stream_audit_receives_placeholder_not_original():
    """Audit chunks contain placeholders; the restored email must not appear there."""
    from unittest.mock import patch
    from gateway.domain.sanitizers.base import BaseSanitizer, SanitizerChain

    with patch("gateway.domain.sanitizers.restoration.secrets.token_hex", return_value="aabbccdd"):
        placeholder = "[EMAIL_ADDRESS_aabbccdd]"

        class RestoringSanitizer(BaseSanitizer):
            async def sanitize(self, text: str, context=None) -> SanitizeResult:
                if context is not None:
                    ph = context.register("john@example.com", "EMAIL_ADDRESS")
                    return SanitizeResult(text=ph, actions=["replaced:EMAIL_ADDRESS"])
                return SanitizeResult(text=text)

        adapter = _streaming_adapter(chunks=[placeholder])
        audit = _audit()

        svc = ChatService(
            input_chain=SanitizerChain([RestoringSanitizer()]),
            output_chain=SanitizerChain([]),
            registry=_registry(adapter=adapter),
            audit=audit,
            log_body=True,
        )
        await _collect_stream(svc.complete_stream(
            raw_messages=[{"role": "user", "content": "my email john@example.com"}],
            model="gpt-mock", auth=_auth(), request_id="sr-2",
        ))
        record: AuditRecord = audit.write.call_args[0][0]
        # Audit body must not contain the restored original
        assert "john@example.com" not in (record.completion or "")


@pytest.mark.asyncio
async def test_complete_stream_injects_system_instruction_when_replacements():
    """System instruction is added to messages before stream when PII was replaced."""
    from unittest.mock import patch
    from gateway.domain.sanitizers.base import BaseSanitizer, SanitizerChain

    with patch("gateway.domain.sanitizers.restoration.secrets.token_hex", return_value="aabbccdd"):
        captured_request = []

        class RestoringSanitizer(BaseSanitizer):
            async def sanitize(self, text: str, context=None) -> SanitizeResult:
                if context is not None:
                    ph = context.register("john@example.com", "EMAIL_ADDRESS")
                    return SanitizeResult(text=ph, actions=["replaced:EMAIL_ADDRESS"])
                return SanitizeResult(text=text)

        a = MagicMock()
        a.name = "mock"

        async def _stream_chat(request, usage_out=None):
            captured_request.append(request)
            yield "ok"

        a.stream_chat = _stream_chat

        svc = ChatService(
            input_chain=SanitizerChain([RestoringSanitizer()]),
            output_chain=SanitizerChain([]),
            registry=_registry(adapter=a),
            audit=_audit(),
            log_body=False,
        )
        await _collect_stream(svc.complete_stream(
            raw_messages=[{"role": "user", "content": "email john@example.com"}],
            model="gpt-mock", auth=_auth(), request_id="sr-3",
        ))
        messages = captured_request[0].messages
        system_msgs = [m for m in messages if m.role == "system"]
        assert system_msgs, "system message should be injected"
        assert "placeholder" in system_msgs[0].content.lower()
