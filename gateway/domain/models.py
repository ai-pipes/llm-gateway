from dataclasses import dataclass
from datetime import datetime


@dataclass
class AuthContext:
    key_id: str
    user_id: str | None
    team_id: str | None


@dataclass
class ChatMessage:
    role: str
    content: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None


@dataclass
class ChatRequest:
    model: str
    messages: list[ChatMessage]
    temperature: float = 0.7
    stream: bool = False
    tools: list[dict] | None = None


@dataclass
class ChatResponse:
    model: str
    usage: dict
    content: str | None = None
    tool_calls: list[dict] | None = None


@dataclass
class AuditRecord:
    request_id: str
    timestamp: datetime
    api_key_id: str
    user_id: str | None
    team_id: str | None
    adapter: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    input_actions: list[str]
    output_actions: list[str]
    status: str
    error: str | None = None
    messages: list[dict] | None = None
    completion: str | None = None
