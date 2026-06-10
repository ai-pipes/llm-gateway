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
        response = await self.chat(request)
        yield response.content
