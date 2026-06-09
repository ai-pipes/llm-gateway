try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine
    _PRESIDIO_AVAILABLE = True
except ImportError:
    _PRESIDIO_AVAILABLE = False
    AnalyzerEngine = None
    AnonymizerEngine = None

from gateway.domain.sanitizers.base import BaseSanitizer, SanitizeResult


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

    async def sanitize(self, text: str) -> SanitizeResult:
        results = self._analyzer.analyze(
            text=text,
            language=self._language,
            entities=self._entities,
        )
        if not results:
            return SanitizeResult(text=text)

        anonymized = self._anonymizer.anonymize(text=text, analyzer_results=results)
        actions = list({f"replaced:{r.entity_type}" for r in results})
        return SanitizeResult(text=anonymized.text, actions=actions)
