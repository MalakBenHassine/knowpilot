#!/usr/bin/env bash
# Creates (or updates) the KnowPilot realm, its BFF client and a development
# user, using Keycloak's admin CLI inside the running container.
#
#   ./infra/keycloak/setup-realm.sh
#
# Idempotent: running it twice leaves the same configuration.
# Secrets are read from .env and never written to the repository.
set -euo pipefail

cd "$(dirname "$0")/../.."

if [[ ! -f .env ]]; then
    echo "Missing .env - copy .env.example and fill it in." >&2
    exit 1
fi
# shellcheck disable=SC1091
set -a && source .env && set +a

REALM="knowpilot"
CLIENT_ID="knowpilot-bff"
KC="docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh"

echo "==> Authenticating against the master realm"
$KC config credentials \
    --server http://localhost:8080 \
    --realm master \
    --user "$KP_KEYCLOAK_ADMIN" \
    --password "$KP_KEYCLOAK_ADMIN_PASSWORD" >/dev/null

### Realm settings, applied whether the realm is new or not, so this script
### stays the single source of truth for the configuration.
REALM_ARGS=(
    -s enabled=true
    -s displayName="KnowPilot"
    # Self-service sign-up. Keycloak provides the whole registration flow:
    # form, password policy, duplicate checks. We write no code for it.
    -s registrationAllowed=true
    -s registrationEmailAsUsername=true
    # TODO before the public deployment: set verifyEmail=true once SMTP is
    # configured, and add per-user quotas. Without them, anyone could consume
    # the free LLM quota and the disk.
    -s verifyEmail=false
    -s resetPasswordAllowed=true
    -s loginWithEmailAllowed=true
    # Temporary lockout after repeated failures: hashing only slows an
    # attacker down offline, not against the live login form.
    -s bruteForceProtected=true
    -s "passwordPolicy=length(12) and notUsername(undefined) and passwordHistory(3)"
    -s ssoSessionIdleTimeout=1800
    -s ssoSessionMaxLifespan=28800
)

echo "==> Realm '$REALM'"
if $KC get "realms/$REALM" >/dev/null 2>&1; then
    $KC update "realms/$REALM" "${REALM_ARGS[@]}" >/dev/null
    echo "    updated"
else
    $KC create realms -s "realm=$REALM" "${REALM_ARGS[@]}" >/dev/null
    echo "    created"
fi

echo "==> Client '$CLIENT_ID' (confidential, BFF)"
CLIENT_UUID=$($KC get clients -r "$REALM" -q "clientId=$CLIENT_ID" --fields id --format csv --noquotes | tr -d '\r')

CLIENT_ARGS=(
    -s "clientId=$CLIENT_ID"
    -s enabled=true
    -s protocol=openid-connect
    # Confidential client: the code exchange happens server to server.
    -s publicClient=false
    -s standardFlowEnabled=true
    # Password grant and implicit flow are deprecated and must stay off.
    -s directAccessGrantsEnabled=false
    -s implicitFlowEnabled=false
    -s serviceAccountsEnabled=false
    # Exact callback URL only: a wildcard here is a known attack vector.
    -s 'redirectUris=["http://localhost:5173/api/auth/callback"]'
    # No web origin: the browser never calls Keycloak with JavaScript, only the
    # backend does, server to server.
    -s 'webOrigins=[]'
    -s 'attributes={"pkce.code.challenge.method":"S256","post.logout.redirect.uris":"http://localhost:5173/login"}'
)

if [[ -z "$CLIENT_UUID" ]]; then
    $KC create clients -r "$REALM" "${CLIENT_ARGS[@]}" >/dev/null
    CLIENT_UUID=$($KC get clients -r "$REALM" -q "clientId=$CLIENT_ID" --fields id --format csv --noquotes | tr -d '\r')
    echo "    created"
else
    $KC update "clients/$CLIENT_UUID" -r "$REALM" "${CLIENT_ARGS[@]}" >/dev/null
    echo "    updated"
fi

echo "==> Client secret"
# Only READ the secret. Regenerating it on every run would silently break the
# value already stored in .env - idempotence matters for setup scripts.
SECRET=$($KC get "clients/$CLIENT_UUID/client-secret" -r "$REALM" --fields value --format csv --noquotes | tr -d '\r')
if [[ -z "$SECRET" ]]; then
    $KC create "clients/$CLIENT_UUID/client-secret" -r "$REALM" >/dev/null
    SECRET=$($KC get "clients/$CLIENT_UUID/client-secret" -r "$REALM" --fields value --format csv --noquotes | tr -d '\r')
fi

echo "==> Development user"
if [[ -n "${KP_DEV_USER_PASSWORD:-}" ]]; then
    USER_ID=$($KC get users -r "$REALM" -q "username=malak" --fields id --format csv --noquotes | tr -d '\r')
    if [[ -z "$USER_ID" ]]; then
        $KC create users -r "$REALM" \
            -s username=malak \
            -s enabled=true \
            -s emailVerified=true \
            -s email=malak@knowpilot.dev \
            -s firstName=Malak \
            -s lastName="Ben Hassine" >/dev/null
        USER_ID=$($KC get users -r "$REALM" -q "username=malak" --fields id --format csv --noquotes | tr -d '\r')
    fi
    # The realm forbids reusing the last three passwords, so re-running the
    # script with an unchanged value fails here. That is the policy working,
    # not a bug: keep going.
    if $KC set-password -r "$REALM" --userid "$USER_ID" --new-password "$KP_DEV_USER_PASSWORD" >/dev/null 2>&1; then
        echo "    user 'malak' ready"
    else
        echo "    user 'malak' exists; password unchanged (password history policy)"
    fi
else
    echo "    skipped (KP_DEV_USER_PASSWORD not set)"
fi

echo
echo "Done. Add this to your .env (it is git-ignored):"
echo
echo "KP_OIDC_CLIENT_SECRET=$SECRET"
