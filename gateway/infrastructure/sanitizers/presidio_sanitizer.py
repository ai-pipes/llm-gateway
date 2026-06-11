from __future__ import annotations
from typing import TYPE_CHECKING

try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine
    _PRESIDIO_AVAILABLE = True
except ImportError:
    _PRESIDIO_AVAILABLE = False
    AnalyzerEngine = None
    AnonymizerEngine = None

from gateway.domain.sanitizers.base import BaseSanitizer, SanitizeResult

if TYPE_CHECKING:
    from gateway.domain.sanitizers.restoration import RestorationContext


class PresidioSanitizer(BaseSanitizer):
    """NLP-based PII sanitizer using Microsoft Presidio + spaCy.

    Detects names, organizations, locations, emails, phone numbers, credit cards, IPs, and more.
    Requires: pip install presidio-analyzer presidio-anonymizer spacy
              python -m spacy download en_core_web_lg
    """

    def __init__(self, language: str = "en", entities: list[str] | None = None):
        if not _PRESIDIO_AVAILABLE:
            raise ImportError(
                "presidio is not installed. Run: "
                "pip install presidio-analyzer presidio-anonymizer spacy "
                "&& python -m spacy download en_core_web_lg"
            )
        self._analyzer = AnalyzerEngine()
        self._anonymizer = AnonymizerEngine()
        self._language = language
        self._entities = entities  # None = detect all supported entities

    async def sanitize(
        self, text: str, context: "RestorationContext | None" = None
    ) -> SanitizeResult:
        results = self._analyzer.analyze(
            text=text,
            language=self._language,
            entities=self._entities,
        )
        if not results:
            return SanitizeResult(text=text)

        if context is None:
            anonymized = self._anonymizer.anonymize(text=text, analyzer_results=results)
            actions = list({f"replaced:{r.entity_type}" for r in results})
            return SanitizeResult(text=anonymized.text, actions=actions)

        # Resolve overlapping spans: keep highest-confidence (then longest) span per conflict
        kept = _resolve_conflicts(results)

        # Replace spans right-to-left to keep earlier indices valid
        sorted_results = sorted(kept, key=lambda r: r.start, reverse=True)
        anonymized_text = text
        for r in sorted_results:
            original_span = text[r.start:r.end]
            placeholder = context.register(original_span, r.entity_type)
            anonymized_text = (
                anonymized_text[: r.start] + placeholder + anonymized_text[r.end :]
            )

        actions = list({f"replaced:{r.entity_type}" for r in kept})
        return SanitizeResult(text=anonymized_text, actions=actions)


def _resolve_conflicts(results: list) -> list:
    """Return a non-overlapping subset of Presidio results.

    When spans overlap, keep the one with the highest score, breaking ties by longest span.
    """
    # Highest score first, then longest span
    by_priority = sorted(results, key=lambda r: (r.score, r.end - r.start), reverse=True)
    kept: list = []
    for r in by_priority:
        if not any(max(r.start, k.start) < min(r.end, k.end) for k in kept):
            kept.append(r)
    return kept
