import json
import httpx
from typing import AsyncGenerator
from gateway.domain.adapters.base import BaseLLMAdapter
from gateway.domain.models import ChatRequest, ChatResponse


class OpenAICompatibleAdapter(BaseLLMAdapter):
    """
    Reference implementation: работает с любым OpenAI-compatible endpoint.
    Настраивается через gateway.yaml без написания кода.
    """

    def __init__(self, name: str, base_url: str, api_key: str, timeout: float = 30.0):
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"OpenAICompatibleAdapter(name={self.name!r}, base_url={self._base_url!r})"

    def _build_payload(self, request: ChatRequest, stream: bool = False) -> dict:
        messages = []
        for m in request.messages:
            msg: dict = {"role": m.role, "content": m.content}
            if m.tool_calls is not None:
                msg["tool_calls"] = m.tool_calls
            if m.tool_call_id is not None:
                msg["tool_call_id"] = m.tool_call_id
            messages.append(msg)

        payload: dict = {
            "model": request.model,
            "messages": messages,
            "temperature": request.temperature,
        }
        if request.tools:
            payload["tools"] = request.tools
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def chat(self, request: ChatRequest) -> ChatResponse:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=self._build_payload(request),
            )
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices") or []
        if not choices:
            raise ValueError(f"LLM response contained no choices: {data}")
        message = choices[0].get("message", {})
        return ChatResponse(
            model=data.get("model", request.model),
            usage=data.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            content=message.get("content"),
            tool_calls=message.get("tool_calls"),
        )

    async def stream_chat(
        self, request: ChatRequest, usage_out: dict | None = None
    ) -> AsyncGenerator[str | dict, None]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=self._build_payload(request, stream=True),
            ) as response:
                response.raise_for_status()
                stream_done = False
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload == "[DONE]":
                        stream_done = True  # keep iterating in case usage chunk follows
                        continue
                    data = json.loads(payload)
                    if usage_out is not None and data.get("usage"):
                        usage_out.update(data["usage"])
                    if stream_done:
                        continue  # don't yield content after [DONE]
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content
                    if delta.get("tool_calls"):
                        yield {"tool_calls": delta["tool_calls"]}
