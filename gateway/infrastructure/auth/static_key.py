import hashlib
from starlette.requests import Request
from gateway.domain.models import AuthContext
from gateway.infrastructure.auth.base import BaseAuthProvider


class StaticKeyAuthProvider(BaseAuthProvider):
    """Reference implementation: validates keys from a static dict.
    For quick-start and tests only — not for production."""

    def __init__(self, keys: dict):
        # keys: {"sk-xxx": {"user_id": "...", "team_id": "..."}}
        self._keys = keys

    async def authenticate(self, request: Request) -> AuthContext | None:
        api_key = request.headers.get("x-api-key", "")
        entry = self._keys.get(api_key)
        if not entry:
            return None
        return AuthContext(
            key_id=hashlib.sha256(api_key.encode()).hexdigest()[:16],
            user_id=entry.get("user_id"),
            team_id=entry.get("team_id"),
        )
