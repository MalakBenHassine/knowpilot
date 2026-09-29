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
out, the release is recorded as having failed on this host and the previous one
is put back: its tag, its compose file, and its containers.

**And when there is no new release, it reconciles.** Comparing tags answers
*what should run here*; it says nothing about what is running. Treating a match
as success is what let this host sit dead for two and a half hours while the
timer reported `already on v0.2.3` every five minutes - the one state a
deployer exists to notice was the state it congratulated itself on. So a run
that finds the tag already correct still reads the health of every container,
and brings up the ones that have stopped. Between releases, converging is the
whole job.

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
- **A rollback is reported, not retried** - which this ADR claimed for the
  wrong reason, and therefore did not get. `Restart=` governs whether *systemd*
  starts the unit again after it exits; it has no bearing on the **timer**,
  which fires five minutes later regardless. The script it ran had no memory of
  the failure, so it would pull 2.5 GB, take the stack down for the whole health
  timeout, roll back, and do it again - for ever. One bad tag would not have
  cost a failed deployment but a permanent cycle of outages, worse than
  deploying nothing. The no-retry property needed a record, not the absence of a
  directive: a tag that failed here is written to `/var/lib/knowpilot/failed`
  and skipped by every later run. Since image tags are immutable, the cure for a
  bad release is the next release, and an operator who wants the same one
  retried deletes the file.
- **Three records, three questions.** `deployed` says which release has been
  healthy here, `failed` which must never be tried again, and `KP_IMAGE_TAG`
  which tag the next `compose up` will use. The first version of this script had
  only the last one and read it as all three, which wedged a real host: a pull
  cut off after ninety minutes left the tag written, and every run afterwards
  concluded it was already deployed - with nothing running at all.
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
