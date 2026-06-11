from __future__ import annotations
from typing import TYPE_CHECKING
from gateway.domain.sanitizers.base import BaseSanitizer, SanitizeResult

if TYPE_CHECKING:
    from gateway.domain.sanitizers.restoration import RestorationContext


class PassthroughSanitizer(BaseSanitizer):
    async def sanitize(
        self, text: str, context: "RestorationContext | None" = None
    ) -> SanitizeResult:
        return SanitizeResult(text=text)
