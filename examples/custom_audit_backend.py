"""
Example custom audit backend — HTTP POST to an external audit service.

Steps to use:
  1. Copy this file to your project
  2. Implement the write() method to send records to your system
  3. Configure in gateway.yaml:
       audit:
         type: plugin
         module: "my_backends.http_audit.HttpAuditBackend"
         config:
           endpoint: "https://audit.corp.internal/v1/events"
           token_env: AUDIT_TOKEN
"""
import json
import os
import httpx
from gateway.domain.audit.base import BaseAuditBackend
from gateway.domain.models import AuditRecord


class HttpAuditBackend(BaseAuditBackend):
    """
    Replace this class with your own implementation.
    The only required method is write().

    write() is called once per request — after success, error, or client disconnect.
    It is always called from a finally block, so it must not raise exceptions.
    """

    def __init__(self, endpoint: str, token_env: str = "AUDIT_TOKEN"):
        self._endpoint = endpoint
        self._token = os.environ.get(token_env, "")

    async def write(self, record: AuditRecord) -> None:
        payload = {
            "request_id": record.request_id,
            "timestamp": record.timestamp.isoformat(),
            "user_id": record.user_id,
            "team_id": record.team_id,
            "adapter": record.adapter,
            "model": record.model,
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "latency_ms": record.latency_ms,
            "status": record.status,
            "error": record.error,
            "input_actions": record.input_actions,
            "output_actions": record.output_actions,
            # messages and completion are only present when body_logging.enabled: true
            "messages": record.messages,
            "completion": record.completion,
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    self._endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except Exception:
            # Audit failures must never crash the gateway or affect the response.
            # Log to stderr as a fallback so records aren't silently lost.
            import sys
            print(f"[audit] failed to POST to {self._endpoint}", file=sys.stderr)
            print(json.dumps(payload), file=sys.stderr)
