from __future__ import annotations
import re
from typing import TYPE_CHECKING
from gateway.domain.sanitizers.base import BaseSanitizer, SanitizeResult

if TYPE_CHECKING:
    from gateway.domain.sanitizers.restoration import RestorationContext

_PATTERNS = [
    ("EMAIL", re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")),
    ("CARD", re.compile(r"\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}")),
    ("PHONE", re.compile(r"\+?[\d\-\(\)][\d\s\-\(\)]{8,18}[\d\)]")),
]


class PiiRegexSanitizer(BaseSanitizer):
    def __init__(self, mode: str = "replace"):
        if mode not in ("replace", "block"):
            raise ValueError(f"Invalid mode '{mode}'. Must be 'replace' or 'block'.")
        self._mode = mode

    async def sanitize(
        self, text: str, context: "RestorationContext | None" = None
    ) -> SanitizeResult:
        if self._mode == "replace":
            return self._replace(text, context)
        return self._block(text)

    def _replace(
        self, text: str, context: "RestorationContext | None"
    ) -> SanitizeResult:
        actions = []
        for label, pattern in _PATTERNS:
            def replacer(m, _label=label):
                if context:
                    return context.register(m.group(), _label)
                return f"[{_label}]"
            new_text = pattern.sub(replacer, text)
            if new_text != text:
                text = new_text
                actions.append(f"replaced:{label}")
        return SanitizeResult(text=text, actions=actions)

    def _block(self, text: str) -> SanitizeResult:
        # stops on the first PII type found — by design (spec: block_reason reports first match)
        for label, pattern in _PATTERNS:
            if pattern.search(text):
                return SanitizeResult(
                    text=text,
                    actions=[f"blocked:{label}"],
                    blocked=True,
                    block_reason=f"pii_detected:{label}",
                )
        return SanitizeResult(text=text)
