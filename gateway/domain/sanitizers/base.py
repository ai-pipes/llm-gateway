from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.domain.sanitizers.restoration import RestorationContext


@dataclass
class SanitizeResult:
    text: str
    actions: list[str] = field(default_factory=list)
    blocked: bool = False
    block_reason: str = ""


class BaseSanitizer(ABC):
    @abstractmethod
    async def sanitize(
        self, text: str, context: "RestorationContext | None" = None
    ) -> SanitizeResult:
        ...


class SanitizerChain:
    def __init__(self, sanitizers: list[BaseSanitizer]):
        self._sanitizers = sanitizers

    def __bool__(self) -> bool:
        return bool(self._sanitizers)

    async def run(
        self, text: str, context: "RestorationContext | None" = None
    ) -> SanitizeResult:
        result = SanitizeResult(text=text)
        for s in self._sanitizers:
            r = await s.sanitize(result.text, context)
            result.text = r.text
            result.actions.extend(r.actions)
            if r.blocked:
                result.blocked = True
                result.block_reason = r.block_reason
                break
        return result
