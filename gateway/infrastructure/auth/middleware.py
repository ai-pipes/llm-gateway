from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from gateway.infrastructure.auth.base import BaseAuthProvider


class AuthMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that delegates authentication to any BaseAuthProvider.

    On success: attaches AuthContext to request.state.auth and calls next middleware.
    On failure: returns 401 immediately — no audit record is written.
    """

    def __init__(self, app, provider: BaseAuthProvider):
        super().__init__(app)
        self._provider = provider

    async def dispatch(self, request: Request, call_next):
        ctx = await self._provider.authenticate(request)
        if ctx is None:
            return JSONResponse(
                {
                    "error": {
                        "type": "auth_error",
                        "message": "Invalid or missing API key",
                        "code": "unauthorized",
                    }
                },
                status_code=401,
            )
        request.state.auth = ctx
        return await call_next(request)
