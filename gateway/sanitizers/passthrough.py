from .base import BaseSanitizer, SanitizeResult


class PassthroughSanitizer(BaseSanitizer):
    """No-op sanitizer. Use as reference implementation and in tests."""

    async def sanitize(self, text: str) -> SanitizeResult:
        return SanitizeResult(text=text)
