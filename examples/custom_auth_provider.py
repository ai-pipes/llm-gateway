"""
Example custom auth provider — JWT-based authentication.

Steps to use:
  1. Copy this file to your project
  2. Implement the authenticate() method with your JWT library
  3. Configure in gateway.yaml:
       auth:
         module: "my_auth.jwt_provider.JWTAuthProvider"
         config:
           jwks_url: "https://your-idp.internal/.well-known/jwks.json"
"""
import hashlib
from starlette.requests import Request
from gateway.infrastructure.auth.base import BaseAuthProvider
from gateway.domain.models import AuthContext


class JWTAuthProvider(BaseAuthProvider):
    """
    Replace this class with your own implementation.
    The only required method is authenticate().

    Return AuthContext to allow the request.
    Return None to reject with 401 (no audit record is written for rejected requests).
    """

    def __init__(self, jwks_url: str):
        self._jwks_url = jwks_url
        # Initialize your JWT library here, e.g.:
        # from some_jwt_lib import JWKSClient
        # self._client = JWKSClient(jwks_url)

    async def authenticate(self, request: Request) -> AuthContext | None:
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return None
        token = authorization.removeprefix("Bearer ")

        # Verify the JWT using your library, e.g.:
        # try:
        #     claims = self._client.verify(token)
        # except Exception:
        #     return None

        # For this example we just decode without verification (DO NOT do this in production):
        claims = _decode_unverified(token)
        if not claims:
            return None

        return AuthContext(
            # key_id identifies the token in audit records (never store the raw token)
            key_id=hashlib.sha256(token.encode()).hexdigest()[:16],
            user_id=claims.get("sub"),
            team_id=claims.get("team") or claims.get("org"),
        )


def _decode_unverified(token: str) -> dict | None:
    """Stub — replace with real JWT verification."""
    import base64, json
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)  # fix padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
