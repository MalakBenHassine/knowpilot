"""OpenID Connect client (Authorization Code + PKCE), server side only.

The browser is merely redirected to Keycloak; the code is exchanged here, so no
token ever reaches JavaScript (ADR-0004).
"""

import base64
import hashlib
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet

from app.core.config import Settings

# Discovery and keys change rarely; refetching them on every login would add a
# round trip and a failure mode for nothing.
_CACHE_TTL_SECONDS = 3600


class OidcError(Exception):
    """Anything that makes the login untrustworthy. Details go to the logs."""


def pkce_challenge(verifier: str) -> str:
    """S256 challenge: only the holder of the verifier can redeem the code."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class OidcClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._metadata: dict[str, Any] | None = None
        self._metadata_fetched_at = 0.0
        self._jwks: Any = None
        self._jwks_fetched_at = 0.0

    async def metadata(self) -> dict[str, Any]:
        if self._metadata and time.time() - self._metadata_fetched_at < _CACHE_TTL_SECONDS:
            return self._metadata
        url = f"{self._settings.oidc_issuer}/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url)
        if response.status_code != 200:
            raise OidcError("discovery document unavailable")
        self._metadata = response.json()
        self._metadata_fetched_at = time.time()
        return self._metadata

    async def authorization_url(
        self, state: str, nonce: str, code_challenge: str, *, register: bool = False
    ) -> str:
        """Builds the login URL, or the sign-up one.

        Registration is the SAME OIDC flow: Keycloak simply opens its
        registration form first, then returns an authorization code exactly as
        a login would. Nothing else changes, here or in the callback.
        """
        metadata = await self.metadata()
        query = urlencode(
            {
                "client_id": self._settings.oidc_client_id,
                "response_type": "code",
                "scope": "openid email profile",
                "redirect_uri": f"{self._settings.frontend_url}/api/auth/callback",
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        endpoint: str = metadata["authorization_endpoint"]
        if register:
            # Keycloak exposes the registration form at .../registrations,
            # which is the authorization endpoint with a different last segment.
            endpoint = endpoint.rsplit("/", 1)[0] + "/registrations"
        return f"{endpoint}?{query}"

    async def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        """Swaps the code for tokens, server to server, with the client secret."""
        metadata = await self.metadata()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                metadata["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": f"{self._settings.frontend_url}/api/auth/callback",
                    "code_verifier": code_verifier,
                },
                auth=(self._settings.oidc_client_id, self._settings.oidc_client_secret),
            )
        if response.status_code != 200:
            raise OidcError("token exchange refused")
        tokens: dict[str, Any] = response.json()
        return tokens

    async def _keys(self) -> Any:
        if self._jwks is not None and time.time() - self._jwks_fetched_at < _CACHE_TTL_SECONDS:
            return self._jwks
        metadata = await self.metadata()
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(metadata["jwks_uri"])
        if response.status_code != 200:
            raise OidcError("jwks unavailable")
        self._jwks = KeySet.import_key_set(response.json())
        self._jwks_fetched_at = time.time()
        return self._jwks

    async def verify_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        """Validates signature, issuer, audience, expiry and nonce.

        Decoding without verifying would let anyone forge an identity: the
        signature is the whole point of the token.
        """
        keys = await self._keys()
        # Only asymmetric algorithms: "none" and HMAC would let a forged token
        # through if the key material ever leaked.
        registry = jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": self._settings.oidc_issuer},
            aud={"essential": True, "value": self._settings.oidc_client_id},
            exp={"essential": True},
            # Binds this token to the login WE started: blocks replay.
            nonce={"essential": True, "value": nonce},
        )
        try:
            token = jwt.decode(id_token, keys, algorithms=["RS256", "ES256"])
            registry.validate(token.claims)
        except Exception as error:  # noqa: BLE001 - one generic failure to the caller
            raise OidcError("invalid id token") from error
        return dict(token.claims)

    async def end_session_url(self, id_token: str) -> str:
        """RP-initiated logout: ends the Keycloak session too, not just ours."""
        metadata = await self.metadata()
        query = urlencode(
            {
                "id_token_hint": id_token,
                "post_logout_redirect_uri": f"{self._settings.frontend_url}/login",
                "client_id": self._settings.oidc_client_id,
            }
        )
        return f"{metadata['end_session_endpoint']}?{query}"
