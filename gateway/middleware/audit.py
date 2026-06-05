import time
import uuid
from datetime import datetime, timezone
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from gateway.audit.base import BaseAuditBackend
from gateway.audit.record import AuditRecord


class AuditLogMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, backend: BaseAuditBackend):
        super().__init__(app)
        self._backend = backend

    async def dispatch(self, request: Request, call_next):
        if request.url.path != "/v1/chat/completions":
            return await call_next(request)

        request_id = str(uuid.uuid4())
        start_time = time.monotonic()
        request.state.request_id = request_id

        response = await call_next(request)

        latency_ms = int((time.monotonic() - start_time) * 1000)
        auth = getattr(request.state, "auth", None)

        record = AuditRecord(
            request_id=request_id,
            timestamp=datetime.now(timezone.utc),
            api_key_id=auth.key_id if auth else "unknown",
            user_id=auth.user_id if auth else None,
            team_id=auth.team_id if auth else None,
            adapter=getattr(request.state, "adapter_name", "unknown"),
            model=getattr(request.state, "model", "unknown"),
            prompt_tokens=getattr(request.state, "prompt_tokens", 0),
            completion_tokens=getattr(request.state, "completion_tokens", 0),
            latency_ms=latency_ms,
            input_actions=getattr(request.state, "input_actions", []),
            output_actions=getattr(request.state, "output_actions", []),
            status=getattr(request.state, "audit_status", "success"),
            error=getattr(request.state, "audit_error", None),
        )

        try:
            await self._backend.write(record)
        except Exception:
            # Intentional: compliance guarantee — if audit fails, client does NOT receive response.
            # BaseHTTPMiddleware buffers the full response body before returning it, so
            # returning a different response here is safe and achieves the guarantee.
            return JSONResponse(
                {
                    "error": {
                        "type": "upstream_error",
                        "message": "Audit write failed",
                        "code": "audit_failed",
                    }
                },
                status_code=500,
            )

        return response
