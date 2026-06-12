import json
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

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

        if body.get("stream"):
            return StreamingResponse(
                _sse_stream(chat_service, body, request, request_id),
                media_type="text/event-stream",
            )

        try:
            response = await chat_service.complete(
                raw_messages=body.get("messages", []),
                model=body.get("model", ""),
                auth=request.state.auth,
                request_id=request_id,
                adapter_name=body.get("adapter"),
                tools=body.get("tools"),
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
        except UpstreamError:
            return JSONResponse(
                {"error": {"type": "upstream_error",
                           "message": "LLM request failed",
                           "code": "upstream_error"}},
                status_code=502,
            )

        finish_reason = "tool_calls" if response.tool_calls else "stop"
        message: dict = {"role": "assistant", "content": response.content}
        if response.tool_calls:
            message["tool_calls"] = response.tool_calls

        return {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion",
            "model": response.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": response.usage,
        }

    return router


async def _sse_stream(
    chat_service: ChatService,
    body: dict,
    request: Request,
    request_id: str,
) -> AsyncIterator[str]:
    chunk_id = f"chatcmpl-{request_id}"
    model = body.get("model", "")

    def _chunk(content: str, finish_reason=None, delta: dict | None = None) -> str:
        payload = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": delta if delta is not None else {"content": content}, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(payload)}\n\n"

    def _error(code: str, message: str) -> str:
        payload = {"error": {"type": "gateway_error", "code": code, "message": message}}
        return f"data: {json.dumps(payload)}\n\n"

    try:
        has_tool_calls = False
        async for chunk in chat_service.complete_stream(
            raw_messages=body.get("messages", []),
            model=model,
            auth=request.state.auth,
            request_id=request_id,
            adapter_name=body.get("adapter"),
            tools=body.get("tools"),
        ):
            if isinstance(chunk, str):
                yield _chunk(chunk)
            else:
                if "tool_calls" in chunk:
                    has_tool_calls = True
                yield _chunk("", delta=chunk)

        finish_reason = "tool_calls" if has_tool_calls else "stop"
        yield _chunk("", finish_reason=finish_reason, delta={})

    except SanitizerBlockedError as e:
        yield _error("sanitizer_blocked", f"Input blocked: {e.reason}")
    except AdapterNotFoundError:
        yield _error("adapter_not_found", "Adapter not found")
    except UpstreamTimeoutError:
        yield _error("upstream_timeout", "LLM request timed out")
    except UpstreamError:
        yield _error("upstream_error", "LLM request failed")
    except Exception:
        yield _error("internal_error", "An unexpected error occurred")
    finally:
        yield "data: [DONE]\n\n"
