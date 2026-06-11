import re
import pytest
from unittest.mock import MagicMock, patch
from gateway.domain.sanitizers.restoration import RestorationContext


def _make_sanitizer(analyzer_results, anonymized_text):
    """Create a PresidioSanitizer with mocked Presidio engines."""
    mock_analyzer = MagicMock()
    mock_analyzer.analyze.return_value = analyzer_results

    mock_anonymized = MagicMock()
    mock_anonymized.text = anonymized_text
    mock_anonymizer = MagicMock()
    mock_anonymizer.anonymize.return_value = mock_anonymized

    with patch("gateway.infrastructure.sanitizers.presidio_sanitizer.AnalyzerEngine") as MockA, \
         patch("gateway.infrastructure.sanitizers.presidio_sanitizer.AnonymizerEngine") as MockB, \
         patch("gateway.infrastructure.sanitizers.presidio_sanitizer._PRESIDIO_AVAILABLE", True):
        MockA.return_value = mock_analyzer
        MockB.return_value = mock_anonymizer
        from gateway.infrastructure.sanitizers.presidio_sanitizer import PresidioSanitizer
        sanitizer = PresidioSanitizer()
    return sanitizer


def _result(entity_type):
    r = MagicMock()
    r.entity_type = entity_type
    return r


def _result_with_span(entity_type, start, end, score=1.0):
    r = MagicMock()
    r.entity_type = entity_type
    r.start = start
    r.end = end
    r.score = score
    return r


@pytest.mark.asyncio
async def test_no_pii_returns_original_text():
    sanitizer = _make_sanitizer(analyzer_results=[], anonymized_text="hello world")
    result = await sanitizer.sanitize("hello world")
    assert result.text == "hello world"
    assert result.actions == []
    assert not result.blocked


@pytest.mark.asyncio
async def test_email_detected_and_replaced():
    sanitizer = _make_sanitizer(
        analyzer_results=[_result("EMAIL_ADDRESS")],
        anonymized_text="contact <EMAIL_ADDRESS>",
    )
    result = await sanitizer.sanitize("contact user@example.com")
    assert result.text == "contact <EMAIL_ADDRESS>"
    assert "replaced:EMAIL_ADDRESS" in result.actions
    assert not result.blocked


@pytest.mark.asyncio
async def test_person_name_detected():
    sanitizer = _make_sanitizer(
        analyzer_results=[_result("PERSON")],
        anonymized_text="<PERSON> joined the team",
    )
    result = await sanitizer.sanitize("John Smith joined the team")
    assert result.text == "<PERSON> joined the team"
    assert "replaced:PERSON" in result.actions


@pytest.mark.asyncio
async def test_multiple_entity_types_deduplicated_in_actions():
    sanitizer = _make_sanitizer(
        analyzer_results=[_result("EMAIL_ADDRESS"), _result("EMAIL_ADDRESS"), _result("PERSON")],
        anonymized_text="<PERSON> email <EMAIL_ADDRESS>",
    )
    result = await sanitizer.sanitize("John email john@x.com")
    assert "replaced:EMAIL_ADDRESS" in result.actions
    assert "replaced:PERSON" in result.actions
    assert result.actions.count("replaced:EMAIL_ADDRESS") == 1  # deduplicated


@pytest.mark.asyncio
async def test_entities_filter_passed_to_analyzer():
    sanitizer = _make_sanitizer(analyzer_results=[], anonymized_text="text")
    sanitizer._entities = ["EMAIL_ADDRESS", "PHONE_NUMBER"]
    await sanitizer.sanitize("some text")
    sanitizer._analyzer.analyze.assert_called_once_with(
        text="some text",
        language="en",
        entities=["EMAIL_ADDRESS", "PHONE_NUMBER"],
    )


@pytest.mark.asyncio
async def test_with_context_registers_span_and_replaces():
    text = "Contact user@example.com for support."
    # email is at index 8..24
    email_span = _result_with_span("EMAIL_ADDRESS", 8, 24)
    sanitizer = _make_sanitizer(
        analyzer_results=[email_span],
        anonymized_text="unused",  # context path doesn't use anonymizer
    )
    ctx = RestorationContext()
    result = await sanitizer.sanitize(text, ctx)

    assert ctx.has_replacements()
    assert "user@example.com" not in result.text
    assert re.search(r"\[EMAIL_ADDRESS_[0-9a-f]{8}\]", result.text)
    assert "replaced:EMAIL_ADDRESS" in result.actions


@pytest.mark.asyncio
async def test_with_context_restore_roundtrip():
    text = "Contact user@example.com for support."
    email_span = _result_with_span("EMAIL_ADDRESS", 8, 24)
    sanitizer = _make_sanitizer(analyzer_results=[email_span], anonymized_text="unused")
    ctx = RestorationContext()
    result = await sanitizer.sanitize(text, ctx)
    restored = ctx.restore(result.text)
    assert "user@example.com" in restored


@pytest.mark.asyncio
async def test_with_context_two_spans_replaced_correctly():
    # "John Smith" at 0-10, "john@x.com" at 18-28
    text = "John Smith emailed john@x.com today."
    spans = [
        _result_with_span("PERSON", 0, 10),
        _result_with_span("EMAIL_ADDRESS", 18, 28),
    ]
    sanitizer = _make_sanitizer(analyzer_results=spans, anonymized_text="unused")
    ctx = RestorationContext()
    result = await sanitizer.sanitize(text, ctx)
    restored = ctx.restore(result.text)
    assert "John Smith" in restored
    assert "john@x.com" in restored


@pytest.mark.asyncio
async def test_with_context_overlapping_spans_keeps_higher_confidence():
    # EMAIL_ADDRESS (score=1.0) overlaps with URL (score=0.5) — email wins
    text = "Register at user@techcorp.io today."
    spans = [
        _result_with_span("EMAIL_ADDRESS", 12, 29, score=1.0),
        _result_with_span("URL", 17, 29, score=0.5),  # overlaps with email
    ]
    sanitizer = _make_sanitizer(analyzer_results=spans, anonymized_text="unused")
    ctx = RestorationContext()
    result = await sanitizer.sanitize(text, ctx)
    restored = ctx.restore(result.text)
    assert "user@techcorp.io" in restored
    # Only one replacement should exist (email wins, url discarded)
    assert "replaced:EMAIL_ADDRESS" in result.actions
    assert "replaced:URL" not in result.actions


@pytest.mark.asyncio
async def test_without_context_uses_anonymizer_engine():
    sanitizer = _make_sanitizer(
        analyzer_results=[_result("EMAIL_ADDRESS")],
        anonymized_text="contact <EMAIL_ADDRESS>",
    )
    result = await sanitizer.sanitize("contact user@example.com")
    # falls back to anonymizer
    assert result.text == "contact <EMAIL_ADDRESS>"
    sanitizer._anonymizer.anonymize.assert_called_once()
