from gateway.domain.sanitizers.base import BaseSanitizer, SanitizeResult


class PassthroughSanitizer(BaseSanitizer):
    async def sanitize(self, text: str) -> SanitizeResult:
        return SanitizeResult(text=text)
