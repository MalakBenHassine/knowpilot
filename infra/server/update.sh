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
# Resolve the symlink FIRST. systemd calls /usr/local/bin/knowpilot-update,
# which points into the checkout, so BASH_SOURCE is the LINK: its directory is
# /usr/local/bin and two levels up is /usr, where no compose file has ever
# lived. Found by running it, not by reading it: "REFUSED: no
# /usr/docker-compose.prod.yml".
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
PROJECT_DIR="${KP_PROJECT_DIR:-$(cd "$(dirname "${SELF}")/../.." && pwd)}"
ENV_FILE="${KP_ENV_FILE:-${PROJECT_DIR}/.env.production}"
COMPOSE_FILE="${KP_COMPOSE_FILE:-${PROJECT_DIR}/docker-compose.prod.yml}"
ISSUER="https://token.actions.githubusercontent.com"
IMAGES=(backend frontend keycloak)

# Must exceed the longest start_period in the compose file, plus a few probe
# intervals. Both the API and the worker declare 300 s, because each loads
# 2.2 GB of weights - measured at 134 s with the two competing for the same
# CPUs. Five minutes total, the old value, expired while a healthy stack was
# still legitimately starting, and the deployer rolled back a good release.
HEALTH_TIMEOUT="${KP_HEALTH_TIMEOUT:-900}"

say() { printf '==> %s\n' "$1"; }
fail() { printf 'REFUSED: %s\n' "$1" >&2; exit 1; }

# --- one at a time ----------------------------------------------------------
# A timer that fires while the previous run is still pulling 2.5 GB would
# deploy two versions at once. flock re-executes this script holding the lock.
LOCK="/tmp/knowpilot-update.lock"
if [[ "${KP_LOCKED:-}" != "1" ]]; then
    export KP_LOCKED=1
    # NOT `exec flock ... || ...`: exec REPLACES this shell, so when flock
    # cannot take the lock there is no shell left to run the fallback. The
    # process exits 1 with no output, and the caller sees a deployer that
    # refused for no stated reason. Found by Ansible's dry run colliding with
    # a real deployment already in progress.
    # -E 66 makes "lock is busy" distinguishable from the script's own failure.
    # `|| status=$?` and not a bare call: set -e would abort on flock's non-zero
    # exit before the code below could read it - which is how the first version
    # of this fix still exited 66 in silence.
    status=0
    flock -n -E 66 "${LOCK}" "$0" "$@" || status=$?
    if (( status == 66 )); then
        say "another update is already running; nothing to do"
        exit 0
    fi
    exit "${status}"
fi

command -v docker >/dev/null || fail "docker is not installed"
command -v cosign >/dev/null || fail "cosign is not installed (3.0 or newer)"
[[ -f "${ENV_FILE}" ]] || fail "no ${ENV_FILE}"
[[ -f "${COMPOSE_FILE}" ]] || fail "no ${COMPOSE_FILE}"

read_env() { sed -n "s/^$1=//p" "${ENV_FILE}" | tail -1; }

REGISTRY="$(read_env KP_IMAGE_REGISTRY)"

# KP_IMAGE_TAG in the env file is what compose SHOULD run - it has to be
# written before `compose pull`, because compose reads it to know what to pull.
# It is therefore NOT evidence that anything runs. This file is: it is written
# only after the containers have become healthy.
#
# Conflating the two wedged a real host. A pull was cut off after ninety
# minutes ("connection reset by peer"), the script exited 1 as it should - and
# left KP_IMAGE_TAG=v0.2.0 behind. Every run after that read that tag, decided
# it was already on v0.2.0, and did nothing, for ever, with nothing running.
#
# There is deliberately no fallback to KP_IMAGE_TAG when this file is missing:
# a host in that state must deploy, not inherit the belief that broke it.
STATE_FILE="${KP_STATE_FILE:-/var/lib/knowpilot/deployed}"
CURRENT="$(cat "${STATE_FILE}" 2>/dev/null || true)"
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

# A release is the images AND the file that runs them. Pinning only the images
# left docker-compose.prod.yml at whatever `main` happened to hold, so a change
# to it sat on the host until some unrelated release carried it - and until
# then the running stack and the tag it claimed to be silently disagreed.
# Found the hard way: a one-character permission fix could not be deployed at
# all, because the deployer had nothing new to react to.
#
# .env.production is git-ignored, so --force cannot take the secrets with it.
checkout() {
    [[ -d "${PROJECT_DIR}/.git" ]] || return 0
    git -C "${PROJECT_DIR}" fetch --tags --quiet origin         || fail "cannot fetch ${REPOSITORY} into ${PROJECT_DIR}"
    git -C "${PROJECT_DIR}" checkout --force --quiet "$1"         || fail "${PROJECT_DIR} has no $1 to check out"
}

# Put the env file back the way it was, so a failed attempt does not leave
# compose pointing at a tag this host never managed to run.
restore_tag() { [[ -n "${CURRENT}" ]] && write_tag "${CURRENT}"; return 0; }

say "Deploying ${LATEST}"
checkout "${LATEST}"
write_tag "${LATEST}"

# 2.5 GB over someone else's network. Seen for real: a reset after ninety
# minutes of pulling. One lost connection is not a reason to give up on a
# release - and the signatures were verified above, so a retry cannot fetch
# anything else.
pulled=""
for attempt in 1 2 3; do
    if compose pull --quiet; then
        pulled=1
        break
    fi
    echo "    pull attempt ${attempt} of 3 failed" >&2
    sleep 30
done
if [[ -z "${pulled}" ]]; then
    restore_tag
    fail "could not pull the images for ${LATEST} after three attempts"
fi

compose up -d --remove-orphans || {
    restore_tag
    fail "compose refused to start ${LATEST}"
}

# --- did it come up? ---------------------------------------------------------
# Every container that declares a healthcheck must reach `healthy`. A container
# that only says `running` says nothing: a process can be up and refusing every
# request, which is the failure this whole script exists to catch.
# Pipe-separated on purpose: with spaces, a container that declares no
# healthcheck shifts every field and the columns stop meaning what they say.
#
# `ps -a`, not `ps`: api and worker sit in `created` until the model job has
# finished downloading the weights. A filter that only looked at `running`
# rows would not see them at all, and a stack whose two main services never
# started would be reported as deployed. Observed on the first real
# deployment: api||created, worker||created, everything else healthy.
unhealthy() {
    local rows
    rows="$(compose ps -a --format '{{.Service}}|{{.Health}}|{{.State}}|{{.ExitCode}}' 2>/dev/null)"
    # Nothing at all is not success either.
    if [[ -z "${rows}" ]]; then
        echo "nothing is running"
        return 0
    fi
    awk -F'|' '
        $3 == "running" { if ($2 != "" && $2 != "healthy") print $1 " (" $2 ")"; next }
        $3 == "exited"  { if ($4 != "0") print $1 " (exit " $4 ")"; next }
        { print $1 " (" $3 ")" }
    ' <<< "${rows}"
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
    # Only now. This file is the record that ${LATEST} actually ran here.
    mkdir -p "$(dirname "${STATE_FILE}")"
    printf '%s\n' "${LATEST}" > "${STATE_FILE}"
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
checkout "${CURRENT}"
write_tag "${CURRENT}"
compose up -d --remove-orphans
echo "Rolled back to ${CURRENT}. ${LATEST} did not become healthy." >&2
exit 1
