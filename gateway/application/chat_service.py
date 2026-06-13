import dataclasses
import json
import time
import httpx
from datetime import datetime, timezone
from typing import AsyncGenerator

from gateway.domain.models import AuthContext, ChatMessage, ChatRequest, ChatResponse, AuditRecord
from gateway.domain.exceptions import (
    SanitizerBlockedError, AdapterNotFoundError, UpstreamTimeoutError, UpstreamError,
)
from gateway.domain.sanitizers.base import SanitizerChain
from gateway.domain.sanitizers.restoration import RestorationContext, StreamingRestorer
from gateway.domain.audit.base import BaseAuditBackend
from gateway.infrastructure.adapters.registry import AdapterRegistry


class ChatService:
    def __init__(
        self,
        input_chain: SanitizerChain,
        output_chain: SanitizerChain,
        registry: AdapterRegistry,
        audit: BaseAuditBackend,
        log_body: bool,
    ):
        self._input = input_chain
        self._output = output_chain
        self._registry = registry
        self._audit = audit
        self._log_body = log_body

    async def _sanitize_input(
        self,
        raw_messages: list[dict],
        model: str,
        auth: AuthContext,
        request_id: str,
        start: float,
        context: RestorationContext | None = None,
    ) -> tuple[list[dict], list[str]]:
        """Returns (sanitized_messages, input_actions). Raises SanitizerBlockedError if blocked."""
        input_actions: list[str] = []
        sanitized_messages: list[dict] = []
        for msg in raw_messages:
            if isinstance(msg.get("content"), str):
                result = await self._input.run(msg["content"], context)
                input_actions.extend(result.actions)
                if result.blocked:
                    await self._audit.write(self._record(
                        request_id=request_id, auth=auth, adapter="unknown", model=model,
                        prompt_tokens=0, completion_tokens=0, latency_ms=_ms(start),
                        input_actions=input_actions, output_actions=[],
                        status="blocked",
                        messages=sanitized_messages if self._log_body else None,
                        completion=None,
                    ))
                    raise SanitizerBlockedError(result.block_reason)
                sanitized_messages.append({**msg, "content": result.text})
            else:
                sanitized_messages.append(msg)
        return sanitized_messages, input_actions

    async def _resolve_adapter(
        self,
        adapter_name: str | None,
        model: str,
        auth: AuthContext,
        request_id: str,
        start: float,
        input_actions: list[str],
        sanitized_messages: list[dict],
    ):
        """Returns adapter. Raises AdapterNotFoundError if not found."""
        try:
            return self._registry.get(adapter_name)
        except KeyError:
            await self._audit.write(self._record(
                request_id=request_id, auth=auth, adapter="unknown", model=model,
                prompt_tokens=0, completion_tokens=0, latency_ms=_ms(start),
                input_actions=input_actions, output_actions=[],
                status="error", error=f"adapter_not_found:{adapter_name}",
                messages=sanitized_messages if self._log_body else None,
                completion=None,
            ))
            raise AdapterNotFoundError(adapter_name)

    async def complete(
        self,
        raw_messages: list[dict],
        model: str,
        auth: AuthContext,
        request_id: str,
        adapter_name: str | None = None,
        tools: list[dict] | None = None,
        temperature: float | None = None,
    ) -> ChatResponse:
        start = time.monotonic()
        context = RestorationContext()
        sanitized_messages, input_actions = await self._sanitize_input(
            raw_messages, model, auth, request_id, start, context=context
        )
        if context.has_replacements():
            sanitized_messages = _inject_system_instruction(
                sanitized_messages, context.build_system_instruction()
            )
        adapter = await self._resolve_adapter(
            adapter_name, model, auth, request_id, start, input_actions, sanitized_messages
        )

        chat_request = ChatRequest(
            model=model,
            messages=[ChatMessage(**m) for m in sanitized_messages],
            tools=tools,
            temperature=temperature,
        )

        try:
            response = await adapter.chat(chat_request)
        except httpx.TimeoutException:
            await self._audit.write(self._record(
                request_id=request_id, auth=auth, adapter=adapter.name, model=model,
                prompt_tokens=0, completion_tokens=0, latency_ms=_ms(start),
                input_actions=input_actions, output_actions=[],
                status="error", error="upstream_timeout",
                messages=sanitized_messages if self._log_body else None,
                completion=None,
            ))
            raise UpstreamTimeoutError()
        except Exception as exc:
            await self._audit.write(self._record(
                request_id=request_id, auth=auth, adapter=adapter.name, model=model,
                prompt_tokens=0, completion_tokens=0, latency_ms=_ms(start),
                input_actions=input_actions, output_actions=[],
                status="error", error=str(exc),
                messages=sanitized_messages if self._log_body else None,
                completion=None,
            ))
            raise UpstreamError(str(exc)) from exc

        output_actions: list[str] = []

        # Save content for audit BEFORE restoring PII (audit must not log raw PII)
        sanitized_content = response.content
        if context.has_replacements():
            if response.content is not None:
                response = dataclasses.replace(response, content=context.restore(response.content))
            if response.tool_calls is not None:
                restored = json.loads(context.restore(json.dumps(response.tool_calls)))
                response = dataclasses.replace(response, tool_calls=restored)

        await self._audit.write(self._record(
            request_id=request_id, auth=auth, adapter=adapter.name, model=response.model,
            prompt_tokens=response.usage.get("prompt_tokens", 0),
            completion_tokens=response.usage.get("completion_tokens", 0),
            latency_ms=_ms(start),
            input_actions=input_actions, output_actions=output_actions,
            status="success",
            messages=sanitized_messages if self._log_body else None,
            completion=sanitized_content if self._log_body else None,
        ))

        return response

    async def complete_stream(
        self,
        raw_messages: list[dict],
        model: str,
        auth: AuthContext,
        request_id: str,
        adapter_name: str | None = None,
        tools: list[dict] | None = None,
        temperature: float | None = None,
    ) -> AsyncGenerator[str | dict, None]:
        start = time.monotonic()
        context = RestorationContext()
        sanitized_messages, input_actions = await self._sanitize_input(
            raw_messages, model, auth, request_id, start, context=context
        )
        if context.has_replacements():
            sanitized_messages = _inject_system_instruction(
                sanitized_messages, context.build_system_instruction()
            )
        adapter = await self._resolve_adapter(
            adapter_name, model, auth, request_id, start, input_actions, sanitized_messages
        )

        chat_request = ChatRequest(
            model=model,
            messages=[ChatMessage(**m) for m in sanitized_messages],
            stream=True,
            tools=tools,
            temperature=temperature,
        )

        restorer = StreamingRestorer(context) if context.has_replacements() else None
        chunks: list[str] = []          # raw chunks for audit (placeholder version, not restored)
        usage_out: dict = {}             # populated by adapter from the trailing usage SSE chunk
        status = "success"
        error: str | None = None
        stream_complete = False          # True only when the async for loop exits normally
        output_actions: list[str] = []
        # Per tool_call index restorer: buffers only around placeholder boundaries,
        # emits everything else immediately — preserving true argument streaming.
        tc_restorers: dict[int, StreamingRestorer] = {}

        try:
            async for chunk in adapter.stream_chat(chat_request, usage_out=usage_out):
                if isinstance(chunk, str):
                    chunks.append(chunk)
                    if restorer:
                        safe = restorer.feed(chunk)
                        if safe:
                            yield safe
                    else:
                        yield chunk
                else:
                    if context.has_replacements():
                        out_tcs = []
                        for tc in chunk.get("tool_calls", []):
                            idx = tc.get("index", 0)
                            func = tc.get("function", {})
                            if "arguments" in func:
                                if idx not in tc_restorers:
                                    tc_restorers[idx] = StreamingRestorer(context)
                                safe = tc_restorers[idx].feed(func["arguments"])
                                out_tcs.append({**tc, "function": {**func, "arguments": safe}})
                            else:
                                out_tcs.append(tc)
                        yield {**chunk, "tool_calls": out_tcs}
                    else:
                        yield chunk

            # Flush any remaining buffered placeholder prefix per index
            for idx in sorted(tc_restorers.keys()):
                tail = tc_restorers[idx].finalize()
                if tail:
                    yield {"tool_calls": [{"index": idx, "function": {"arguments": tail}}]}

            if restorer:
                tail = restorer.finalize()
                if tail:
                    yield tail
            stream_complete = True

        except httpx.TimeoutException:
            status = "error"
            error = "upstream_timeout"
            raise UpstreamTimeoutError()
        except Exception as exc:
            status = "error"
            error = str(exc)
            raise UpstreamError(str(exc)) from exc
        finally:
            # finally runs on success, raised exception, AND GeneratorExit (client disconnect).
            # GeneratorExit is a BaseException — a bare `except Exception` would miss it.
            if not stream_complete and status == "success":
                status = "cancelled"    # loop interrupted before normal exit → client disconnected
            full_text = "".join(chunks)
            completion: str | None = None

            if self._log_body and full_text:
                completion = full_text

            await self._audit.write(self._record(
                request_id=request_id,
                auth=auth,
                adapter=adapter.name,
                model=model,
                prompt_tokens=usage_out.get("prompt_tokens", 0),
                completion_tokens=usage_out.get("completion_tokens", 0),
                latency_ms=_ms(start),
                input_actions=input_actions,
                output_actions=output_actions,
                status=status,
                error=error,
                messages=sanitized_messages if self._log_body else None,
                completion=completion,
            ))

    def _record(
        self,
        request_id: str,
        auth: AuthContext,
        adapter: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        input_actions: list[str],
        output_actions: list[str],
        status: str,
        error: str | None = None,
        messages: list[dict] | None = None,
        completion: str | None = None,
    ) -> AuditRecord:
        return AuditRecord(
            request_id=request_id,
            timestamp=datetime.now(timezone.utc),
            api_key_id=auth.key_id,
            user_id=auth.user_id,
            team_id=auth.team_id,
            adapter=adapter,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            input_actions=input_actions,
            output_actions=output_actions,
            status=status,
            error=error,
            messages=messages,
            completion=completion,
        )


def _inject_system_instruction(messages: list[dict], instruction: str) -> list[dict]:
    for i, msg in enumerate(messages):
        if msg.get("role") == "system" and isinstance(msg.get("content"), str):
            updated = {**msg, "content": msg["content"] + "\n\n" + instruction}
            return messages[:i] + [updated] + messages[i + 1:]
    return [{"role": "system", "content": instruction}] + messages


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
