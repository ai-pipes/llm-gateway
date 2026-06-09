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
