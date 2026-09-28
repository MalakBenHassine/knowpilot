#!/usr/bin/env bash
# Invites one person to a production instance where self-registration is off.
#
#   KP_ENV_FILE=.env.production KP_COMPOSE_FILE=docker-compose.prod.yml \
#       ./infra/keycloak/invite-user.sh someone@example.org
#
# And when they lose it - the ordinary case, not a mistake:
#
#       ./infra/keycloak/invite-user.sh --reset someone@example.org
#
# Creates the account with a random TEMPORARY password and the required action
# UPDATE_PASSWORD: Keycloak makes them choose their own at the first login, so
# the value printed below works exactly once. Send it through a channel other
# than the one carrying the link.
set -euo pipefail

cd "$(dirname "$0")/../.."

RESET=""
if [[ "${1:-}" == "--reset" ]]; then
    RESET=1
    shift
fi
EMAIL="${1:?usage: invite-user.sh [--reset] EMAIL}"
ENV_FILE="${KP_ENV_FILE:-.env.production}"
COMPOSE_FILE="${KP_COMPOSE_FILE:-docker-compose.prod.yml}"
# shellcheck disable=SC1090
set -a && source "$ENV_FILE" && set +a

REALM="knowpilot"
KC="docker compose -f $COMPOSE_FILE --env-file $ENV_FILE exec -T keycloak /opt/keycloak/bin/kcadm.sh"

$KC config credentials --server http://localhost:8080/auth --realm master \
    --user "$KP_KEYCLOAK_ADMIN" --password "$KP_KEYCLOAK_ADMIN_PASSWORD" >/dev/null

USER_ID=$($KC get users -r "$REALM" -q "email=$EMAIL" --fields id --format csv --noquotes | tr -d '\r')

if [[ -n "$USER_ID" && -z "$RESET" ]]; then
    # A refusal that does not say what to do instead is a wall, and losing a
    # one-time password is the ordinary case.
    echo "$EMAIL already has an account." >&2
    echo "To give them a new temporary password:" >&2
    echo "    $0 --reset $EMAIL" >&2
    exit 1
fi
if [[ -z "$USER_ID" && -n "$RESET" ]]; then
    echo "$EMAIL has no account to reset. Run it without --reset to create one." >&2
    exit 1
fi

# 18 random bytes, base64: satisfies the realm's 12-character policy.
TEMPORARY=$(openssl rand -base64 18)

if [[ -z "$USER_ID" ]]; then
    $KC create users -r "$REALM" \
        -s "username=$EMAIL" \
        -s "email=$EMAIL" \
        -s enabled=true \
        -s emailVerified=true \
        -s 'requiredActions=["UPDATE_PASSWORD"]' >/dev/null
    USER_ID=$($KC get users -r "$REALM" -q "email=$EMAIL" --fields id --format csv --noquotes | tr -d '\r')
    ACTION="Invited"
else
    # Put UPDATE_PASSWORD back: a reset that let them keep the temporary
    # password would turn a value meant to be used once into a real one.
    $KC update "users/$USER_ID" -r "$REALM" \
        -s 'requiredActions=["UPDATE_PASSWORD"]' >/dev/null
    ACTION="Reset the password for"
fi

$KC set-password -r "$REALM" --userid "$USER_ID" --new-password "$TEMPORARY" --temporary >/dev/null

echo "$ACTION $EMAIL. Temporary password (valid for the first login only):"
echo
echo "    $TEMPORARY"
