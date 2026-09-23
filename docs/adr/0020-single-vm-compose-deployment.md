# ADR-0020: Deploy on one VM with Docker Compose, behind Caddy

- **Status:** Accepted
- **Date:** 2026-09-22

## Context

The product needs to run on the internet, on free infrastructure, with the
security properties the code already assumes:

- one HTTPS origin, which the `__Host-` session cookie requires;
- private data stores;
- no secret in the repository.

Two facts about the application constrain the choice more than any
preference:

- **Memory.** The API and the worker each load a 2.2 GB embedding model.
  With PostgreSQL and Keycloak, the stack needs about 7 GB of RAM.
- **CPU architecture.** The only free tier with that much memory is Oracle
  Cloud's Always Free Ampere instance: 4 ARM cores and 24 GB of RAM.
  `uv.lock` already contains the aarch64 builds of PyTorch, so the images can
  be built for ARM.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| Kubernetes (k3s) | The industry target; rolling updates; the CV mentions it | A control plane on the same 24 GB; manifests, an ingress and a secret store to operate, for one replica of each service |
| PaaS free tiers (Render, Railway, Fly) | No server to manage | 512 MB to 1 GB of RAM: the embedding model alone does not fit. They also sleep on idle, and a cold start reloads 2.2 GB |
| **Docker Compose on one VM, Caddy in front** | The same images and the same healthchecks as development; one file to review; automatic HTTPS | One machine is one point of failure; updates restart containers |

Compose was chosen because it matches what the product needs today. The
images, healthchecks, networks and one-shot jobs map one-to-one onto
Kubernetes objects (Deployment, readiness probe, NetworkPolicy, Job) the day
more than one machine is needed.

## Decision

- **Caddy is the only published container**, on ports 80 and 443. It
  obtains and renews Let's Encrypt certificates on its own. It routes `/api`
  to the API (with immediate flushing, for SSE), `/auth` to Keycloak, and
  everything else to the SPA. It never exposes the Keycloak admin console,
  the master realm or the API's `/metrics`. It sets HSTS and a strict
  Content-Security-Policy on the SPA, and removes the OAuth `code` from its
  access logs.
- **Two networks.**
  - `edge` is Caddy and its upstreams.
  - `backend` is marked `internal`, with no route to the internet:
    PostgreSQL, Redis, the worker and the migration job live there.
  - The API is on both, because it calls Groq. The worker is on `backend`
    only: a compromised document parser has no network to send data out on.
- **One-shot jobs make the start order explicit.**
  - `model` downloads the weights into a volume. It is the only container
    with a download network, and it checks the dimensions with the same
    function the services use.
  - `migrate` applies the migrations.
  - The API and the worker wait for both with
    `service_completed_successfully`, then mount the weights read-only with
    `HF_HUB_OFFLINE=1`.
- **Containers are hardened by default:** `no-new-privileges`, all Linux
  capabilities dropped (Caddy keeps `NET_BIND_SERVICE`), read-only root
  filesystems with a tmpfs `/tmp` for the API, the worker and the frontend,
  memory limits, and rotated logs.
- **Images are built by CI.** The Release workflow builds x86 and ARM on
  native runners, pushes by digest, then tags the multi-platform image with
  the commit sha, with provenance and an SBOM. The server pulls by sha:
  rolling back means setting the previous sha.
- **Keycloak is pre-built** (`kc.sh build`, then `start --optimized`), under
  `/auth`, with its hostname set to the public URL. Self-registration is
  **off** in production until email verification exists; people are invited
  with `infra/keycloak/invite-user.sh`.
- **The configuration fails closed.** In production, the API refuses to
  start without the OIDC secret or a database password, or with an http URL
  or a Redis URL without a password. Empty variables mean "use the default"
  (`env_ignore_empty`), so every default lives in `config.py` only.
- **The API reaches Keycloak through the public URL**, because the issuer of
  every token must be the one browsers see. A network alias on Caddy keeps
  that hop inside the machine.

## Consequences

- One `.env.production` (mode 600) holds every secret on the server. The
  secrets are generated with `openssl rand -hex 32`, and none is ever copied
  from one tool to another: `setup-realm.sh` SETS the client secret in
  Keycloak from that file.
- The development and production stacks have different project names
  (`knowpilot` and `knowpilot-prod`). With the same name they would share
  volume names, and production migrations would run against the development
  database on a machine where both exist. This was found before the first
  local rehearsal.
- Downtime during an update is the restart time of the API, about twenty
  seconds while the model loads. This is accepted for v1.

## Revisit when

- There is more than one machine, or updates need zero downtime: move to
  k3s. The compose file is already shaped like the manifests.
- Secrets are rotated regularly: Docker secrets, or a secret manager,
  instead of environment variables.
- Email verification exists: re-open self-registration.
