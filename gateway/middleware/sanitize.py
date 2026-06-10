import json
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from gateway.sanitizers.base import SanitizerChain


class SanitizeMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, input_chain: SanitizerChain, output_chain: SanitizerChain):
        super().__init__(app)
        self._input = input_chain
        self._output = output_chain

    async def dispatch(self, request: Request, call_next):
        input_actions: list[str] = []

        if request.method == "POST" and request.url.path == "/v1/chat/completions":
            body_bytes = await request.body()
            try:
                data = json.loads(body_bytes)
            except (json.JSONDecodeError, ValueError):
                return JSONResponse(
                    {"error": {"type": "invalid_request_error", "message": "Invalid JSON", "code": "invalid_json"}},
                    status_code=400,
                )

            messages = data.get("messages", [])
            for i, msg in enumerate(messages):
                # TODO v2: list-typed content (multimodal: text + images) bypasses sanitization
                if isinstance(msg.get("content"), str):
                    result = await self._input.run(msg["content"])
                    messages[i]["content"] = result.text
                    input_actions.extend(result.actions)
                    if result.blocked:
                        request.state.input_actions = input_actions
                        request.state.audit_status = "blocked"
                        # Store blocked error — route handler will return this as 400
                        # We still call call_next so AuditLogMiddleware (innermost) runs
                        request.state.blocked_error = {
                            "error": {
                                "type": "invalid_request_error",
                                "message": f"Input blocked: {result.block_reason}",
                                "code": "sanitizer_blocked",
                            }
                        }
                        new_body = json.dumps(data).encode()

                        async def receive():
                            return {"type": "http.request", "body": new_body, "more_body": False}

                        request._receive = receive
                        response = await call_next(request)
                        request.state.output_actions = []
                        return response

            new_body = json.dumps(data).encode()

            async def receive():
                return {"type": "http.request", "body": new_body, "more_body": False}

            request._receive = receive

        request.state.input_actions = input_actions
        response = await call_next(request)

        # Output sanitization — v1: empty chain, tracked for audit
        request.state.output_actions = []

        return response
