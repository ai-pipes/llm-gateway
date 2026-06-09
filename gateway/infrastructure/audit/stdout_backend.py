import json
import sys
from dataclasses import asdict
from gateway.domain.audit.base import BaseAuditBackend
from gateway.domain.models import AuditRecord


class StdoutAuditBackend(BaseAuditBackend):
    async def write(self, record: AuditRecord) -> None:
        data = asdict(record)
        data["timestamp"] = record.timestamp.isoformat()
        print(json.dumps(data), file=sys.stdout, flush=True)
