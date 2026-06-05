import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class AuthContext:
    key_id: str       # хэш ключа — идёт в аудит, не сам ключ
    user_id: str | None
    team_id: str | None


class BaseAuthProvider(ABC):
    @abstractmethod
    async def authenticate(self, request) -> AuthContext | None:
        """
        Принять Starlette Request, вернуть AuthContext если аутентифицирован.
        Вернуть None чтобы gateway ответил 401.
        """
        ...


class StaticKeyAuthProvider(BaseAuthProvider):
    """
    Reference implementation: валидирует ключи из статического словаря.
    Только для быстрого старта и тестов — не для production.
    """

    def __init__(self, keys: dict):
        # keys: {"sk-xxx": {"user_id": "...", "team_id": "..."}}
        self._keys = keys

    async def authenticate(self, request) -> AuthContext | None:
        api_key = request.headers.get("x-api-key", "")
        entry = self._keys.get(api_key)
        if not entry:
            return None
        return AuthContext(
            key_id=hashlib.sha256(api_key.encode()).hexdigest()[:16],
            user_id=entry.get("user_id"),
            team_id=entry.get("team_id"),
        )
