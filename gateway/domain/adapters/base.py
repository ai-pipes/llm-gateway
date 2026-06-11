from abc import ABC, abstractmethod
from typing import AsyncGenerator, AsyncIterator
from gateway.domain.models import ChatRequest, ChatResponse


class BaseLLMAdapter(ABC):
    name: str

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResponse:
        ...

    async def stream_chat(
        self, request: ChatRequest, usage_out: dict | None = None
    ) -> AsyncGenerator[str, None]:
        # Default: wraps chat() as a single-chunk stream for adapters that don't
        # support native streaming. Override for true token-by-token streaming.
        response = await self.chat(request)
        yield response.content
