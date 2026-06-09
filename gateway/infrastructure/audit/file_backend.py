import json
from pathlib import Path
from dataclasses import asdict
from gateway.domain.audit.base import BaseAuditBackend
from gateway.domain.models import AuditRecord


class FileAuditBackend(BaseAuditBackend):
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a", encoding="utf-8")

    async def write(self, record: AuditRecord) -> None:
        data = asdict(record)
        data["timestamp"] = record.timestamp.isoformat()
        self._file.write(json.dumps(data) + "\n")
        self._file.flush()
