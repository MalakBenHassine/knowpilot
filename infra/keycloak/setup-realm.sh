#!/usr/bin/env bash
# Creates (or updates) the KnowPilot realm and its BFF client, using Keycloak's
# admin CLI inside the running container. One script for both environments:
#
#   ./infra/keycloak/setup-realm.sh                                  development
#   KP_ENV_FILE=.env.production KP_COMPOSE_FILE=docker-compose.prod.yml \
#       ./infra/keycloak/setup-realm.sh                              production
#
# Idempotent: running it twice leaves the same configuration.
# Secrets are read from the env file and never written to the repository.
set -euo pipefail

cd "$(dirname "$0")/../.."

ENV_FILE="${KP_ENV_FILE:-.env}"
COMPOSE_FILE="${KP_COMPOSE_FILE:-docker-compose.yml}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Missing $ENV_FILE - copy the matching .example file and fill it in." >&2
    exit 1
fi
# shellcheck disable=SC1090
set -a && source "$ENV_FILE" && set +a

REALM="knowpilot"
CLIENT_ID="knowpilot-bff"
KC="docker compose -f $COMPOSE_FILE --env-file $ENV_FILE exec -T keycloak /opt/keycloak/bin/kcadm.sh"

# Where the browser reaches the product. Production derives it from the
# domain; development keeps the Vite dev server.
if [[ -n "${KP_DOMAIN:-}" ]]; then
    PUBLIC_URL="https://$KP_DOMAIN"
    # Production serves Keycloak under /auth (infra/keycloak/Dockerfile).
    ADMIN_URL="http://localhost:8080/auth"
else
    PUBLIC_URL="${KP_PUBLIC_URL:-http://localhost:5173}"
    ADMIN_URL="http://localhost:8080"
fi

# Self-service sign-up. On by default for development; OFF in production
# (.env.production.example) until email verification exists: an open
# registration without it lets anyone create accounts in a loop and spend the
# service's daily question budget - the per-user quota does not stop a
# thousand users.
REGISTRATION="${KP_KEYCLOAK_REGISTRATION:-true}"

echo "==> Authenticating against the master realm ($ADMIN_URL, inside the container)"
$KC config credentials \
    --server "$ADMIN_URL" \
    --realm master \
    --user "$KP_KEYCLOAK_ADMIN" \
    --password "$KP_KEYCLOAK_ADMIN_PASSWORD" >/dev/null

### Realm settings, applied whether the realm is new or not, so this script
### stays the single source of truth for the configuration.
REALM_ARGS=(
    -s enabled=true
    -s displayName="KnowPilot"
    # Keycloak provides the whole registration flow: form, password policy,
    # duplicate checks. We write no code for it.
    -s "registrationAllowed=$REGISTRATION"
    -s registrationEmailAsUsername=true
    # Needs SMTP, which is not configured yet (docs/deploy.md).
    -s verifyEmail=false
    -s resetPasswordAllowed=false
    -s loginWithEmailAllowed=true
    # Temporary lockout after repeated failures: hashing only slows an
    # attacker down offline, not against the live login form.
    -s bruteForceProtected=true
    -s "passwordPolicy=length(12) and notUsername(undefined) and passwordHistory(3)"
    -s ssoSessionIdleTimeout=1800
    -s ssoSessionMaxLifespan=28800
)

echo "==> Realm '$REALM' (registration: $REGISTRATION)"
if $KC get "realms/$REALM" >/dev/null 2>&1; then
    $KC update "realms/$REALM" "${REALM_ARGS[@]}" >/dev/null
    echo "    updated"
else
    $KC create realms -s "realm=$REALM" "${REALM_ARGS[@]}" >/dev/null
    echo "    created"
fi

echo "==> Client '$CLIENT_ID' (confidential, BFF) for $PUBLIC_URL"
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
    -s "redirectUris=[\"$PUBLIC_URL/api/auth/callback\"]"
    # No web origin: the browser never calls Keycloak with JavaScript, only the
    # backend does, server to server.
    -s 'webOrigins=[]'
    -s "attributes={\"pkce.code.challenge.method\":\"S256\",\"post.logout.redirect.uris\":\"$PUBLIC_URL/login\"}"
)

# The secret: SET from the env file when it is there (production, where it is
# generated with the other secrets before anything starts), otherwise read or
# created and printed (development, as before).
if [[ -n "${KP_OIDC_CLIENT_SECRET:-}" ]]; then
    CLIENT_ARGS+=(-s "secret=$KP_OIDC_CLIENT_SECRET")
fi

if [[ -z "$CLIENT_UUID" ]]; then
    $KC create clients -r "$REALM" "${CLIENT_ARGS[@]}" >/dev/null
    CLIENT_UUID=$($KC get clients -r "$REALM" -q "clientId=$CLIENT_ID" --fields id --format csv --noquotes | tr -d '\r')
    echo "    created"
else
    $KC update "clients/$CLIENT_UUID" -r "$REALM" "${CLIENT_ARGS[@]}" >/dev/null
    echo "    updated"
fi

if [[ -n "${KP_OIDC_CLIENT_SECRET:-}" ]]; then
    echo "==> Client secret: set from $ENV_FILE"
else
    echo "==> Client secret"
    # Only READ the secret. Regenerating it on every run would silently break
    # the value already stored in .env - idempotence matters for setup scripts.
    SECRET=$($KC get "clients/$CLIENT_UUID/client-secret" -r "$REALM" --fields value --format csv --noquotes | tr -d '\r')
    if [[ -z "$SECRET" ]]; then
        $KC create "clients/$CLIENT_UUID/client-secret" -r "$REALM" >/dev/null
        SECRET=$($KC get "clients/$CLIENT_UUID/client-secret" -r "$REALM" --fields value --format csv --noquotes | tr -d '\r')
    fi
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
if [[ -z "${KP_OIDC_CLIENT_SECRET:-}" ]]; then
    echo "Done. Add this to $ENV_FILE (it is git-ignored):"
    echo
    echo "KP_OIDC_CLIENT_SECRET=$SECRET"
else
    echo "Done."
fi
