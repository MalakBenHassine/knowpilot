# ADR-0022: Kubernetes manifests for a local cluster, while production stays on Compose

- **Status:** Accepted
- **Date:** 2026-09-23

## Context

[ADR-0020](0020-single-vm-compose-deployment.md) chose Docker Compose on one
VM and wrote down why k3s was rejected: a control plane on the same 24 GB,
manifests and an ingress to operate, for one replica of each service. That
reasoning has not changed.

What changed is that there is **no VM**. The only free tier with enough memory
demands a payment card, so production is not running anywhere - and the
sentence at the end of ADR-0020 became the only way left to demonstrate the
operational model:

> The images, healthchecks, networks and one-shot jobs map one-to-one onto
> Kubernetes objects the day more than one machine is needed.

A claim like that is worth exactly as much as the attempt to carry it out.
This ADR records the attempt, including the place where the claim was wrong.

## Decision

**Kustomize, not Helm.** A reviewer reads YAML. Helm's templates hide the
object behind a language, which is a good trade when a chart is published for
strangers to configure, and a bad one when the point is to be read.

**One base and one overlay.** The base holds every object; `overlays/local`
supplies the secrets, the image tags, and resource requests a laptop can
meet. There is no production overlay, because production is Compose -
committing manifests nobody applies is how a repository starts lying.

**k3d, not kind.** k3s ships a CNI that enforces `NetworkPolicy`; kind's
default does not. On a cluster that ignores them, `kubectl apply` succeeds,
`kubectl get netpol` lists them, and nothing is blocked - no error anywhere.
The choice of cluster is what makes the policies testable, so it is a
decision, not a preference.

**Everything runs unprivileged, and the namespace enforces it.** The
namespace carries the `restricted` Pod Security Admission label, so a pod that
runs as root, that can escalate privileges, or that keeps a capability is
refused by the API server at apply time rather than discovered later.

## The three translations

**Healthchecks become richer, not poorer.** Compose has one healthcheck;
Kubernetes has three probes with different jobs, and the application already
had the right endpoints for them. `/api/health/live` checks nothing but the
process, so it drives `livenessProbe` - a liveness probe that tested the
database would restart a healthy API every time PostgreSQL hiccupped, turning
one outage into two. `/api/health/ready` checks the dependencies and drives
`readinessProbe`: the pod leaves the Service and takes no traffic, without
being killed. `startupProbe` covers the twenty seconds the embedding model
takes to load, which Compose could only approximate with `start_period`.

**The one-shot Jobs translate; their ordering does not.** This is the part of
the Compose file with no equivalent. `service_completed_successfully` held the
API back until `model` and `migrate` had exited 0. A Kubernetes Deployment
never looks at a Job. The order is rebuilt inside the Deployments, by two
initContainers:

- `wait-for-model` blocks until the marker the model Job writes *after* it has
  verified the dimensions of what it downloaded exists - so a truncated
  download never looks finished;
- `wait-for-migrations` compares the revision the database is at with the
  newest revision in the image. No RBAC, no reading a Job's status: the
  question "is the schema current?" is answered by the schema itself.

Running `alembic upgrade head` in every pod instead would have been shorter,
and a race between two replicas migrating the same database at the same time.

**The networks do not translate at all.** In Compose, `backend` is a
*topology*: the worker has no interface to the outside, so there is nothing to
configure and nothing to get wrong. In Kubernetes the network is flat, and
isolation is a *rule* enforced by a component chosen separately. A
`default-deny` policy plus one exception per component reproduces the intent,
and `k8s/test-isolation.sh` proves it by opening connections from pods wearing
the labels the policies select on - because the YAML only states the
intention.

## What running it changed

The manifests assembled, validated and passed a configuration scan before
they were ever applied. Applying them found six things no amount of reading
would have:

- **The model Job died on a read-only filesystem.** `huggingface_hub` writes
  more than the weights - a lock, a staging directory for its chunked
  downloader, metadata - and all of it defaults to `$HOME/.cache`. The Compose
  version never hit this, because that job's root filesystem is writable.
  `HF_HOME` now points into the volume.
- **The migration Job exhausted its retries in sixty seconds.** It starts the
  moment it is created, which on a fresh cluster is before PostgreSQL exists.
  Three name resolution failures and it was done. Compose held it back with
  `depends_on: service_healthy`; here it needed the same initContainer
  treatment as the Deployments. The missing dependency mechanism shows up
  twice, in two different places.
- **nginx refuses to start without a writable `/tmp`**, and **Keycloak writes
  a truststore into its data directory on every start**, external database or
  not. A read-only root filesystem is not a setting you turn on; it is a list
  of paths you then have to provide.
- **Keycloak's health endpoints are under `/auth`.** The management interface
  inherits `KC_HTTP_RELATIVE_PATH`, so `/health/started` is a 404. The startup
  probe failed twenty-three times against a Keycloak whose own log said the
  bootstrap had completed.
- **The worker sat at 0/1 for fifteen minutes while its log said it was
  ready.** `arq --check` is not a request to a running server: it starts a
  Python interpreter that imports the application, torch included, and takes
  about seven seconds. A probe's default `timeoutSeconds` is one.
- **The isolation test was flaky, and the policies were not.** The first
  version started a pod per probe and connected immediately; it reported three
  failures, then three different ones. kube-router programs a pod's rules a
  few seconds after the pod is running, so a container that connects at once
  slips through a window where nothing is enforced. The test now uses
  long-lived pods and waits. It passes seven out of seven, twice in a row -
  and the worker cannot reach the internet, which was the point.

## Consequences

- The claim in ADR-0020 is now backed by 32 objects, rather than by a
  sentence. They assemble, they pass a strict schema validation - 31 of them
  against the catalogue, the Traefik `Middleware` being a CRD whose schema
  lives in the cluster and is skipped - and a configuration scan with no
  HIGH or CRITICAL finding.
- `ReadWriteOnce` on the shared volumes is a **real** limit, not an oversight.
  On one node the API, the worker and the Jobs mount the same claim happily.
  On a second node they would not: that day needs ReadWriteMany (NFS, EFS,
  Longhorn) or the weights baked into the image, at 2.2 GB per pull.
- The secrets are a git-ignored env file read by Kustomize's
  `secretGenerator`. Not SOPS, not sealed-secrets: the file never leaves the
  machine that created it, so there is nothing to encrypt *for*. The day these
  manifests are applied by a pipeline rather than by a person at a keyboard,
  the secret has to travel, and that is the day it needs to be encrypted at
  rest.
- The monitoring stack of [ADR-0021](0021-monitoring-stack.md) is **not**
  translated. Prometheus and Grafana in Kubernetes are a different subject -
  an operator, `ServiceMonitor` objects, a bundled chart - and putting two
  plain Deployments here would have demonstrated nothing the Compose stack
  does not already.

## Revisit when

- There is a second node. Then `ReadWriteOnce`, the `Recreate` strategies and
  the single replicas are all up for review at once.
- Something other than a person applies these manifests. Then the secrets
  need encryption at rest, and the initContainer waits are better expressed as
  sync-waves or hooks by whatever does the applying.
- Production moves off Compose. Then the base needs the overlay this ADR
  deliberately does not contain.
