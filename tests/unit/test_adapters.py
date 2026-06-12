import pytest
import json as _json
import httpx
from unittest.mock import AsyncMock, MagicMock, patch
from gateway.domain.models import ChatMessage, ChatRequest, ChatResponse
from gateway.domain.adapters.base import BaseLLMAdapter
from gateway.infrastructure.adapters.registry import AdapterRegistry
from gateway.infrastructure.adapters.openai_compatible import OpenAICompatibleAdapter


def test_chat_message_has_role_and_content():
    msg = ChatMessage(role="user", content="hello")
    assert msg.role == "user"
    assert msg.content == "hello"


def test_chat_request_defaults():
    req = ChatRequest(
        model="gpt-4o",
        messages=[ChatMessage(role="user", content="hi")],
    )
    assert req.temperature == 0.7
    assert req.stream is False


def test_chat_response_holds_usage():
    resp = ChatResponse(
        content="Hello!",
        model="gpt-4o",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )
    assert resp.usage["prompt_tokens"] == 10


def test_base_adapter_is_abstract():
    with pytest.raises(TypeError):
        BaseLLMAdapter()


async def test_base_adapter_stream_chat_default_impl():
    class ConcreteAdapter(BaseLLMAdapter):
        name = "test"
        async def chat(self, request):
            return ChatResponse(content="hi", model="test", usage={})

    adapter = ConcreteAdapter()
    chunks = [chunk async for chunk in adapter.stream_chat(
        ChatRequest(model="test", messages=[])
    )]
    assert chunks == ["hi"]


class StubAdapter(BaseLLMAdapter):
    def __init__(self, name: str):
        self.name = name

    async def chat(self, request: ChatRequest) -> ChatResponse:
        return ChatResponse(content="stub", model=self.name, usage={})


def test_registry_get_by_name():
    registry = AdapterRegistry()
    adapter = StubAdapter("openai")
    registry.register(adapter)
    assert registry.get("openai") is adapter


def test_registry_get_default():
    registry = AdapterRegistry()
    adapter = StubAdapter("openai")
    registry.register(adapter, default=True)
    assert registry.get() is adapter


def test_registry_get_unknown_raises():
    registry = AdapterRegistry()
    with pytest.raises(KeyError, match="unknown"):
        registry.get("unknown")


def test_registry_get_no_default_raises():
    registry = AdapterRegistry()
    registry.register(StubAdapter("openai"))
    with pytest.raises(KeyError):
        registry.get()


@pytest.fixture
def adapter():
    return OpenAICompatibleAdapter(
        name="test-openai",
        base_url="https://api.example.com/v1",
        api_key="sk-test",
    )


async def test_openai_adapter_sends_correct_request(adapter):
    fake_response_data = {
        "choices": [{"message": {"role": "assistant", "content": "Hello!"}}],
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    fake_request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    mock_response = httpx.Response(200, json=fake_response_data, request=fake_request)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        request = ChatRequest(
            model="gpt-4o",
            messages=[ChatMessage(role="user", content="Hi")],
        )
        response = await adapter.chat(request)

    assert response.content == "Hello!"
    assert response.model == "gpt-4o"
    assert response.usage["prompt_tokens"] == 10

    call_kwargs = mock_post.call_args
    assert call_kwargs.args[0] == "https://api.example.com/v1/chat/completions"
    assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer sk-test"
    body = call_kwargs.kwargs["json"]
    assert body["model"] == "gpt-4o"
    assert body["messages"] == [{"role": "user", "content": "Hi"}]
    assert "temperature" in body


async def test_openai_adapter_raises_on_http_error(adapter):
    fake_request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    mock_response = httpx.Response(429, json={"error": "rate limited"}, request=fake_request)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.chat(ChatRequest(model="gpt-4o", messages=[]))


async def test_openai_adapter_raises_on_timeout(adapter):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               side_effect=httpx.TimeoutException("timed out")):
        with pytest.raises(httpx.TimeoutException):
            await adapter.chat(ChatRequest(model="gpt-4o", messages=[]))


def _make_stream_mock(lines):
    """Returns mock_cls for patching httpx.AsyncClient, with stream() returning SSE lines."""
    async def _aiter_lines():
        for line in lines:
            yield line

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.aiter_lines = _aiter_lines
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    return MagicMock(return_value=mock_client)


async def test_stream_chat_yields_content_chunks(adapter):
    lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"content": "Hello"}, "index": 0}]}),
        'data: ' + _json.dumps({"choices": [{"delta": {"content": " World"}, "index": 0}]}),
        'data: [DONE]',
    ]
    mock_cls = _make_stream_mock(lines)
    with patch("gateway.infrastructure.adapters.openai_compatible.httpx.AsyncClient", mock_cls):
        request = ChatRequest(model="gpt-4o", messages=[ChatMessage(role="user", content="hi")])
        chunks = [c async for c in adapter.stream_chat(request)]
    assert chunks == ["Hello", " World"]


async def test_stream_chat_populates_usage_out(adapter):
    lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"content": "Hi"}, "index": 0}]}),
        'data: ' + _json.dumps({
            "choices": [{"delta": {}, "index": 0}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }),
        'data: [DONE]',
    ]
    mock_cls = _make_stream_mock(lines)
    usage_out = {}
    with patch("gateway.infrastructure.adapters.openai_compatible.httpx.AsyncClient", mock_cls):
        request = ChatRequest(model="gpt-4o", messages=[])
        _ = [c async for c in adapter.stream_chat(request, usage_out=usage_out)]
    assert usage_out["prompt_tokens"] == 5
    assert usage_out["completion_tokens"] == 2


async def test_stream_chat_skips_empty_delta(adapter):
    lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"role": "assistant"}, "index": 0}]}),
        'data: ' + _json.dumps({"choices": [{"delta": {"content": "Hi"}, "index": 0}]}),
        'data: ' + _json.dumps({"choices": [{"delta": {}, "index": 0}]}),
        'data: [DONE]',
    ]
    mock_cls = _make_stream_mock(lines)
    with patch("gateway.infrastructure.adapters.openai_compatible.httpx.AsyncClient", mock_cls):
        chunks = [c async for c in adapter.stream_chat(
            ChatRequest(model="gpt-4o", messages=[]), usage_out=None
        )]
    assert chunks == ["Hi"]


async def test_stream_chat_raises_on_http_error(adapter):
    fake_request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    fake_resp = httpx.Response(429, request=fake_request)
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("429", request=fake_request, response=fake_resp)
    )
    mock_resp.aiter_lines = MagicMock()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_cls = MagicMock(return_value=mock_client)
    with patch("gateway.infrastructure.adapters.openai_compatible.httpx.AsyncClient", mock_cls):
        with pytest.raises(httpx.HTTPStatusError):
            _ = [c async for c in adapter.stream_chat(ChatRequest(model="x", messages=[]))]


def test_build_payload_includes_tools(adapter):
    tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
    req = ChatRequest(model="gpt-4o", messages=[], tools=tools)
    payload = adapter._build_payload(req)
    assert payload["tools"] == tools


def test_build_payload_no_tools_key_when_none(adapter):
    req = ChatRequest(model="gpt-4o", messages=[])
    payload = adapter._build_payload(req)
    assert "tools" not in payload


def test_build_payload_serialises_tool_calls_in_message(adapter):
    tool_calls = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    msg = ChatMessage(role="assistant", tool_calls=tool_calls)
    req = ChatRequest(model="gpt-4o", messages=[msg])
    payload = adapter._build_payload(req)
    assert payload["messages"][0]["tool_calls"] == tool_calls
    assert payload["messages"][0]["content"] is None


def test_build_payload_serialises_tool_call_id_in_message(adapter):
    msg = ChatMessage(role="tool", content="result", tool_call_id="c1")
    req = ChatRequest(model="gpt-4o", messages=[msg])
    payload = adapter._build_payload(req)
    assert payload["messages"][0]["tool_call_id"] == "c1"
    assert payload["messages"][0]["content"] == "result"


async def test_openai_adapter_chat_parses_tool_calls(adapter):
    tool_calls = [{"id": "c1", "type": "function", "function": {"name": "search", "arguments": '{"q":"test"}'}}]
    fake_response_data = {
        "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": tool_calls}}],
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    fake_request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    mock_response = httpx.Response(200, json=fake_response_data, request=fake_request)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
        request = ChatRequest(model="gpt-4o", messages=[ChatMessage(role="user", content="search for test")])
        response = await adapter.chat(request)

    assert response.content is None
    assert response.tool_calls == tool_calls


async def test_stream_chat_yields_tool_call_delta(adapter):
    tool_call_delta = [{"index": 0, "id": "c1", "type": "function", "function": {"name": "search", "arguments": ""}}]
    lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"tool_calls": tool_call_delta}, "index": 0}]}),
        'data: ' + _json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"q":"x"}'}}]}, "index": 0}]}),
        'data: [DONE]',
    ]
    mock_cls = _make_stream_mock(lines)
    with patch("gateway.infrastructure.adapters.openai_compatible.httpx.AsyncClient", mock_cls):
        request = ChatRequest(model="gpt-4o", messages=[], tools=[{"type": "function", "function": {"name": "search"}}])
        chunks = [c async for c in adapter.stream_chat(request)]
    assert len(chunks) == 2
    assert all(isinstance(c, dict) for c in chunks)
    assert chunks[0]["tool_calls"] == tool_call_delta


async def test_stream_chat_mixes_text_and_tool_call_deltas(adapter):
    tool_call_delta = [{"index": 0, "id": "c1", "function": {"name": "search", "arguments": ""}}]
    lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"content": "Hello"}, "index": 0}]}),
        'data: ' + _json.dumps({"choices": [{"delta": {"tool_calls": tool_call_delta}, "index": 0}]}),
        'data: [DONE]',
    ]
    mock_cls = _make_stream_mock(lines)
    with patch("gateway.infrastructure.adapters.openai_compatible.httpx.AsyncClient", mock_cls):
        chunks = [c async for c in adapter.stream_chat(ChatRequest(model="gpt-4o", messages=[]))]
    assert chunks[0] == "Hello"
    assert isinstance(chunks[1], dict)
    assert "tool_calls" in chunks[1]


async def test_base_adapter_stream_chat_yields_tool_calls_from_response():
    tool_calls = [{"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]

    class ConcreteAdapter(BaseLLMAdapter):
        name = "test"
        async def chat(self, request):
            return ChatResponse(model="test", usage={}, tool_calls=tool_calls)

    adapter = ConcreteAdapter()
    chunks = [c async for c in adapter.stream_chat(ChatRequest(model="test", messages=[]))]
    assert len(chunks) == 1
    assert isinstance(chunks[0], dict)
    assert chunks[0]["tool_calls"] == tool_calls


async def test_stream_chat_usage_after_done_sentinel(adapter):
    # Some providers (OpenAI) send the usage chunk AFTER [DONE]
    lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"content": "Hi"}, "index": 0}]}),
        'data: [DONE]',
        'data: ' + _json.dumps({
            "choices": [],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
        }),
    ]
    mock_cls = _make_stream_mock(lines)
    usage_out = {}
    with patch("gateway.infrastructure.adapters.openai_compatible.httpx.AsyncClient", mock_cls):
        chunks = [c async for c in adapter.stream_chat(
            ChatRequest(model="gpt-4o", messages=[]), usage_out=usage_out
        )]
    assert chunks == ["Hi"]
    assert usage_out["prompt_tokens"] == 8
    assert usage_out["completion_tokens"] == 3
