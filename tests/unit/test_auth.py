import pytest
from unittest.mock import MagicMock
from gateway.domain.models import AuthContext
from gateway.infrastructure.auth.base import BaseAuthProvider
from gateway.infrastructure.auth.static_key import StaticKeyAuthProvider


def test_auth_context_fields():
    ctx = AuthContext(key_id="abc123", user_id="user-1", team_id="team-a")
    assert ctx.key_id == "abc123"
    assert ctx.user_id == "user-1"
    assert ctx.team_id == "team-a"


def test_base_auth_provider_is_abstract():
    with pytest.raises(TypeError):
        BaseAuthProvider()


async def test_static_key_valid_key_returns_context():
    provider = StaticKeyAuthProvider({
        "sk-valid": {"user_id": "alice", "team_id": "engineering"},
    })
    request = MagicMock()
    request.headers = {"x-api-key": "sk-valid"}

    ctx = await provider.authenticate(request)
    assert ctx is not None
    assert ctx.user_id == "alice"
    assert ctx.team_id == "engineering"
    assert len(ctx.key_id) > 0
    assert "sk-valid" not in ctx.key_id  # key_id — хэш, не сам ключ


async def test_static_key_invalid_key_returns_none():
    provider = StaticKeyAuthProvider({"sk-valid": {"user_id": "alice", "team_id": "eng"}})
    request = MagicMock()
    request.headers = {"x-api-key": "sk-wrong"}

    ctx = await provider.authenticate(request)
    assert ctx is None


async def test_static_key_missing_header_returns_none():
    provider = StaticKeyAuthProvider({"sk-valid": {"user_id": "alice", "team_id": "eng"}})
    request = MagicMock()
    request.headers = {}

    ctx = await provider.authenticate(request)
    assert ctx is None


async def test_static_key_different_keys_get_different_ids():
    provider = StaticKeyAuthProvider({
        "sk-one": {"user_id": "a", "team_id": "t"},
        "sk-two": {"user_id": "b", "team_id": "t"},
    })
    request_one = MagicMock()
    request_one.headers = {"x-api-key": "sk-one"}
    request_two = MagicMock()
    request_two.headers = {"x-api-key": "sk-two"}

    ctx_one = await provider.authenticate(request_one)
    ctx_two = await provider.authenticate(request_two)
    assert ctx_one.key_id != ctx_two.key_id
