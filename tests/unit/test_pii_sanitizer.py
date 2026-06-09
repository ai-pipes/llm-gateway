import pytest
from gateway.infrastructure.sanitizers.pii_regex import PiiRegexSanitizer


@pytest.mark.asyncio
async def test_replace_email():
    s = PiiRegexSanitizer(mode="replace")
    result = await s.sanitize("Contact us at alice@example.com for help.")
    assert result.text == "Contact us at [EMAIL] for help."
    assert result.blocked is False


@pytest.mark.asyncio
async def test_replace_phone():
    s = PiiRegexSanitizer(mode="replace")
    result = await s.sanitize("Call me at +1 (555) 123-4567 please.")
    assert result.text == "Call me at [PHONE] please."
    assert result.blocked is False


@pytest.mark.asyncio
async def test_replace_card():
    s = PiiRegexSanitizer(mode="replace")
    result = await s.sanitize("Card: 4111 1111 1111 1111")
    assert result.text == "Card: [CARD]"
    assert result.blocked is False


@pytest.mark.asyncio
async def test_replace_multiple_types():
    s = PiiRegexSanitizer(mode="replace")
    result = await s.sanitize("Email: bob@corp.com, card: 4111-1111-1111-1111")
    assert "[EMAIL]" in result.text
    assert "[CARD]" in result.text
    assert result.blocked is False


@pytest.mark.asyncio
async def test_replace_actions_contain_labels():
    s = PiiRegexSanitizer(mode="replace")
    result = await s.sanitize("bob@corp.com and alice@corp.com paid 4111 1111 1111 1111")
    # два email → одна запись "replaced:EMAIL" (дедупликация по типу)
    assert result.actions.count("replaced:EMAIL") == 1
    assert "replaced:CARD" in result.actions
    assert result.blocked is False


@pytest.mark.asyncio
async def test_no_pii_passthrough():
    s = PiiRegexSanitizer(mode="replace")
    original = "Hello, how are you today?"
    result = await s.sanitize(original)
    assert result.text == original
    assert result.actions == []
    assert result.blocked is False


@pytest.mark.asyncio
async def test_block_mode_returns_blocked():
    s = PiiRegexSanitizer(mode="block")
    result = await s.sanitize("My email is secret@corp.com")
    assert result.blocked is True
    assert result.block_reason == "pii_detected:EMAIL"
    assert result.actions == ["blocked:EMAIL"]
    # текст возвращается без изменений
    assert "secret@corp.com" in result.text


@pytest.mark.asyncio
async def test_block_mode_no_pii_passes():
    s = PiiRegexSanitizer(mode="block")
    original = "Summarize this document for me."
    result = await s.sanitize(original)
    assert result.blocked is False
    assert result.text == original
    assert result.actions == []


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        PiiRegexSanitizer(mode="redact")


@pytest.mark.asyncio
async def test_block_mode_stops_on_first_pii_type():
    s = PiiRegexSanitizer(mode="block")
    # email comes before card in pattern order → EMAIL reported even when card also present
    result = await s.sanitize("Email bob@corp.com, card 4111 1111 1111 1111")
    assert result.blocked is True
    assert result.block_reason == "pii_detected:EMAIL"
    assert result.actions == ["blocked:EMAIL"]
