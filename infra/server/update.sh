#!/usr/bin/env bash
# Deploy the latest release, if there is one, and only if it proves what it is.
#
# The server pulls. Nothing in GitHub holds a credential that opens this
# machine, because nothing in GitHub ever connects to it: this script asks the
# public Releases API what the newest version is, and decides for itself.
# A compromised workflow cannot deploy here - it can only publish an image,
# and an image it cannot sign is one this script refuses.
#
# Run it from a systemd timer (infra/server/knowpilot-update.timer), or by
# hand. It is idempotent: with no new release it does nothing at all.
#
#   ./infra/server/update.sh              # deploy the newest release
#   KP_DRY_RUN=1 ./infra/server/update.sh # say what it would do, change nothing
#
# Exit codes: 0 nothing to do or deployed; 1 refused or rolled back.
set -euo pipefail

REPOSITORY="${KP_GITHUB_REPOSITORY:-MalakBenHassine/knowpilot}"
PROJECT_DIR="${KP_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ENV_FILE="${KP_ENV_FILE:-${PROJECT_DIR}/.env.production}"
COMPOSE_FILE="${KP_COMPOSE_FILE:-${PROJECT_DIR}/docker-compose.prod.yml}"
ISSUER="https://token.actions.githubusercontent.com"
IMAGES=(backend frontend keycloak)

# A deployment that has not become healthy in five minutes is not slow, it is
# broken: the images are already pulled, so this only covers starting.
HEALTH_TIMEOUT="${KP_HEALTH_TIMEOUT:-300}"

say() { printf '==> %s\n' "$1"; }
fail() { printf 'REFUSED: %s\n' "$1" >&2; exit 1; }

# --- one at a time ----------------------------------------------------------
# A timer that fires while the previous run is still pulling 2.5 GB would
# deploy two versions at once. flock re-executes this script holding the lock.
LOCK="/tmp/knowpilot-update.lock"
if [[ "${KP_LOCKED:-}" != "1" ]]; then
    export KP_LOCKED=1
    exec flock -n "${LOCK}" "$0" "$@" || {
        echo "another update is already running"
        exit 0
    }
fi

command -v docker >/dev/null || fail "docker is not installed"
command -v cosign >/dev/null || fail "cosign is not installed (3.0 or newer)"
[[ -f "${ENV_FILE}" ]] || fail "no ${ENV_FILE}"
[[ -f "${COMPOSE_FILE}" ]] || fail "no ${COMPOSE_FILE}"

read_env() { sed -n "s/^$1=//p" "${ENV_FILE}" | tail -1; }

REGISTRY="$(read_env KP_IMAGE_REGISTRY)"
CURRENT="$(read_env KP_IMAGE_TAG)"
[[ -n "${REGISTRY}" ]] || fail "KP_IMAGE_REGISTRY is not set in ${ENV_FILE}"

# --- what is the newest release? --------------------------------------------
say "Asking ${REPOSITORY} for its latest release"
# A server with no route out, a rate limit, a repository that moved: all
# three arrive here as a curl exit code, and `set -e` would end the run on
# a bare number. A timer that fires every five minutes deserves a sentence.
if ! response=$(curl -fsSL --max-time 30 \
    -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/${REPOSITORY}/releases/latest" 2>/dev/null); then
    fail "cannot reach the GitHub API for ${REPOSITORY} - offline, rate-limited, or no such repository"
fi
LATEST=$(printf '%s' "${response}" \
    | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)

# The tag names the images and the signing identity. Anything other than a
# version is either an API error or something a reader would not expect to be
# deployed, and both deserve a stop rather than a guess.
[[ "${LATEST}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "unexpected tag from the API: '${LATEST}'"

echo "    deployed: ${CURRENT:-<none>}"
echo "    latest:   ${LATEST}"

if [[ "${CURRENT}" == "${LATEST}" ]]; then
    say "Already on ${LATEST}; nothing to do"
    exit 0
fi

# --- the signature decides ---------------------------------------------------
# Verified HERE, on the machine that will run the code - not only in the
# workflow that built it. This is where a swapped image in the registry is
# caught, and the only place where catching it still prevents anything.
IDENTITY="https://github.com/${REPOSITORY}/.github/workflows/release.yml@refs/tags/${LATEST}"
say "Verifying the signature of each image for ${LATEST}"
for image in "${IMAGES[@]}"; do
    reference="${REGISTRY}/knowpilot-${image}:${LATEST}"
    if cosign verify "${reference}" \
        --certificate-identity "${IDENTITY}" \
        --certificate-oidc-issuer "${ISSUER}" >/dev/null 2>&1; then
        echo "    ok  ${reference}"
    else
        fail "${reference} is not signed by ${IDENTITY} - NOT deploying"
    fi
done

if [[ -n "${KP_DRY_RUN:-}" ]]; then
    say "Dry run: would deploy ${LATEST}. Nothing changed."
    exit 0
fi

# --- deploy ------------------------------------------------------------------
compose() { docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" "$@"; }

write_tag() {
    if grep -q '^KP_IMAGE_TAG=' "${ENV_FILE}"; then
        sed -i "s|^KP_IMAGE_TAG=.*|KP_IMAGE_TAG=$1|" "${ENV_FILE}"
    else
        printf 'KP_IMAGE_TAG=%s\n' "$1" >> "${ENV_FILE}"
    fi
}

say "Deploying ${LATEST}"
write_tag "${LATEST}"
compose pull --quiet
compose up -d --remove-orphans

# --- did it come up? ---------------------------------------------------------
# Every container that declares a healthcheck must reach `healthy`. A container
# that only says `running` says nothing: a process can be up and refusing every
# request, which is the failure this whole script exists to catch.
unhealthy() {
    compose ps --format '{{.Service}} {{.Health}} {{.State}}' 2>/dev/null \
      | awk '$3 == "running" && $2 != "" && $2 != "healthy" { print $1 }'
}

say "Waiting up to ${HEALTH_TIMEOUT}s for the containers to become healthy"
deadline=$(( SECONDS + HEALTH_TIMEOUT ))
while (( SECONDS < deadline )); do
    pending="$(unhealthy)"
    [[ -z "${pending}" ]] && break
    sleep 5
done

pending="$(unhealthy)"
if [[ -z "${pending}" ]]; then
    say "Deployed ${LATEST}"
    exit 0
fi

# --- roll back ---------------------------------------------------------------
echo "    still not healthy: ${pending//$'\n'/ }" >&2
if [[ -z "${CURRENT}" ]]; then
    echo "REFUSED: no previous tag to roll back to; the stack is left as it is" >&2
    exit 1
fi

say "Rolling back to ${CURRENT}"
write_tag "${CURRENT}"
compose up -d --remove-orphans
echo "Rolled back to ${CURRENT}. ${LATEST} did not become healthy." >&2
exit 1
