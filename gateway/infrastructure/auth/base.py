from abc import ABC, abstractmethod
from starlette.requests import Request
from gateway.domain.models import AuthContext


class BaseAuthProvider(ABC):
    @abstractmethod
    async def authenticate(self, request: Request) -> AuthContext | None:
        """Return AuthContext to allow the request. Return None to reject with 401."""
        ...
