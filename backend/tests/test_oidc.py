"""OIDC URL building.

No network here: the discovery document is stubbed. What is tested is the URL
we send the browser to, because a mistake there is a security bug (wrong
redirect target, missing PKCE, weak challenge method).
"""

from urllib.parse import parse_qs, urlparse

import anyio

from app.core.config import Settings
from app.core.oidc import OidcClient, pkce_challenge

METADATA = {
    "authorization_endpoint": "http://kc/realms/knowpilot/protocol/openid-connect/auth",
    "token_endpoint": "http://kc/realms/knowpilot/protocol/openid-connect/token",
    "jwks_uri": "http://kc/realms/knowpilot/protocol/openid-connect/certs",
    "end_session_endpoint": "http://kc/realms/knowpilot/protocol/openid-connect/logout",
}


def build(register: bool) -> tuple[str, dict[str, list[str]]]:
    client = OidcClient(Settings())
    # Pre-seed the cache so no HTTP call is made.
    client._metadata = METADATA  # noqa: SLF001
    client._metadata_fetched_at = 1e12  # noqa: SLF001

    async def run() -> str:
        return await client.authorization_url(
            "state123", "nonce123", "challenge", register=register
        )

    url = anyio.run(run)
    parsed = urlparse(url)
    return parsed.path, parse_qs(parsed.query)


def test_login_url_uses_the_authorization_endpoint_with_pkce() -> None:
    path, query = build(register=False)

    assert path.endswith("/auth")
    assert query["response_type"] == ["code"]
    # S256 only: the "plain" method offers no protection.
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == ["challenge"]
    assert query["state"] == ["state123"]
    assert query["nonce"] == ["nonce123"]
    # The callback must be the exact URL registered in Keycloak.
    assert query["redirect_uri"] == ["http://localhost:5173/api/auth/callback"]


def test_register_url_only_changes_the_first_page() -> None:
    login_path, login_query = build(register=False)
    register_path, register_query = build(register=True)

    assert register_path.endswith("/registrations")
    assert login_path != register_path
    # Sign-up is the same OIDC flow: every security parameter is identical.
    assert login_query == register_query


def test_pkce_challenge_is_a_sha256_digest() -> None:
    challenge = pkce_challenge("a-very-long-random-verifier-value")
    # Base64url of a SHA-256 digest: 43 characters, no padding.
    assert len(challenge) == 43
    assert "=" not in challenge
    assert challenge != "a-very-long-random-verifier-value"
