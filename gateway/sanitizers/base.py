from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SanitizeResult:
    text: str
    actions: list[str] = field(default_factory=list)
    blocked: bool = False
    block_reason: str = ""


class BaseSanitizer(ABC):
    @abstractmethod
    async def sanitize(self, text: str) -> SanitizeResult:
        ...


class SanitizerChain:
    def __init__(self, sanitizers: list[BaseSanitizer]):
        self._sanitizers = sanitizers

    async def run(self, text: str) -> SanitizeResult:
        result = SanitizeResult(text=text)
        for s in self._sanitizers:
            r = await s.sanitize(result.text)
            result.text = r.text
            result.actions.extend(r.actions)
            if r.blocked:
                result.blocked = True
                result.block_reason = r.block_reason
                break
        return result
