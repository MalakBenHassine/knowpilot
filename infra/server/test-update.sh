#!/usr/bin/env bash
# Exercise every branch of update.sh against real containers.
#
#     ./infra/server/test-update.sh
#
# Why this file exists: the reconcile branch shipped without one, and its first
# version could not tell a container that had STOPPED from one that was running
# with a red healthcheck. `compose up -d` cures the first and does nothing at
# all for the second, so the deployer sat waiting out a thirty-minute
# deployment budget for a condition it could never fix, forty-one minutes per
# cycle, holding the lock that any real release would have needed. Three hours
# of it on a live host - while the container it was chasing answered every
# request, its probe having merely run out of a five-second budget three times
# in a row. Case 3 is that bug. It takes seconds to fail now, and this test
# says so.
#
# It needs docker and the network: the deployer asks GitHub which release is
# newest, so this test asks the same question and builds its scenarios around
# the real answer rather than pretending to be the API.
#
# It cannot touch production. Its own compose project, its own env file, its
# own state files and its own lock, all under /tmp/knowpilot-update-test, and
# all removed on the way out.
set -uo pipefail

REPOSITORY="${KP_GITHUB_REPOSITORY:-MalakBenHassine/knowpilot}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SANDBOX=/tmp/knowpilot-update-test

passed=0
failed=0
OUT=""
RC=0
ELAPSED=0

ok()   { printf '     ok   %s\n' "$1"; passed=$(( passed + 1 )); }
ko()   { printf '     KO   %s\n' "$1"; failed=$(( failed + 1 )); }
case_() { printf '\n%s\n' "$1"; }

expect_rc() {
    if [[ "${RC}" == "$1" ]]; then ok "exit ${RC}"; else ko "exit ${RC}, expected $1"; fi
}
expect_says() {
    if grep -qF -- "$1" <<< "${OUT}"; then ok "says \"$1\""; else ko "never says \"$1\""; fi
}
expect_silent_about() {
    if grep -qF -- "$1" <<< "${OUT}"; then ko "should not say \"$1\""; else ok "silent about \"$1\""; fi
}
expect_faster_than() {
    if (( ELAPSED < $1 )); then
        ok "finished in ${ELAPSED}s, under $1"
    else
        ko "took ${ELAPSED}s, expected under $1"
    fi
}
expect_file() {
    local got
    got="$(cat "$1" 2>/dev/null)"
    if [[ "${got}" == "$2" ]]; then ok "$1 holds $2"; else ko "$1 holds '${got}', expected '$2'"; fi
}

compose() {
    docker compose -f "${SANDBOX}/docker-compose.prod.yml" --env-file "${SANDBOX}/.env" "$@"
}

deployer() {
    local start=${SECONDS}
    OUT="$("${SANDBOX}/update.sh" 2>&1)"
    RC=$?
    ELAPSED=$(( SECONDS - start ))
}

healthy()   { mkdir -p "${SANDBOX}/flag"; : > "${SANDBOX}/flag/ok"; sleep 6; }
unhealthy() { rm -f "${SANDBOX}/flag/ok"; sleep 6; }

cleanup() {
    compose down --remove-orphans >/dev/null 2>&1 || true
    rm -rf "${SANDBOX}"
}
trap cleanup EXIT

command -v docker >/dev/null || { echo "docker is required"; exit 1; }

LATEST="$(curl -fsSL -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/${REPOSITORY}/releases/latest" 2>/dev/null \
    | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
if [[ ! "${LATEST}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "cannot read the newest release of ${REPOSITORY}; this test needs the network"
    exit 1
fi
OLDER=v0.0.1

# --- the sandbox ------------------------------------------------------------
rm -rf "${SANDBOX}"
mkdir -p "${SANDBOX}/bin" "${SANDBOX}/state" "${SANDBOX}/flag"

# The signature is not what this file tests; update.sh refuses to run without a
# cosign on PATH, so it gets one that agrees with everything.
printf '#!/bin/sh\nexit 0\n' > "${SANDBOX}/bin/cosign"
chmod +x "${SANDBOX}/bin/cosign"

# One container whose health is a file on the host, so health can be flipped
# WITHOUT stopping it. That is the whole point: running-and-red is a different
# state from stopped, and nothing else reproduces it.
cat > "${SANDBOX}/docker-compose.prod.yml" <<'YAML'
name: knowpilot-update-test
services:
  probe:
    image: busybox:1.36
    command: ["sh", "-c", "sleep 3600"]
    volumes:
      - ./flag:/flag:ro
    healthcheck:
      test: ["CMD", "test", "-f", "/flag/ok"]
      interval: 2s
      timeout: 2s
      retries: 1
      start_period: 1s
YAML

printf 'KP_IMAGE_REGISTRY=ghcr.io/example\nKP_IMAGE_TAG=%s\n' "${LATEST}" > "${SANDBOX}/.env"
cp "${HERE}/update.sh" "${SANDBOX}/update.sh"

export PATH="${SANDBOX}/bin:${PATH}"
export KP_PROJECT_DIR="${SANDBOX}"
export KP_ENV_FILE="${SANDBOX}/.env"
export KP_COMPOSE_FILE="${SANDBOX}/docker-compose.prod.yml"
export KP_STATE_FILE="${SANDBOX}/state/deployed"
export KP_FAILED_FILE="${SANDBOX}/state/failed"
export KP_LOCK_FILE="${SANDBOX}/state/lock"
export KP_HEALTH_TIMEOUT=20
export KP_RECONCILE_GRACE=5

echo "newest release: ${LATEST}"
compose up -d >/dev/null 2>&1
healthy

# --- 1 ----------------------------------------------------------------------
case_ "1. on the newest release, and healthy: do nothing"
printf '%s\n' "${LATEST}" > "${KP_STATE_FILE}"
deployer
expect_rc 0
expect_says "and healthy; nothing to do"

# --- 2 ----------------------------------------------------------------------
case_ "2. on the newest release, but a container has stopped: bring it back"
compose stop >/dev/null 2>&1
deployer
expect_rc 0
expect_says "Reconciling"
expect_says "Reconciled"
if [[ "$(compose ps --format '{{.State}}' 2>/dev/null)" == "running" ]]; then
    ok "the container is running again"
else
    ko "the container is not running"
fi

# --- 3 ---------------------------------------------- the regression --------
case_ "3. running, but its probe is red: refuse fast, do not loop"
healthy
unhealthy
deployer
expect_rc 1
expect_says "running but not healthy"
expect_silent_about "Reconciling"
# The bug this guards: the first version waited out KP_HEALTH_TIMEOUT here.
expect_faster_than $(( KP_RECONCILE_GRACE + 20 ))

# --- 4 ----------------------------------------------------------------------
case_ "4. the newest release already failed here: do not retry it"
healthy
printf '%s\n' "${LATEST}" > "${KP_FAILED_FILE}"
deployer
expect_rc 0
expect_says "not retrying"
rm -f "${KP_FAILED_FILE}"

# --- 5 ----------------------------------------------------------------------
case_ "5. a new release that never becomes healthy: quarantine and roll back"
printf '%s\n' "${OLDER}" > "${KP_STATE_FILE}"
unhealthy
deployer
expect_rc 1
expect_says "Rolling back to ${OLDER}"
expect_file "${KP_FAILED_FILE}" "${LATEST}"
expect_file "${KP_STATE_FILE}" "${OLDER}"
if grep -qF "KP_IMAGE_TAG=${OLDER}" "${KP_ENV_FILE}"; then
    ok "the env file is back on ${OLDER}"
else
    ko "the env file was not restored"
fi

# --- 6 ----------------------------------------------------------------------
case_ "6. the tick after a rollback: still refuse the quarantined release"
deployer
expect_rc 0
expect_says "not retrying"

printf '\n%s passed, %s failed\n' "${passed}" "${failed}"
(( failed == 0 ))
