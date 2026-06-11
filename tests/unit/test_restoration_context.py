import re
import pytest
from gateway.domain.sanitizers.restoration import RestorationContext, StreamingRestorer


def test_register_returns_placeholder_with_correct_format():
    ctx = RestorationContext()
    placeholder = ctx.register("john@example.com", "EMAIL")
    assert re.fullmatch(r"\[EMAIL_[0-9a-f]{8}\]", placeholder)


def test_register_same_value_returns_same_placeholder():
    ctx = RestorationContext()
    p1 = ctx.register("john@example.com", "EMAIL")
    p2 = ctx.register("john@example.com", "EMAIL")
    assert p1 == p2


def test_register_different_values_return_different_placeholders():
    ctx = RestorationContext()
    p1 = ctx.register("john@example.com", "EMAIL")
    p2 = ctx.register("jane@example.com", "EMAIL")
    assert p1 != p2


def test_restore_replaces_placeholder_with_original():
    ctx = RestorationContext()
    placeholder = ctx.register("john@example.com", "EMAIL")
    restored = ctx.restore(f"Contact {placeholder} for info.")
    assert restored == "Contact john@example.com for info."


def test_restore_multiple_values():
    ctx = RestorationContext()
    p_email = ctx.register("john@example.com", "EMAIL")
    p_phone = ctx.register("+1234567890", "PHONE")
    text = f"Email: {p_email}, Phone: {p_phone}"
    assert ctx.restore(text) == "Email: john@example.com, Phone: +1234567890"


def test_restore_no_replacements_returns_text_unchanged():
    ctx = RestorationContext()
    assert ctx.restore("hello world") == "hello world"


def test_has_replacements_false_initially():
    ctx = RestorationContext()
    assert ctx.has_replacements() is False


def test_has_replacements_true_after_register():
    ctx = RestorationContext()
    ctx.register("john@example.com", "EMAIL")
    assert ctx.has_replacements() is True


def test_build_system_instruction_is_non_empty_string():
    ctx = RestorationContext()
    instruction = ctx.build_system_instruction()
    assert isinstance(instruction, str)
    assert len(instruction) > 0


def test_build_system_instruction_describes_pattern():
    ctx = RestorationContext()
    instruction = ctx.build_system_instruction()
    # must describe the pattern without using a real-looking key as example
    assert "HEXCHARS" in instruction or "TYPENAME" in instruction or "placeholder" in instruction.lower()


# ---------------------------------------------------------------------------
# StreamingRestorer tests
# ---------------------------------------------------------------------------

def _restorer_with(original: str, entity_type: str = "EMAIL_ADDRESS") -> tuple["StreamingRestorer", str]:
    ctx = RestorationContext()
    placeholder = ctx.register(original, entity_type)
    return StreamingRestorer(ctx), placeholder


def test_streaming_restorer_passthrough_no_brackets():
    restorer, _ = _restorer_with("john@example.com")
    assert restorer.feed("hello world") == "hello world"
    assert restorer.finalize() == ""


def test_streaming_restorer_complete_placeholder_in_one_chunk():
    restorer, placeholder = _restorer_with("john@example.com")
    result = restorer.feed(f"email: {placeholder}!")
    assert result == "email: john@example.com!"


def test_streaming_restorer_holds_partial_then_restores():
    restorer, placeholder = _restorer_with("john@example.com")
    mid = len(placeholder) // 2
    out1 = restorer.feed("email: " + placeholder[:mid])
    out2 = restorer.feed(placeholder[mid:] + " done")
    assert out1 == "email: "           # held back partial placeholder
    assert out2 == "john@example.com done"


def test_streaming_restorer_char_by_char():
    restorer, placeholder = _restorer_with("john@example.com")
    result = ""
    for ch in f"see {placeholder} here":
        result += restorer.feed(ch)
    result += restorer.finalize()
    assert result == "see john@example.com here"


def test_streaming_restorer_non_matching_bracket_flushed_immediately():
    restorer, _ = _restorer_with("john@example.com")
    assert restorer.feed("[not a placeholder]") == "[not a placeholder]"


def test_streaming_restorer_multiple_placeholders_in_stream():
    ctx = RestorationContext()
    p1 = ctx.register("john@example.com", "EMAIL_ADDRESS")
    p2 = ctx.register("+1234567890", "PHONE_NUMBER")
    restorer = StreamingRestorer(ctx)
    text = f"Email {p1} phone {p2} end"
    result = restorer.feed(text) + restorer.finalize()
    assert result == "Email john@example.com phone +1234567890 end"


def test_streaming_restorer_finalize_flushes_incomplete_buffer():
    restorer, placeholder = _restorer_with("john@example.com")
    restorer.feed(placeholder[:5])   # partial "[EMAI"
    tail = restorer.finalize()
    assert tail == placeholder[:5]   # flushed as-is, no mangling


def test_streaming_restorer_empty_context_is_noop():
    ctx = RestorationContext()       # no registrations
    restorer = StreamingRestorer(ctx)
    assert restorer.feed("any text [whatever]") == "any text [whatever]"
    assert restorer.finalize() == ""
