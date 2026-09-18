# Architecture Decision Records

Each file records **one** decision that is expensive to reverse, and the
reasoning behind it — including the options that were rejected.

An ADR is never edited after it is accepted. When a decision changes, a new ADR
supersedes the old one, and the old one is marked accordingly. The history of
decisions is as useful as the decisions themselves.

| # | Decision | Status |
| - | -------- | ------ |
| [0001](0001-react-typescript-vite.md) | React + TypeScript + Vite for the frontend | Accepted |
| [0002](0002-monorepo.md) | Single repository for frontend, backend and infrastructure | Accepted |
| [0003](0003-keycloak-for-authentication.md) | Keycloak (OIDC) for authentication | Accepted |
| [0004](0004-bff-session-cookie.md) | Backend-for-Frontend: tokens stay server side | Accepted |
| [0005](0005-wsl2-development-environment.md) | Develop inside WSL2 Ubuntu | Accepted |
| [0006](0006-local-embeddings.md) | Local BGE-M3 embeddings instead of a hosted API | Accepted |
| [0007](0007-tailwind-and-own-design-system.md) | Tailwind CSS with our own design system | Accepted |
| [0008](0008-contract-first-with-a-mock-backend.md) | Contract-first UI against a mock backend | Accepted |
| [0009](0009-no-state-management-library.md) | No state management library for now | Accepted |

## Template

```markdown
# ADR-XXXX: <decision in one sentence>

- **Status:** Proposed | Accepted | Superseded by ADR-YYYY
- **Date:** YYYY-MM-DD

## Context
The situation and the constraints. No solution yet.

## Options considered
| Option | Pros | Cons |

## Decision
What we chose.

## Consequences
What this buys us, and what it costs us.

## Revisit when
The concrete signal that should make us reopen this decision.
```
