import time
import httpx
from datetime import datetime, timezone

from gateway.domain.models import AuthContext, ChatMessage, ChatRequest, ChatResponse, AuditRecord
from gateway.domain.exceptions import (
    SanitizerBlockedError, AdapterNotFoundError, UpstreamTimeoutError, UpstreamError,
)
from gateway.domain.sanitizers.base import SanitizerChain
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

    async def complete(
        self,
        raw_messages: list[dict],
        model: str,
        auth: AuthContext,
        request_id: str,
        adapter_name: str | None = None,
    ) -> ChatResponse:
        start = time.monotonic()
        input_actions: list[str] = []
        sanitized_messages: list[dict] = []

        for msg in raw_messages:
            if isinstance(msg.get("content"), str):
                result = await self._input.run(msg["content"])
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

        try:
            adapter = self._registry.get(adapter_name)
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

        chat_request = ChatRequest(
            model=model,
            messages=[ChatMessage(**m) for m in sanitized_messages],
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

        await self._audit.write(self._record(
            request_id=request_id, auth=auth, adapter=adapter.name, model=response.model,
            prompt_tokens=response.usage.get("prompt_tokens", 0),
            completion_tokens=response.usage.get("completion_tokens", 0),
            latency_ms=_ms(start),
            input_actions=input_actions, output_actions=[],
            status="success",
            messages=sanitized_messages if self._log_body else None,
            completion=response.content if self._log_body else None,
        ))

        return response

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


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
