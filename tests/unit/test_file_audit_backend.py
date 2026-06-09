import json
import pytest
from datetime import datetime, timezone
from pathlib import Path
from gateway.infrastructure.audit.file_backend import FileAuditBackend
from gateway.domain.models import AuditRecord


def _make_record(**kwargs) -> AuditRecord:
    defaults = dict(
        request_id="req-1",
        timestamp=datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc),
        api_key_id="hash-abc",
        user_id="alice",
        team_id="eng",
        adapter="mock",
        model="gpt-4",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=42,
        input_actions=[],
        output_actions=[],
        status="success",
    )
    defaults.update(kwargs)
    return AuditRecord(**defaults)


@pytest.mark.asyncio
async def test_write_creates_file(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    backend = FileAuditBackend(path=path)
    await backend.write(_make_record())
    assert Path(path).exists()


@pytest.mark.asyncio
async def test_write_appends_json_line(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    backend = FileAuditBackend(path=path)
    await backend.write(_make_record(request_id="req-1"))
    content = Path(path).read_text().strip()
    record = json.loads(content)
    assert record["request_id"] == "req-1"
    assert record["status"] == "success"
    assert record["timestamp"] == "2026-06-07T12:00:00+00:00"


@pytest.mark.asyncio
async def test_write_multiple_records(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    backend = FileAuditBackend(path=path)
    await backend.write(_make_record(request_id="req-1"))
    await backend.write(_make_record(request_id="req-2"))
    lines = Path(path).read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["request_id"] == "req-1"
    assert json.loads(lines[1])["request_id"] == "req-2"


@pytest.mark.asyncio
async def test_init_creates_parent_dirs(tmp_path):
    path = str(tmp_path / "nested" / "dirs" / "audit.jsonl")
    backend = FileAuditBackend(path=path)
    await backend.write(_make_record())
    assert Path(path).exists()


@pytest.mark.asyncio
async def test_write_serializes_none_fields(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    backend = FileAuditBackend(path=path)
    await backend.write(_make_record(user_id=None, team_id=None, error=None))
    record = json.loads(Path(path).read_text().strip())
    assert record["user_id"] is None
    assert record["team_id"] is None
    assert record["error"] is None


@pytest.mark.asyncio
async def test_write_appends_across_restarts(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    backend1 = FileAuditBackend(path=path)
    await backend1.write(_make_record(request_id="req-1"))

    backend2 = FileAuditBackend(path=path)
    await backend2.write(_make_record(request_id="req-2"))

    lines = Path(path).read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["request_id"] == "req-1"
    assert json.loads(lines[1])["request_id"] == "req-2"
