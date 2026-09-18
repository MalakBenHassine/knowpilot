# ADR-0002: Single repository for frontend, backend and infrastructure

- **Status:** Accepted
- **Date:** 2026-09-17

## Context

The project is built by one person and consists of a React SPA, a FastAPI
service, infrastructure files (Compose, Keycloak realm, reverse proxy) and
documentation. Frontend and backend evolve together through a shared API
contract.

## Options considered

| Option | Pros | Cons |
| ------ | ---- | ---- |
| **Monorepo** | A contract change is one atomic commit; one CI configuration; one security setup; one link for a reviewer | CI must filter by folder; no per-service access control |
| Polyrepo | Independent release cycles; team autonomy | A contract change spans several pull requests that must be synchronised; duplicated tooling; harder to review as a whole |

## Decision

One repository, with `frontend/`, `backend/`, `infra/`, `docs/` and `.github/`.

## Consequences

- Adding a field to an API response changes the Pydantic schema, the TypeScript
  type and the component **in the same commit**. The history stays coherent.
- Workflows use `paths:` filters so a CSS change does not run the Python tests.
- Security tooling (gitleaks, Trivy, Dependabot) is configured once.
- The advantages of a polyrepo — independent deployments, separate permissions —
  do not apply to a single author with a single release cycle.

## Revisit when

Several teams own different services, or one service needs its own release
cadence and access restrictions.
