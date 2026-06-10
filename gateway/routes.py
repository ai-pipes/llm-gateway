import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from gateway.adapters.base import ChatMessage, ChatRequest
from gateway.adapters.registry import AdapterRegistry


def create_router(registry: AdapterRegistry) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        # Check if SanitizeMiddleware blocked the request
        if hasattr(request.state, "blocked_error"):
            return JSONResponse(request.state.blocked_error, status_code=400)

        body = await request.json()

        messages = [
            ChatMessage(role=m["role"], content=m["content"])
            for m in body.get("messages", [])
        ]
        chat_request = ChatRequest(
            model=body.get("model", ""),
            messages=messages,
            temperature=body.get("temperature", 0.7),
            stream=body.get("stream", False),
        )

        # Выбор адаптера: клиент может указать явно через поле "adapter"
        adapter_name = body.get("adapter")
        try:
            adapter = registry.get(adapter_name)
        except KeyError:
            request.state.audit_status = "error"
            request.state.audit_error = f"adapter_not_found:{adapter_name}"
            return JSONResponse(
                {"error": {"type": "invalid_request_error", "message": "Adapter not found", "code": "adapter_not_found"}},
                status_code=400,
            )

        request.state.adapter_name = adapter.name
        request.state.model = chat_request.model

        try:
            response = await adapter.chat(chat_request)
        except httpx.TimeoutException:
            request.state.audit_status = "error"
            request.state.audit_error = "upstream_timeout"
            return JSONResponse(
                {"error": {"type": "upstream_error", "message": "LLM request timed out", "code": "upstream_timeout"}},
                status_code=504,
            )
        except Exception as exc:
            request.state.audit_status = "error"
            request.state.audit_error = str(exc)
            return JSONResponse(
                {"error": {"type": "upstream_error", "message": "LLM request failed", "code": "upstream_error"}},
                status_code=502,
            )

        request.state.prompt_tokens = response.usage.get("prompt_tokens", 0)
        request.state.completion_tokens = response.usage.get("completion_tokens", 0)

        request_id = getattr(request.state, "request_id", "")
        return {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion",
            "model": response.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": response.content},
                    "finish_reason": "stop",
                }
            ],
            "usage": response.usage,
        }

    return router
