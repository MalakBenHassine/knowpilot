# Deploying KnowPilot

One virtual machine, Docker Compose, Caddy in front (ADR-0020). Every command
below runs **on the server**, in the checkout, unless it says otherwise.

## 1. The machine

The stack needs about **7 GB of RAM**: the API and the worker each hold the
2.2 GB embedding model. The free option that fits is **Oracle Cloud Always
Free, Ampere A1**: up to 4 ARM cores and 24 GB of RAM. The images are built
for ARM and x86 alike.

- Ubuntu 24.04, 4 OCPU / 24 GB, a 100 GB boot volume.
- Open **TCP 80 and 443** (and UDP 443 for HTTP/3) in the cloud firewall, and
  nothing else except SSH. On Oracle, that is both the VCN security list AND
  the instance firewall:
  ```bash
  sudo iptables -I INPUT 6 -p tcp -m multiport --dports 80,443 -j ACCEPT
  sudo iptables -I INPUT 6 -p udp --dport 443 -j ACCEPT
  sudo netfilter-persistent save
  ```
- SSH with a key only (`PasswordAuthentication no`), and unattended security
  updates: `sudo apt install unattended-upgrades`.
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
cp .env.production.example .env.production && chmod 600 .env.production
```

Fill in `.env.production`. Generate **every** password and the client secret
with `openssl rand -hex 32` (hex: the Redis password travels inside a URL).
Paste the Groq key from console.groq.com/keys.

## 4. Images

**Pulled from CI (recommended).** The Release workflow publishes the images
for every commit on `main`. In `.env.production`:

```bash
KP_IMAGE_REGISTRY=ghcr.io/malakbenhassine
KP_IMAGE_TAG=<full commit sha, from the Actions tab>
```

The first time, make the three `knowpilot-*` packages public on GitHub
(Packages → Package settings → Change visibility). Otherwise, run
`docker login ghcr.io` with a token that has `read:packages` only.

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

## 7. Updates and rollback

```bash
# in .env.production: KP_IMAGE_TAG=<new sha>
git pull                      # compose file, Caddyfile, scripts
kp pull && kp up -d
```

`migrate` runs again, and the API and the worker restart only if it
succeeded. To roll back, set the previous sha and run `kp up -d` again. A
migration is only rolled back by hand (`kp run --rm migrate alembic downgrade
-1`): read it first.

## 8. Backups

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

## 9. Operating

| Need | Command |
| --- | --- |
| Logs of one service | `kp logs -f api` |
| Metrics (from the server) | `kp exec api python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/metrics').read().decode())"` |
| Keycloak administration | the scripts in `infra/keycloak/`, never the web console (not exposed) |
| Re-index every document | `kp exec api python -m scripts.reindex` |
| Disk usage | `docker system df` |
