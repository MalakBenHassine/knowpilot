#!/usr/bin/env bash
# Hardens a fresh Ubuntu server before KnowPilot runs on it.
#
#     sudo ./infra/server/harden.sh
#
# Idempotent: run it twice and nothing changes the second time. It touches
# four things, each for a reason written next to it:
#
#   1. a firewall that denies by default, including for Docker;
#   2. SSH: keys only, no root, and never without checking first;
#   3. fail2ban, because a public SSH port is knocked on continuously;
#   4. unattended security upgrades.
#
# What it does NOT do: install Docker, open a port for the application beyond
# 80 and 443, or touch anything inside the stack. Hardening the host and
# deploying the product are two jobs, and mixing them makes both harder to
# review.
set -euo pipefail

SSH_PORT="${KP_SSH_PORT:-22}"
SSH_DROPIN=/etc/ssh/sshd_config.d/99-knowpilot.conf
DOCKER_RULES_MARKER="# BEGIN KNOWPILOT DOCKER RULES"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this with sudo." >&2
    exit 1
fi

say() { printf '\n==> %s\n' "$1"; }

# --------------------------------------------------------------------------
say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ufw fail2ban unattended-upgrades >/dev/null

# --------------------------------------------------------------------------
say "Firewall"

# The order matters more than the rules. Allowing SSH BEFORE enabling ufw is
# what stops `ufw enable` from closing the connection it is being typed into.
ufw allow "${SSH_PORT}/tcp" comment "ssh" >/dev/null
# `limit` rather than `allow` would also rate-limit the operator's own
# reconnections during an incident; fail2ban below does that job with a
# memory of who is knocking.

ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
# The only two ports the product needs. 443/udp is HTTP/3, which Caddy
# serves; without it browsers silently fall back to TCP and nobody notices
# the feature is off.
ufw allow 80/tcp comment "http (redirects to https)" >/dev/null
ufw allow 443/tcp comment "https" >/dev/null
ufw allow 443/udp comment "http/3" >/dev/null

ufw --force enable >/dev/null
echo "    default deny in, allow out; open: ${SSH_PORT}/tcp, 80/tcp, 443/tcp+udp"

# --------------------------------------------------------------------------
say "Docker and the firewall"

# THE trap, and the reason this section exists.
#
# Docker does not ask ufw for permission. It writes its own rules into the
# nat and filter tables, and published ports are DNAT'd before ufw's chains
# are consulted - so `ufw deny 5432` is obeyed by everything on the machine
# EXCEPT a container that published 5432. People discover this when their
# "firewalled" database turns out to be on the internet.
#
# Docker leaves one hook for exactly this: the DOCKER-USER chain, consulted
# first for container traffic. Sending it through ufw's forward chain puts
# containers back under the same rules as everything else. The approach is
# the one chaifeng/ufw-docker documents; the rules are written out here
# rather than fetched, because a firewall is not something to curl into a
# shell.
#
# KnowPilot does not actually need rescuing: docker-compose.prod.yml
# publishes only Caddy, and binds Prometheus and Grafana to 127.0.0.1. This
# is the second lock - for the day somebody adds `ports:` in a hurry.
if ! grep -q "${DOCKER_RULES_MARKER}" /etc/ufw/after.rules; then
    cat >> /etc/ufw/after.rules <<'RULES'

# BEGIN KNOWPILOT DOCKER RULES
# Container traffic is filtered by ufw's own rules instead of bypassing them.
*filter
:ufw-user-forward - [0:0]
:ufw-docker-logging-deny - [0:0]
:DOCKER-USER - [0:0]

# Established connections keep working, both directions.
-A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN

# Traffic between containers and from the private networks is Docker's own
# business; only what arrives from outside is judged here.
-A DOCKER-USER -s 10.0.0.0/8 -j RETURN
-A DOCKER-USER -s 172.16.0.0/12 -j RETURN
-A DOCKER-USER -s 192.168.0.0/16 -j RETURN

# Everything else asks ufw, exactly as a process on the host would.
-A DOCKER-USER -j ufw-user-forward
-A DOCKER-USER -j ufw-docker-logging-deny
-A DOCKER-USER -j RETURN

-A ufw-docker-logging-deny -m limit --limit 3/min --limit-burst 10 -j LOG --log-prefix "[UFW DOCKER BLOCK] "
-A ufw-docker-logging-deny -j DROP
COMMIT
# END KNOWPILOT DOCKER RULES
RULES
    echo "    DOCKER-USER now goes through ufw"
    ufw reload >/dev/null
else
    echo "    already in /etc/ufw/after.rules"
fi

# --------------------------------------------------------------------------
say "SSH"

if ! command -v sshd >/dev/null 2>&1; then
    # Nothing to harden, and no reason to abandon the firewall and the
    # upgrades that already applied.
    echo "    sshd is not installed on this machine; skipping."
else

# Refusing passwords on a machine with no key installed locks everybody out,
# permanently, on a cloud instance with no console. Check first.
keys_found=0
for home in /root /home/*; do
    [[ -s "${home}/.ssh/authorized_keys" ]] && keys_found=1
done
if [[ "${keys_found}" -eq 0 ]]; then
    echo "    NO authorized_keys anywhere. Refusing to disable passwords -" >&2
    echo "    that would lock this machine. Install your key, then re-run." >&2
    exit 1
fi

# A fresh install has the directory; a minimal image does not, and a
# redirection into a missing directory fails after the checks have passed.
mkdir -p "$(dirname "${SSH_DROPIN}")"

cat > "${SSH_DROPIN}" <<CONFIG
# Written by infra/server/harden.sh. A drop-in rather than an edit of
# sshd_config: a package upgrade replaces that file and would quietly restore
# its defaults.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
X11Forwarding no
MaxAuthTries 3
ClientAliveInterval 300
ClientAliveCountMax 2
CONFIG

# Validate BEFORE reloading. A syntax error applied to a running sshd is how
# a server becomes unreachable.
if ! sshd -t; then
    rm -f "${SSH_DROPIN}"
    echo "    sshd rejected the configuration; it was removed, nothing changed." >&2
    exit 1
fi
systemctl reload ssh 2>/dev/null || systemctl reload sshd
echo "    keys only, no root login, validated before reload"

fi

# --------------------------------------------------------------------------
say "fail2ban"

# A public SSH port is scanned continuously. fail2ban reads the log and bans
# the addresses that keep failing - which is what `ufw limit` cannot do,
# because it counts connections without knowing whether they succeeded.
cat > /etc/fail2ban/jail.d/knowpilot.conf <<CONFIG
[sshd]
enabled = true
port = ${SSH_PORT}
maxretry = 5
findtime = 10m
bantime = 1h
# Ubuntu 24.04 logs sshd to the journal, not to /var/log/auth.log.
backend = systemd
CONFIG
systemctl enable --now fail2ban >/dev/null
systemctl restart fail2ban
echo "    sshd jail: 5 failures in 10 minutes, banned for an hour"

# --------------------------------------------------------------------------
say "Unattended upgrades"

# Security updates only. Automatic reboots are deliberately NOT enabled: the
# stack takes minutes to come back (the model loads at start-up), and that
# should happen when somebody is watching.
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONFIG'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONFIG
systemctl enable --now unattended-upgrades >/dev/null
echo "    security updates applied automatically, reboots left to a human"

# --------------------------------------------------------------------------
say "State"
ufw status verbose | head -12
echo
fail2ban-client status sshd 2>/dev/null | head -5 || true
echo
echo "Done. Nothing here replaces the cloud provider's own firewall:"
echo "on Oracle, the VCN security list still has to allow 80, 443 and SSH."
