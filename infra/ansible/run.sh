#!/usr/bin/env bash
# One entry point for provisioning, so nobody has to remember which SSH agent
# holds which key.
#
#   ./run.sh --check      # change nothing, say what would change
#   ./run.sh              # apply
#
# WHY THIS EXISTS. The key that reaches the VM has a passphrase, so it must be
# held by an agent. When it is not, ssh has nowhere to ask and Ansible reports:
#
#   ssh_askpass: exec(/usr/bin/ssh-askpass): No such file or directory
#   mlek@...: Permission denied (publickey)
#   UNREACHABLE!
#
# which names neither the cause nor the fix. It happened three times in one
# afternoon, each time in a new terminal, and each time the answer was a socket
# path copied by hand. Operational knowledge that lives in someone's memory is
# a defect; this file is where it lives instead.
set -euo pipefail

KEY="${KP_VM_KEY:-${HOME}/.ssh/id_ed25519_knowpilot_vm}"
# A FIXED path, not the per-shell /tmp/ssh-XXXX/agent.NNN: an agent at a
# predictable address is one every terminal can find. This is the same socket
# ~/.bashrc sets up.
export SSH_AUTH_SOCK="${SSH_AUTH_SOCK:-${HOME}/.ssh/agent.sock}"

die() { printf 'run.sh: %s\n' "$1" >&2; exit 1; }

[[ -f "${KEY}" ]] || die "no key at ${KEY}. Create it with: ssh-keygen -t ed25519 -f ${KEY}"

# ssh-add -l exits 2 when no agent is listening, 1 when one is but holds
# nothing, 0 when it holds keys. Only 2 means we must start one.
agent_status=0
ssh-add -l >/dev/null 2>&1 || agent_status=$?
if (( agent_status == 2 )); then
    echo "==> starting an agent at ${SSH_AUTH_SOCK}"
    rm -f "${SSH_AUTH_SOCK}"
    ssh-agent -a "${SSH_AUTH_SOCK}" >/dev/null
fi

# Compare fingerprints rather than counting keys: the agent also holds the
# GitHub key, and "it has one key" would be the wrong question.
want="$(ssh-keygen -lf "${KEY}.pub" | awk '{ print $2 }')"
if ! ssh-add -l 2>/dev/null | grep -qF "${want}"; then
    echo "==> the agent does not hold ${KEY##*/} yet; it will ask once"
    ssh-add "${KEY}" || die "the key was not added, so the VM is unreachable"
fi

exec ansible-playbook "${@:-site.yml}"
