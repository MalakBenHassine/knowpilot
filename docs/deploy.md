# Deploying KnowPilot

One virtual machine, Docker Compose, Caddy in front (ADR-0020). Every command
below runs **on the server**, in the checkout, unless it says otherwise.

## 1. The machine

The stack needs about **7 GB of RAM**: the API and the worker each hold the
2.2 GB embedding model. The free option that fits is **Oracle Cloud Always
Free, Ampere A1**: up to 4 ARM cores and 24 GB of RAM. The images are built
for ARM and x86 alike.

- Ubuntu 24.04, 4 OCPU / 24 GB, a 100 GB boot volume.
- Open **TCP 80 and 443** (and UDP 443 for HTTP/3) in the **cloud** firewall,
  and nothing else except SSH. On Oracle that is the VCN security list, and no
  script on the instance can do it for you.
- Then harden the machine itself:
  ```bash
  sudo ./infra/server/harden.sh
  ```
  A firewall that denies by default, SSH keys only, fail2ban on the SSH port
  and automatic security updates. It is idempotent, and it **refuses to
  disable password authentication if no `authorized_keys` exists anywhere** -
  on a cloud instance with no console, that mistake has no way back.

  It also puts Docker back under the firewall. Docker writes its own iptables
  rules and published ports bypass ufw entirely, so `ufw deny 5432` is obeyed
  by everything except the container that published 5432. The script routes
  Docker's `DOCKER-USER` chain through ufw. KnowPilot does not depend on that
  - `docker-compose.prod.yml` publishes only Caddy, and binds Prometheus and
  Grafana to `127.0.0.1` - but the day somebody adds a `ports:` in a hurry,
  the firewall is already the one deciding.
- Docker Engine with the Compose plugin, from Docker's own repository
  (docs.docker.com/engine/install/ubuntu). Add your user to the `docker`
  group, then log out and back in.

## 2. A name

Let's Encrypt needs a DNS name. Without buying one, **sslip.io** resolves any
name containing an IP to that IP, with no account to create:

    knowpilot.203-0-113-10.sslip.io  →  203.0.113.10

Replace the dashes with your server's public IP.

## 3. Configuration

```bash
git clone https://github.com/MalakBenHassine/knowpilot.git && cd knowpilot
cp .env.production.example .env.production
./infra/server/fill-secrets.sh
```

The script generates every empty password and the client secret with
`openssl rand -hex 32` (hex, because the Redis password travels inside a URL,
where a `/` would have to be escaped by every reader of it), sets the file to
mode 600, and prints nothing. Run it twice and nothing changes: a value that
is already there is a value PostgreSQL or Keycloak may already have stored.

It then lists what it deliberately did not fill, because those are decisions
and credentials rather than random bytes - the domain, the URLs, and the Groq
key to paste from console.groq.com/keys.

## 4. Images

**Pulled from CI (recommended).** The Release workflow publishes the images
when a version tag is pushed - never on every commit, so nothing is published
that was not chosen. To cut one, from a commit whose checks are green:

```bash
git tag -a v0.3.0 -m "what this release contains"
git push origin v0.3.0
```

Each image then carries two tags: the release name and the full commit sha.
Deployments use the sha, because it can never be moved - rolling back is
setting the previous one. In `.env.production`:

```bash
KP_IMAGE_REGISTRY=ghcr.io/malakbenhassine
KP_IMAGE_TAG=<full commit sha, from the release run in the Actions tab>
```

The first time, make the three `knowpilot-*` packages public on GitHub
(Packages → Package settings → Change visibility). Otherwise, run
`docker login ghcr.io` with a token that has `read:packages` only.

**Check the signature before the first pull.** Every image is signed by the
Release workflow with a keyless certificate; verifying it is what turns "the
registry served me these bytes" into "CI built these bytes, from this commit".
Install cosign once, then, for each of the three images:

```bash
IMAGE=ghcr.io/malakbenhassine/knowpilot-backend:<sha>
cosign verify "$IMAGE" \
  --certificate-identity "https://github.com/MalakBenHassine/knowpilot/.github/workflows/release.yml@refs/tags/v0.3.0" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com"
```

The identity must name the tag you are deploying. A failure here means the
tag does not point at what the workflow produced.
Stop and find out why; do not start the stack.

**Or built on the server:** leave both variables unset, then:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production build
```

## 5. First start

```bash
alias kp='docker compose -f docker-compose.prod.yml --env-file .env.production'

kp pull                       # skip when building on the server
kp up -d
kp ps                         # wait until everything is healthy
```

The first start downloads the embedding model (2.2 GB, a few minutes, `kp
logs -f model`), applies the migrations, then starts the API and the worker.
Caddy obtains the certificate on the first request to the domain.

Then create the realm and its client. The client secret is **set** from
`.env.production`, so nothing needs to be copied:

```bash
KP_ENV_FILE=.env.production KP_COMPOSE_FILE=docker-compose.prod.yml \
    ./infra/keycloak/setup-realm.sh
```

Self-registration is off. Invite each user:

```bash
KP_ENV_FILE=.env.production KP_COMPOSE_FILE=docker-compose.prod.yml \
    ./infra/keycloak/invite-user.sh someone@example.org
```

It prints a temporary password, valid for the first login only. Send it by a
channel other than the one carrying the link.

## 6. Check

```bash
curl -fsS https://$KP_DOMAIN/api/health/ready     # {"status":"ready",...}
curl -s -o /dev/null -w "%{http_code}\n" https://$KP_DOMAIN/auth/admin/   # 404
```

Then log in through the browser, upload a document and ask a question.

## 7. Watching it

Prometheus and Grafana run with the rest of the stack, and publish on
`127.0.0.1` only. Nothing about them is reachable from the internet, so
reading a dashboard means bringing the port to your own machine:

```bash
ssh -N -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 ubuntu@<the server>
```

Leave that running, then open <http://localhost:3000> and log in with
`KP_GRAFANA_ADMIN` / `KP_GRAFANA_ADMIN_PASSWORD`. The **KnowPilot** dashboard
is already there: it is provisioned from the repository, not created by hand.
Prometheus itself is on <http://localhost:9090>, where
*Status -> Targets* must show `api` and `worker` **up** - the worker is the
one that matters, because the API can answer every request while no upload is
being indexed at all.

The alert rules are evaluated every fifteen seconds and shown under
*Alerts*. They are **not delivered anywhere**: notifying needs a mail or chat
channel this deployment does not have (ADR-0021). Until one exists, the
dashboard is what an operator looks at.

## 8. Updates and rollback

```bash
# in .env.production: KP_IMAGE_TAG=<new sha>
git pull                      # compose file, Caddyfile, scripts
kp pull && kp up -d
```

`migrate` runs again, and the API and the worker restart only if it
succeeded. To roll back, set the previous sha and run `kp up -d` again. A
migration is only rolled back by hand (`kp run --rm migrate alembic downgrade
-1`): read it first.

## 9. Backups

The state is in three volumes: `postgres_data` (documents, chunks, users),
`uploads` (the original files) and `caddy_data` (certificates). `models` can
be downloaded again.

```bash
kp exec -T postgres pg_dumpall -U "$KP_POSTGRES_USER" | gzip > "backup-$(date +%F).sql.gz"
docker run --rm -v knowpilot-prod_uploads:/data:ro -v "$PWD":/out alpine \
    tar czf "/out/uploads-$(date +%F).tar.gz" -C /data .
```

Copy both files **off the machine**: a backup on the server it protects is
not a backup. Restore them once, on a scratch machine, before you need to.

## 10. Operating

| Need | Command |
| --- | --- |
| Logs of one service | `kp logs -f api` |
| Dashboards | the SSH tunnel of section 7 |
| Raw metrics of the API | `kp exec api python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/metrics').read().decode())"` |
| Raw metrics of the worker | `kp exec worker python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:9100/metrics').read().decode())"` |
| Keycloak administration | the scripts in `infra/keycloak/`, never the web console (not exposed) |
| Re-index every document | `kp exec api python -m scripts.reindex` |
| Disk usage | `docker system df` |
