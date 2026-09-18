# ADR-0004: Backend-for-Frontend — tokens never reach the browser

- **Status:** Accepted
- **Date:** 2026-09-17

## Context

With Keycloak chosen (ADR-0003), the SPA needs an authenticated session. The
data at stake is a user's private documents. The IETF draft *OAuth 2.0 for
Browser-Based Applications* recommends the BFF pattern for sensitive
applications.

## Options considered

| Option | Pros | Cons |
| ------ | ---- | ---- |
| SPA as a public OIDC client | Simpler; a well-known library does most of the work | Access and refresh tokens live in the browser; an XSS lets an attacker steal a token and reuse it outside the victim's browser |
| **BFF: FastAPI as a confidential client** | No token in JavaScript; revocation is server side; the SPA gets a simple session cookie; the frontend needs no OIDC library | CSRF protection required; server-side session storage needed; more backend code |

## Decision

FastAPI performs the Authorization Code + PKCE exchange, stores the tokens
server side in Redis, and returns a `__Host-` session cookie that is `HttpOnly`,
`Secure` and `SameSite=Lax`. The cookie contains a random identifier only.

## Consequences

- An XSS can still act **as the user while the page is open**, but cannot steal
  anything reusable. Preventing XSS remains mandatory: markdown is rendered
  without raw HTML, and a CSP is applied.
- CSRF protection is required, because the browser attaches the cookie
  automatically: `SameSite` plus a CSRF token on state-changing requests.
- Redis becomes a dependency — the same instance later serves rate limiting, so
  it earns its place twice.
- The SPA and the API must share one origin, served by a reverse proxy. Welcome
  side effect: no CORS configuration, and the `__Host-` prefix becomes usable.
- The API stays stateless per request, so it can scale horizontally: sessions
  live in Redis, not in process memory.

## Revisit when

A non-browser client (mobile app, CLI, third-party integration) needs access,
which would call for token-based authentication alongside the session.
