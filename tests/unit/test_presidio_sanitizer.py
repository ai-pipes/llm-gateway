import pytest
from unittest.mock import MagicMock, patch


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
