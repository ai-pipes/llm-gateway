from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class ChatMessage:
    role: str
    content: str


@dataclass
class ChatRequest:
    model: str
    messages: list[ChatMessage]
    temperature: float = 0.7
    stream: bool = False


@dataclass
class ChatResponse:
    content: str
    model: str
    usage: dict


class BaseLLMAdapter(ABC):
    name: str

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResponse:
        ...

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[str]:
        response = await self.chat(request)
        yield response.content
