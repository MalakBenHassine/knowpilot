#!/usr/bin/env bash
# Proves the NetworkPolicies from inside the cluster.
#
# This script exists because of one fact: a NetworkPolicy is enforced by the
# CNI, not by Kubernetes. On a CNI that ignores them - kindnet, for instance -
# `kubectl apply` succeeds, `kubectl get netpol` lists them, and nothing is
# blocked. No error, anywhere. Reading the YAML tells you what was INTENDED;
# only a connection attempt tells you what happens.
#
#     ./k8s/test-isolation.sh
#
# It starts one pod per component, wearing the labels the policies select on,
# tries a few TCP connections from inside each, and deletes them. Nothing it
# does touches the application's data.
#
# WHY THE PODS ARE LONG-LIVED, AND WHY IT WAITS. The first version ran one
# short pod per probe, each connecting the moment it started. It reported
# three failures, then a DIFFERENT three on the next run. The policies were
# not the problem: kube-router programs a pod's rules a few seconds AFTER the
# pod is running, so a container that connects immediately can slip through a
# window where nothing is enforced yet. A flapping security test is worse than
# no test - it teaches people to re-run it until it passes.
set -uo pipefail

NAMESPACE="${KP_NAMESPACE:-knowpilot}"
# Long enough for a new pod's rules to be in place. Measured: the window is a
# few seconds; this leaves room on a loaded laptop.
SETTLE_SECONDS="${KP_SETTLE_SECONDS:-25}"
# 1.1.1.1:443 rather than a hostname: no DNS, no TLS, no certificate - just
# "can a packet leave for the internet", which is exactly what the policies
# decide.
INTERNET=1.1.1.1

failures=0
roles=(worker api frontend)

# shellcheck disable=SC2329  # invoked by the trap below, which shellcheck cannot see
cleanup() {
    kubectl -n "$NAMESPACE" delete pod -l knowpilot.test=isolation \
        --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

start_probe() {
    local as="$1"
    kubectl -n "$NAMESPACE" delete pod "probe-$as" --ignore-not-found --wait=true >/dev/null 2>&1
    kubectl -n "$NAMESPACE" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: probe-$as
  labels:
    app.kubernetes.io/name: $as
    knowpilot.test: isolation
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 65534
    runAsGroup: 65534
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: probe
      image: busybox:1.37
      command: ["sleep", "600"]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: [ALL]
      resources:
        requests:
          cpu: 10m
          memory: 16Mi
        limits:
          memory: 32Mi
YAML
}

probe() {
    local as="$1" host="$2" port="$3" expected="$4" why="$5"
    local actual="blocked"

    if kubectl -n "$NAMESPACE" exec "probe-$as" -- nc -z -w 6 "$host" "$port" >/dev/null 2>&1; then
        actual="open"
    fi

    if [[ "$actual" == "$expected" ]]; then
        printf '  OK      %-9s -> %-9s %-8s %s\n' "$as" "$host" "$actual" "$why"
    else
        printf '  FAILED  %-9s -> %-9s %-8s expected %s - %s\n' \
            "$as" "$host" "$actual" "$expected" "$why"
        failures=$((failures + 1))
    fi
}

echo "Network isolation in namespace $NAMESPACE"
echo

for role in "${roles[@]}"; do
    start_probe "$role"
done
for role in "${roles[@]}"; do
    kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/probe-$role" --timeout=90s >/dev/null
done

echo "Letting the CNI program the rules of the new pods (${SETTLE_SECONDS}s)..."
sleep "$SETTLE_SECONDS"
echo

# The rule the whole exercise is about. In the Compose stack the worker has no
# interface to the outside; here it has one, and only a policy stops it.
probe worker "$INTERNET" 443 blocked \
    "a compromised document parser has nowhere to send what it read"
probe worker postgres 5432 open "it indexes into the database"
probe worker redis 6379 open "it takes its jobs from the queue"

# The API is the one process that legitimately calls out.
probe api "$INTERNET" 443 open "it calls Groq"
probe api postgres 5432 open "it retrieves passages"

# A static file server that opens connections is a static file server that
# has been replaced.
probe frontend postgres 5432 blocked "nginx has no business with the database"
probe frontend "$INTERNET" 443 blocked "nor with the internet"

echo
if [[ "$failures" -eq 0 ]]; then
    echo "All probes behaved as the policies say they should."
else
    echo "$failures probe(s) disagreed with the policies."
    echo "If everything is open, the CNI probably does not implement"
    echo "NetworkPolicy at all - check which one the cluster runs."
fi
exit "$failures"
