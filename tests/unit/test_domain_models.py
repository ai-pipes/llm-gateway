from datetime import datetime, timezone
from gateway.domain.models import (
    AuthContext, ChatMessage, ChatRequest, ChatResponse, AuditRecord
)


def test_audit_record_body_fields_default_to_none():
    record = AuditRecord(
        request_id="r1",
        timestamp=datetime.now(timezone.utc),
        api_key_id="k1",
        user_id=None,
        team_id=None,
        adapter="openai",
        model="gpt-4",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=100,
        input_actions=[],
        output_actions=[],
        status="success",
    )
    assert record.messages is None
    assert record.completion is None
    assert record.error is None


def test_audit_record_accepts_body_fields():
    msgs = [{"role": "user", "content": "hello"}]
    record = AuditRecord(
        request_id="r2",
        timestamp=datetime.now(timezone.utc),
        api_key_id="k1",
        user_id="u1",
        team_id="t1",
        adapter="openai",
        model="gpt-4",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=100,
        input_actions=[],
        output_actions=[],
        status="success",
        messages=msgs,
        completion="hi there",
    )
    assert record.messages == msgs
    assert record.completion == "hi there"


def test_chat_message_tool_fields_default_to_none():
    msg = ChatMessage(role="user", content="hi")
    assert msg.tool_calls is None
    assert msg.tool_call_id is None


def test_chat_message_accepts_tool_calls():
    tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]
    msg = ChatMessage(role="assistant", tool_calls=tool_calls)
    assert msg.content is None
    assert msg.tool_calls == tool_calls


def test_chat_message_accepts_tool_call_id():
    msg = ChatMessage(role="tool", content="result", tool_call_id="call_1")
    assert msg.tool_call_id == "call_1"


def test_chat_request_tools_defaults_to_none():
    req = ChatRequest(model="gpt-4o", messages=[])
    assert req.tools is None


def test_chat_request_accepts_tools():
    tools = [{"type": "function", "function": {"name": "search", "description": "search", "parameters": {}}}]
    req = ChatRequest(model="gpt-4o", messages=[], tools=tools)
    assert req.tools == tools


def test_chat_response_tool_calls_defaults_to_none():
    resp = ChatResponse(content="hi", model="gpt-4o", usage={})
    assert resp.tool_calls is None


def test_chat_response_accepts_tool_calls():
    tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]
    resp = ChatResponse(content=None, model="gpt-4o", usage={}, tool_calls=tool_calls)
    assert resp.tool_calls == tool_calls
    assert resp.content is None
