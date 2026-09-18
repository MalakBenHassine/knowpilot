# ADR-0003: Keycloak (OIDC) for authentication

- **Status:** Accepted
- **Date:** 2026-09-17

## Context

KnowPilot is multi-user from v1: every user has a private document space, so
identity is a prerequisite for everything else. Authentication is the part of an
application where a mistake is most expensive: password storage, brute-force
protection, password reset, email verification, MFA. The project must stay free
to run.

## Options considered

| Option | Pros | Cons |
| ------ | ---- | ---- |
| Home-made authentication in FastAPI | Full control; teaches the mechanics | We would implement password hashing, lockout, reset flows and MFA ourselves — the highest-risk code in the project |
| **Keycloak (self-hosted)** | Open standard (OIDC/OAuth2); battle-tested implementation of the risky parts; widely used in enterprises; free | One more service to run, secure and upgrade; extra memory; realm configuration to manage |
| Managed provider (Auth0, Clerk) | Least operational effort | Vendor lock-in; paid beyond a free tier; teaches integration rather than protocols |

## Decision

Keycloak, self-hosted, with the Authorization Code flow **and PKCE**. Direct
access grants (password grant) stay disabled.

## Consequences

- FastAPI never sees or stores a password.
- Users are identified by the `sub` claim, never by email, which can change.
- The realm configuration is exported to `infra/keycloak/` and versioned,
  **without secrets**, so the environment is reproducible.
- Operational cost: Keycloak needs its own database, HTTPS in production, an
  admin console that must never be publicly exposed, and regular updates.
- In production it runs with `start --optimized`, never `start-dev`.

## Revisit when

The operational burden outweighs the benefit — for example if the project moves
to a managed platform, or if a single-tenant deployment makes a simpler,
well-reviewed library sufficient.
