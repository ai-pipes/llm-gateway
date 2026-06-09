import pytest
from gateway.domain.exceptions import (
    GatewayError,
    SanitizerBlockedError,
    AdapterNotFoundError,
    UpstreamTimeoutError,
    UpstreamError,
)


def test_sanitizer_blocked_error_has_reason():
    e = SanitizerBlockedError("contains PII")
    assert e.reason == "contains PII"
    assert isinstance(e, GatewayError)


def test_adapter_not_found_error_has_name():
    e = AdapterNotFoundError("my-adapter")
    assert e.name == "my-adapter"
    assert isinstance(e, GatewayError)


def test_adapter_not_found_with_none():
    e = AdapterNotFoundError(None)
    assert e.name is None


def test_upstream_errors_are_gateway_errors():
    assert isinstance(UpstreamTimeoutError(), GatewayError)
    assert isinstance(UpstreamError("fail"), GatewayError)
