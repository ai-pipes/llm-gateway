import uuid
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from gateway.application.chat_service import ChatService
from gateway.domain.exceptions import (
    SanitizerBlockedError, AdapterNotFoundError, UpstreamTimeoutError, UpstreamError,
)


def create_router(chat_service: ChatService) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        request_id = str(uuid.uuid4())

        try:
            response = await chat_service.complete(
                raw_messages=body.get("messages", []),
                model=body.get("model", ""),
                auth=request.state.auth,
                request_id=request_id,
                adapter_name=body.get("adapter"),
            )
        except SanitizerBlockedError as e:
            return JSONResponse(
                {"error": {"type": "invalid_request_error",
                           "message": f"Input blocked: {e.reason}",
                           "code": "sanitizer_blocked"}},
                status_code=400,
            )
        except AdapterNotFoundError:
            return JSONResponse(
                {"error": {"type": "invalid_request_error",
                           "message": "Adapter not found",
                           "code": "adapter_not_found"}},
                status_code=400,
            )
        except UpstreamTimeoutError:
            return JSONResponse(
                {"error": {"type": "upstream_error",
                           "message": "LLM request timed out",
                           "code": "upstream_timeout"}},
                status_code=504,
            )
        except UpstreamError as e:
            return JSONResponse(
                {"error": {"type": "upstream_error",
                           "message": "LLM request failed",
                           "code": "upstream_error"}},
                status_code=502,
            )

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
