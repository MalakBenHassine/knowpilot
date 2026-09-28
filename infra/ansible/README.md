# Provisioning a KnowPilot host

One playbook takes a bare Ubuntu machine to one that deploys itself.

```bash
cd infra/ansible
cp inventory.example.ini inventory.ini     # put your host in it
ansible-playbook site.yml --check          # change nothing, say what would change
ansible-playbook site.yml
```

Needs `ansible-core` on the machine you run it *from*, and SSH to the target.
Nothing is installed on the target beforehand except Python, which Ubuntu has.

## What it does

| Role | |
| --- | --- |
| `base` | An engine **and Compose v2** - from Docker's repository on a bare host, or just the missing plugin on a host that already runs containers; the login user in the `docker` group; the repository at `/opt/knowpilot`; `harden.sh` |
| `knowpilot` | cosign, pinned by checksum; `.env.production` with generated secrets; the deployer symlink; both systemd units; the timer |

It ends with a **dry run of the deployer** - reading the latest release and
verifying three signatures without deploying - so a provisioning run that
would have produced a broken timer fails here instead of at four in the
morning.

## Where the line is drawn

**Provisioning is declarative and belongs in Ansible.** Packages, users,
firewall, unit files: running the playbook twice changes nothing the second
time, and that is the whole value.

**Deploying is not.** `infra/server/update.sh` runs every five minutes from a
systemd timer, verifies a signature and rolls back on a failed healthcheck.
Ansible is not on the machine at that moment, and should not be
([ADR-0023](../../docs/adr/0023-pull-based-deployment.md)). This playbook
installs the deployer; it does not replace it.

**`harden.sh` is run, not rewritten.** It is idempotent, and its refusals are
tested in containers - no key installed, an `sshd_config` that does not
validate. Reimplementing 200 lines of verified shell as tasks would mean
writing those tests again in another language, for the same behaviour.

## Secrets

**No password is ever templated from the control machine.** The playbook
copies `.env.production.example` on the target and runs
`infra/server/fill-secrets.sh` *there*, so the generated values exist only in
a mode-600 file on the host - never in this repository, never on your laptop,
never in an Ansible log. The file is never overwritten on a second run: those
passwords are the ones PostgreSQL and Keycloak already store.

`KP_GROQ_API_KEY` stays empty. It is a credential you hold, and the playbook
does not ask for it; the product runs without it and answers nothing.

## It refuses before it breaks

The stack needs about **7 GB of RAM** - the API and the worker each hold the
2.2 GB embedding model - and around 20 GB of disk. The playbook checks both
before touching anything:

```
This host has 4096 MB of RAM and the stack needs about 6000. Give the machine
more memory, or stop what is already using it, before running this again.
```

Without that check the first deployment is killed by the OOM killer halfway
through, the healthcheck never passes, and the deployer rolls back - which
looks exactly like a broken release and is not.

## Checked

- `ansible-lint` at the **production** profile: 0 findings.
- `ansible-playbook --syntax-check`.
- Run against a throwaway Ubuntu container in `--check` mode: both refusals
  produce their sentence, and a host that passes the preflight gets past it.

Two defects came out of running it rather than reading it: a variable passed
with `-e` arrives as a string, so `int >= str` raised instead of comparing;
and a host whose root mount Ansible cannot read died on a Jinja error rather
than the refusal message. Both are fixed.
