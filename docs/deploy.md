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

**On a private address there is no public certificate to be had.** A VM on a
laptop, or any host behind NAT, cannot pass an ACME challenge: the challenge
has to reach port 80 from the internet. Set

    KP_TLS=tls internal

and Caddy signs with its own CA instead. Browsers warn once and everything
else behaves identically. Leave `KP_TLS` empty on a public server - that is
Let's Encrypt, which is what you want there.

Without this the stack still starts, and Caddy holds no certificate at all: no
HTTPS, and every request refused at the handshake. The Ansible playbook decides
this from the host's own address, so a private machine gets it right by itself
([infra/ansible](../infra/ansible/README.md)).

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
Install cosign **3.0 or newer**, then, for each of the three images:

```bash
IMAGE=ghcr.io/malakbenhassine/knowpilot-backend:<sha>
cosign verify "$IMAGE" \
  --certificate-identity "https://github.com/MalakBenHassine/knowpilot/.github/workflows/release.yml@refs/tags/v0.3.0" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com"
```

The identity must name the tag you are deploying. A failure here means the
tag does not point at what the workflow produced.
Stop and find out why; do not start the stack.

**The version matters, and the error does not say so.** These signatures
are written in the bundle format cosign 3 introduced. A cosign 2.x client
reports `no signatures found` - which reads like *there is no signature*,
when it means *there is one and I cannot see it*. Checked against the
published images: v3.0.2 verifies them, v2.6.1 does not. If you get that
message, run `cosign version` before you suspect the release.

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

When someone loses it - which is the ordinary case, not a mistake - the same
script issues a new one and puts `UPDATE_PASSWORD` back, so the value stays
usable exactly once:

```bash
KP_ENV_FILE=.env.production KP_COMPOSE_FILE=docker-compose.prod.yml     ./infra/keycloak/invite-user.sh --reset someone@example.org
```

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

## 8. Updates, automatic

A systemd timer deploys every new release on its own, and rolls back a
release that does not come up ([ADR-0023](adr/0023-pull-based-deployment.md)).

**Install cosign 3 first** - the deployer refuses to deploy what it cannot
verify, and a 2.x client cannot read these signatures:

```bash
curl -fsSLO https://github.com/sigstore/cosign/releases/download/v3.0.2/cosign-linux-amd64
curl -fsSL https://github.com/sigstore/cosign/releases/download/v3.0.2/cosign_checksums.txt |
  grep ' cosign-linux-amd64$' | sha256sum -c
sudo install -m 755 cosign-linux-amd64 /usr/local/bin/cosign && rm cosign-linux-amd64
```

Then see what it would do, before letting it do anything:

```bash
KP_DRY_RUN=1 ./infra/server/update.sh
```

It prints the deployed tag, the newest release, and the result of verifying
each image. Nothing is written. When that reads correctly:

```bash
sudo ln -sf "$PWD/infra/server/update.sh" /usr/local/bin/knowpilot-update
sudo cp infra/server/knowpilot-update.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now knowpilot-update.timer
```

The symlink is why the unit never has to know where the checkout lives.

Watch it:

```bash
systemctl list-timers knowpilot-update.timer   # when it next fires
journalctl -u knowpilot-update.service -f      # what it did
sudo systemctl start knowpilot-update.service  # now, without waiting
```

A run that refuses says why and changes nothing. A run that deploys and then
finds a container never became healthy puts the previous release back - the
tag, the compose file that belongs to it, **and** the containers - and exits
non-zero, so `systemctl status` is red and the journal holds the reason.

It does **not** retry: a timer that reinstalls a broken release every five
minutes is a loop, not a recovery. The tag goes to
`/var/lib/knowpilot/failed`, and every later run skips it:

```
==> v0.2.4 already failed to become healthy here; not retrying
```

The cure for a bad release is the next release, which is not in that file.
Image tags here are immutable, so there is nothing to wait for. Force a retry
only when the fault was the machine and not the code - a full disk, a pull
cut off by the network - by deleting the file:

```bash
sudo rm /var/lib/knowpilot/failed
```

Between releases the timer is not idle. A matching tag says what *should*
run, not that anything does, so every run also looks at the containers and
brings back the ones that have stopped:

```
==> On v0.2.3 but not healthy: api (unhealthy) worker (exited)
==> Reconciling v0.2.3
==> Reconciled v0.2.3
```

This is the state that used to be reported as success. Without it the host
stayed dead for two and a half hours while the timer said `already on v0.2.3`
every five minutes.

Three records say what the host believes, and they answer different
questions - never read one for another:

| Record | Question it answers |
| --- | --- |
| `/var/lib/knowpilot/deployed` | which release has actually been healthy here |
| `/var/lib/knowpilot/failed` | which releases must never be tried again |
| `KP_IMAGE_TAG` in `.env.production` | which tag compose uses on the next `up` |

## 9. Updates, by hand

Still the way to move to a version that is not the newest release, and the
way to apply a change to the compose file or the Caddyfile:

```bash
sudo systemctl stop knowpilot-update.timer    # so it does not undo this
# in .env.production: KP_IMAGE_TAG=<tag or sha>
git pull                      # compose file, Caddyfile, scripts
kp pull && kp up -d
```

`migrate` runs again, and the API and the worker restart only if it
succeeded.

**The deployer cannot roll back a migration, and neither can setting the old
tag.** A migration is only rolled back by hand (`kp run --rm migrate alembic
downgrade -1`): read it first. This is why migrations are written additively -
a new column is nullable, a removal waits for the release after the one that
stopped using it. A release that cannot be rolled back by restarting the
previous images is a release the automation cannot save.

Start the timer again when you are done:

```bash
sudo systemctl start knowpilot-update.timer
```

## 10. Backups

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

## 11. Operating

| Need | Command |
| --- | --- |
| Logs of one service | `kp logs -f api` |
| Dashboards | the SSH tunnel of section 7 |
| Raw metrics of the API | `kp exec api python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/metrics').read().decode())"` |
| Why a release is being skipped | `cat /var/lib/knowpilot/failed` |
| What has really run here | `cat /var/lib/knowpilot/deployed` |
| Raw metrics of the worker | `kp exec worker python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:9100/metrics').read().decode())"` |
| Keycloak administration | the scripts in `infra/keycloak/`, never the web console (not exposed) |
| Re-index every document | `kp exec api python -m scripts.reindex` |
| Disk usage | `docker system df` |
