import httpx
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

    async def chat(self, request: ChatRequest) -> ChatResponse:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": request.model,
                    "messages": [
                        {"role": m.role, "content": m.content}
                        for m in request.messages
                    ],
                    "temperature": request.temperature,
                },
            )
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices") or []
        if not choices:
            raise ValueError(f"LLM response contained no choices: {data}")
        return ChatResponse(
            content=choices[0]["message"]["content"],
            model=data.get("model", request.model),
            usage=data.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
