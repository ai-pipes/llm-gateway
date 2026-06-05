from dataclasses import dataclass
from datetime import datetime


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
    status: str  # "success" | "error" | "blocked"
    error: str | None = None  # при status=error: сообщение исключения или HTTP статус upstream
