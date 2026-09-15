"""OAuth device-code support for cloud engine credentials.

Engines today carry one static bearer secret. Cloud engines may instead
hold an OAuth refresh token (device-code grant) whose access token
expires and must be refreshed before each upstream request.

Token lifecycle: the refresh token is stored encrypted, exactly like a
static secret, by Store. Access tokens are held in memory only, and are
never written to disk. An expired access token is re-issued on demand
via the provider's token endpoint.

Providers are scoped by their public OAuth identifiers only; no
deployment-specific values belong here.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from enum import Enum

import httpx

XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_ISSUER = "https://auth.x.ai"
XAI_DISCOVERY_URL = f"{XAI_ISSUER}/.well-known/openid-configuration"
XAI_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_REFRESH_SKEW = 3600  # access tokens last ~6h; refresh an hour early

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_ISSUER = "https://auth.openai.com"
CODEX_TOKEN_URL = f"{CODEX_ISSUER}/oauth/token"
CODEX_SCOPE = "openid email profile offline_access model.request"
CODEX_REFRESH_SKEW = 120


class Provider(str, Enum):
    XAI = "xai_oauth"
    CODEX = "codex_oauth"


@dataclass(frozen=True)
class Spec:
    client_id: str
    token_url: str
    scope: str
    refresh_skew: int


SPECS: dict[str, Spec] = {
    Provider.XAI.value: Spec(XAI_CLIENT_ID, XAI_DISCOVERY_URL, XAI_SCOPE, XAI_REFRESH_SKEW),
    Provider.CODEX.value: Spec(CODEX_CLIENT_ID, CODEX_TOKEN_URL, CODEX_SCOPE, CODEX_REFRESH_SKEW),
}


def _jwt_exp(token: str) -> float | None:
    """Return the exp claim of an opaque JWT access token, if parseable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except (IndexError, ValueError, TypeError, AttributeError):
        # Opaque or malformed tokens are not our access tokens; treat as unknown.
        return None


class OAuthSession:
    """Caches one access token per provider; refreshes on expiry.

    The token endpoint for xAI is discovered from its OIDC metadata once
    per process; Codex uses a fixed token URL. Refresh tokens are never
    cached here -- callers hold them in the encrypted secret store.
    """

    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}
        self._token_urls: dict[str, str] = {}

    async def _resolve_token_url(self, provider: str, http: httpx.AsyncClient) -> str:
        if provider == "xai_oauth":
            if provider not in self._token_urls:
                response = await http.get(
                    XAI_DISCOVERY_URL, headers={"Accept": "application/json"}
                )
                response.raise_for_status()
                url = response.json().get("token_endpoint", "")
                if not url:
                    raise RuntimeError("xai_oauth: discovery returned no token endpoint")
                self._token_urls[provider] = url
            return self._token_urls[provider]
        return SPECS[provider].token_url

    async def access_token(
        self, provider: str, refresh_token: str, http: httpx.AsyncClient
    ) -> tuple[str, str | None]:
        """Return (access_token, rotated_refresh_token|None).

        Providers rotate the refresh token on every use; the caller must
        persist the rotated value when one is returned, or the next
        refresh will be rejected as reused.
        """
        cached, expires_at = self._tokens.get((provider, refresh_token), ("", 0.0))
        if cached and time.time() < expires_at:
            return cached, None
        url = await self._resolve_token_url(provider, http)
        response = await http.post(
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "client_id": SPECS[provider].client_id,
                "refresh_token": refresh_token,
            },
        )
        response.raise_for_status()
        token = response.json()
        access = token.get("access_token", "") or ""
        if not access:
            raise RuntimeError(f"{provider}: token refresh returned no access token")
        expires_in = token.get("expires_in", 3600)
        self._tokens[(provider, refresh_token)] = (
            access,
            time.time() + float(expires_in) - SPECS[provider].refresh_skew,
        )
        return access, token.get("refresh_token") or None
