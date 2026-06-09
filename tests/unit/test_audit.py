import json
import pytest
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import patch
from gateway.domain.models import AuditRecord
from gateway.domain.audit.base import BaseAuditBackend
from gateway.infrastructure.audit.stdout_backend import StdoutAuditBackend


def make_record(**kwargs) -> AuditRecord:
    defaults = dict(
        request_id="req-123",
        timestamp=datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc),
        api_key_id="abc123",
        user_id="user-1",
        team_id="team-a",
        adapter="openai",
        model="gpt-4o",
        prompt_tokens=100,
        completion_tokens=50,
        latency_ms=234,
        input_actions=[],
        output_actions=[],
        status="success",
        error=None,
    )
    return AuditRecord(**{**defaults, **kwargs})


def test_audit_record_fields():
    record = make_record()
    assert record.request_id == "req-123"
    assert record.status == "success"
    assert record.error is None


def test_base_audit_backend_is_abstract():
    with pytest.raises(TypeError):
        BaseAuditBackend()


@pytest.mark.asyncio
async def test_stdout_backend_writes_json_line():
    backend = StdoutAuditBackend()
    record = make_record()

    with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
        await backend.write(record)
        output = mock_stdout.getvalue().strip()

    data = json.loads(output)
    assert data["request_id"] == "req-123"
    assert data["status"] == "success"
    assert data["timestamp"] == "2026-06-05T12:00:00+00:00"


@pytest.mark.asyncio
async def test_stdout_backend_serializes_error_field():
    backend = StdoutAuditBackend()
    record = make_record(status="error", error="upstream_timeout")

    with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
        await backend.write(record)
        data = json.loads(mock_stdout.getvalue().strip())

    assert data["error"] == "upstream_timeout"
