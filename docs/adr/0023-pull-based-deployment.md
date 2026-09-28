# ADR-0023: The server pulls its own releases, and verifies them first

- **Status:** Accepted
- **Date:** 2026-09-28

## Context

[ADR-0020](0020-single-vm-compose-deployment.md) ends with a sentence that was
never implemented: *the server pulls by sha*. Until now a deployment meant a
person opening `docs/deploy.md`, editing `KP_IMAGE_TAG` and running
`docker compose up -d`. Everything upstream of that is automated - built once,
scanned before the tag is written, signed, published, and announced with its
digests - and then it stops at a human.

The deployment target is a VMware virtual machine on a laptop, behind NAT. It
has no public address, and nothing on the internet can open a connection to
it.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| A deploy job in GitHub Actions, over SSH | Familiar; the whole pipeline is in one place | **Cannot work here** - GitHub cannot reach a machine behind NAT. Making it reachable means a tunnel or a forwarded port, and a private key in GitHub that opens production |
| A pull agent on the server | No inbound connection, no credential anywhere but the machine itself. Works behind NAT, on a laptop, on a free tier | The server polls, so a release takes up to one interval to land. One more script to own |
| GitOps (Argo CD, Flux) | The reference answer, and pull-based too | Needs a Kubernetes cluster. Production is Compose ([ADR-0020](0020-single-vm-compose-deployment.md)) |
| Watchtower or a similar image watcher | Nothing to write | Follows a moving tag, which is the opposite of releasing on a version; and it verifies no signature |

## Decision

`infra/server/update.sh`, run by a systemd timer every five minutes.

It reads the newest release from the **public** Releases API - no token, no
credential, nothing to leak - compares it with the deployed tag, and stops
immediately if they match.

**The signature is verified on the machine that will run the code**, against
the identity `.../release.yml@refs/tags/<version>`, before anything is pulled.
This is the part that matters. The release workflow already verifies its own
signature, but a check performed by the thing that produced the artifact
proves only that the producer was consistent with itself. The registry is a
separate system, and whoever controls it can replace an image after the fact;
they cannot forge a certificate issued by GitHub's OIDC provider. The
verification here is the only one that happens after the bytes have travelled,
and the only one that can still prevent something.

Then: write the tag, `compose pull`, `compose up -d`, and wait for every
container that declares a healthcheck to become `healthy`. A container that is
merely `running` is not evidence - a process can be up and refusing every
request, which is exactly the failure this exists to catch. If the wait times
out, the previous tag is written back and the stack is brought up again.

**Push and pull differ in who holds a secret.** Deploying from CI means
storing a key in GitHub that opens production; a compromised workflow then
owns the machine. Here the machine holds everything and GitHub holds nothing,
so the worst a compromised workflow can do is publish an image - and an image
it cannot sign as the release workflow is one this script refuses.

This is continuous deployment for the delivery pipeline of ADR-0020, and it
does not replace the runbook: `docs/deploy.md` still describes the first
install, and `KP_DRY_RUN=1` shows what the timer would do without doing it.

## Consequences

- **A release reaches the machine within five minutes, unattended.** The
  window is a choice: it costs one unauthenticated API call, and the GitHub
  rate limit for those is sixty an hour per address.
- **A bad release rolls itself back.** Not a fix, a floor: the previous images
  are still local, so the rollback is a container restart.
- **A rollback is reported, not retried.** The unit has no `Restart=`: a timer
  that reinstalls a broken release every five minutes turns one bad tag into a
  loop and buries the log line that explained the first failure.
- **The script cannot roll back the database.** A release whose migration has
  already run and is not backward compatible will not be saved by writing the
  old tag back. That is a constraint on how migrations are written - additive
  first - not something a deployer can fix.
- **cosign 3 or newer is now required on the server**, because that is the
  format the release workflow signs in.
- Only signed releases deploy, which means the tag, the workflow and the
  signing identity are now load-bearing in a second place. Renaming the
  workflow file breaks deployment, not just verification.

## Revisit when

- **There is a second machine.** Five minutes of polling per server is fine;
  fifty servers hitting the API is not, and that is when this becomes a pull
  agent with a push notification, or GitOps.
- **A deployment needs to be coordinated with something else** - a migration
  that must run once across several nodes, a cache to warm. A timer per
  machine has no idea what the other machines are doing.
- **Production moves to Kubernetes.** Then Argo CD or Flux does this properly,
  and this script is deleted rather than ported.
