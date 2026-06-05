import pytest
from gateway.sanitizers.base import BaseSanitizer, SanitizerChain, SanitizeResult
from gateway.sanitizers.passthrough import PassthroughSanitizer


def test_sanitize_result_defaults():
    result = SanitizeResult(text="hello")
    assert result.actions == []
    assert result.blocked is False
    assert result.block_reason == ""


def test_base_sanitizer_is_abstract():
    with pytest.raises(TypeError):
        BaseSanitizer()


async def test_passthrough_sanitizer_returns_unchanged():
    s = PassthroughSanitizer()
    result = await s.sanitize("hello world")
    assert result.text == "hello world"
    assert result.actions == []
    assert result.blocked is False


async def test_chain_runs_sanitizers_in_order():
    class AppendSanitizer(BaseSanitizer):
        def __init__(self, suffix: str):
            self._suffix = suffix

        async def sanitize(self, text: str) -> SanitizeResult:
            return SanitizeResult(text=text + self._suffix, actions=[f"appended:{self._suffix}"])

    chain = SanitizerChain([AppendSanitizer("-A"), AppendSanitizer("-B")])
    result = await chain.run("hello")
    assert result.text == "hello-A-B"
    assert result.actions == ["appended:-A", "appended:-B"]


async def test_chain_stops_on_blocked():
    class BlockingSanitizer(BaseSanitizer):
        async def sanitize(self, text: str) -> SanitizeResult:
            return SanitizeResult(text=text, blocked=True, block_reason="test_block")

    class ShouldNotRunSanitizer(BaseSanitizer):
        async def sanitize(self, text: str) -> SanitizeResult:
            raise AssertionError("Should not be called after block")

    chain = SanitizerChain([BlockingSanitizer(), ShouldNotRunSanitizer()])
    result = await chain.run("hello")
    assert result.blocked is True
    assert result.block_reason == "test_block"


async def test_empty_chain_is_passthrough():
    chain = SanitizerChain([])
    result = await chain.run("sensitive data")
    assert result.text == "sensitive data"
    assert result.actions == []
    assert result.blocked is False
