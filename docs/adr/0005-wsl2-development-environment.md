# ADR-0005: Develop inside WSL2 Ubuntu

- **Status:** Accepted
- **Date:** 2026-09-17

## Context

The workstation runs Windows 11 Home. CI runners, the future VM and the
Kubernetes nodes all run Linux. Docker Desktop already uses a WSL2 backend.
Keeping source code on the Windows filesystem makes container volumes slow and
breaks file watching, and Windows tooling introduces CRLF line endings.

## Options considered

| Option | Pros | Cons |
| ------ | ---- | ---- |
| Windows native | Nothing to install | Different OS from CI and production; slow Docker volumes; CRLF issues; DevOps tools are second-class |
| **WSL2 Ubuntu** | Same OS as CI and production; fast volumes; native tooling (`kubectl`, `helm`, `trivy`, bash) | A little Linux to learn; editing must go through VS Code with the WSL extension |
| Local Linux VM | Full isolation | A second virtual machine to run; Windows Home has no Hyper-V manager; more RAM |

## Decision

The repository lives in the Linux filesystem (`~/projects/knowpilot`), edited
through VS Code with the WSL extension. Node, uv, gitleaks and the other tools
are installed inside Ubuntu, in `~/.local`, without `sudo`.

## Consequences

- What passes locally passes in CI: same OS, same shell, same paths.
- Tools installed in the user's home directory follow least privilege: a
  malicious package cannot touch the system.
- Files written from Windows through `\\wsl.localhost` are owned by `root` and
  must have their ownership fixed — a known friction of this setup.
- The Windows copy of the project was deleted to avoid working in the wrong one.

## Revisit when

The workstation changes, or the team grows and needs a documented, shared
development container instead.
