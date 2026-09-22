# API contract

The frontend and the backend are two systems that communicate through this
contract. It is written **before** the code, and any change to it is a change to
both sides.

## Conventions

| Rule | Value | Why |
| ---- | ----- | --- |
| Base path | `/api` | Same origin as the SPA behind the reverse proxy: no CORS, session cookie works |
| Field names | `snake_case` | Natural for FastAPI/Pydantic. The frontend maps to `camelCase` in `services/`, which is the single place the contract is translated |
| Collections | `{ "items": [...] }` | An envelope can gain `total` / `next_cursor` later without breaking clients |
| Identifiers | UUID v4 strings | Sequential ids allow enumeration and leak activity volume |
| Timestamps | ISO 8601, UTC, `Z` suffix | Unambiguous. Time zones are a display concern |
| Enumerations | Closed sets of lowercase strings | The client must never parse free text |
| Errors | `{ "detail": "<generic message>" }` | Technical details go to the logs, never to the client |
| Authentication | `__Host-session` cookie (httpOnly), issued by the BFF | No token is ever exposed to JavaScript |
| Authorisation | Enforced server side on every request | Hiding a control in the UI is not a security measure |

### Status codes used

| Code | Meaning here |
| ---- | ------------ |
| `200` | Success |
| `201` | Resource created (upload accepted) |
| `400` | Malformed request |
| `401` | No valid session |
| `404` | Not found **or not yours** — the two are indistinguishable on purpose |
| `413` | File too large |
| `415` | Unsupported file type |
| `422` | Validation error (FastAPI default) |
| `429` | Rate limit reached |
| `500` | Unexpected server error |
| `503` | Dependency unavailable (readiness) |

> **404 instead of 403 for another user's resource.** A `403` confirms that the
> resource exists, which allows enumeration. The query itself filters by owner,
> so "missing" and "not yours" produce the same result.

---

## Metrics

### `GET /metrics`

Prometheus text format (ADR-0019). Deliberately **outside `/api`**: the
reverse proxy forwards `/api` only, so this answers on the internal network
and never to a browser. No authentication, for that reason - it is scraped
by Prometheus, not called by the client. It exports counts, durations and
token totals; no label ever carries a user, a question or a document id.

## Health

### `GET /api/health/live`

Liveness. Answers one question: *is the process alive?* It checks **nothing
external**, so a failing database never triggers a pointless restart.

- Authentication: none (it must work before anything else does)
- `200` → `{ "status": "ok" }`

### `GET /api/health/ready`

Readiness. Answers: *can this instance serve traffic?* Checks every dependency
with a short timeout.

- Authentication: none
- `200` → `{ "status": "ready", "checks": { "database": "ok", "cache": "ok", "vector_store": "ok" } }`
- `503` → `{ "status": "not_ready", "checks": { "database": "ok", "cache": "error", "vector_store": "ok" } }`

Each check is `"ok"` or `"error"` — **never** a version, a hostname, a
connection string or an exception message. Those would map the internal
infrastructure for an attacker. Causes belong in the logs.

Responses must not be cached: a cached health state is a lie.

---

## Authentication

The backend is the OIDC client (Backend-for-Frontend, ADR-0004). `login` and
`callback` are reached by **browser redirects**, not by `fetch`: the user must
see Keycloak's page and the application never handles a password.

| Endpoint | Called by | Result |
| -------- | --------- | ------ |
| `GET /api/auth/login` | Browser redirect | `303` to Keycloak, with `state`, `nonce` and a PKCE challenge; sets a five-minute `__Host-oidc-tx` cookie |
| `GET /api/auth/register` | Browser redirect | Same flow, but Keycloak shows its registration form first. The application contains no sign-up code |
| `GET /api/auth/callback` | Keycloak redirect | Verifies `state`, exchanges the code server side, validates the id token (issuer, audience, expiry, nonce), creates the session, sets `__Host-session`, then `303` to `/documents`. Any failure redirects to `/login?error=auth` |
| `GET /api/auth/me` | `fetch` | `200` with the user, or `401` when there is no session |
| `POST /api/auth/logout` | `fetch` | Requires the CSRF header. Deletes the session and returns the URL that ends the Keycloak session |

```json
// GET /api/auth/me
{
  "id": "8f2c1e40-…",          // the `sub` claim: stable, unlike an email
  "email": "malak@knowpilot.dev",
  "display_name": "Malak Ben Hassine",
  "csrf_token": "…"            // sent back as X-CSRF-Token on writes
}
```

The session cookie holds a **random identifier only**. Tokens live in Redis with
two independent lifetimes: a sliding idle timeout and a hard absolute limit.

**CSRF:** every request that changes state (`POST`, `PUT`, `PATCH`, `DELETE`)
must carry `X-CSRF-Token`. Safe methods are exempt, which is only sound because
a `GET` never changes state.

## Documents

### `GET /api/documents`

Lists the documents of the **current user**, newest first.

- Authentication: required
- `401` when there is no session
- `200` with an **empty** `items` array when the user has no documents — an
  empty list is a normal result, not a `404`

```json
{
  "items": [
    {
      "id": "0b7d3f3e-1c5a-4a9e-9f2e-3a7c1d2e5f80",
      "filename": "contract.pdf",
      "mime_type": "application/pdf",
      "size_bytes": 2411724,
      "status": "ready",
      "stage": null,
      "page_count": 12,
      "chunk_count": 24,
      "failure_reason": null,
      "retryable": false,
      "created_at": "2026-09-17T18:12:04Z"
    }
  ]
}
```

| Field | Type | Notes |
| ----- | ---- | ----- |
| `id` | UUID | |
| `filename` | string | Original name, shown to the user. The stored file uses a generated name |
| `mime_type` | string | Detected server side from the content, not from the extension |
| `size_bytes` | integer | |
| `status` | `uploading` \| `processing` \| `ready` \| `failed` | |
| `stage` | `parsing` \| `chunking` \| `embedding` \| `indexing` \| `null` | Real pipeline step, only while `processing`. **No invented percentage** |
| `page_count` | integer \| null | Known once parsed; null for formats without pages |
| `chunk_count` | integer \| null | Number of searchable passages |
| `failure_reason` | `no_text_found` \| `unsupported_format` \| `too_large` \| `processing_error` \| `null` | A **code**, not a message: the wording belongs to the frontend |
| `retryable` | boolean | Only the backend knows whether retrying can succeed. A scanned PDF never will; a timeout might |
| `created_at` | timestamp | |

**Never returned:** `owner_id`, storage path, content hash, internal error text.
The response is built from an explicit schema, never from the database model.

### `POST /api/documents`

Uploads one file as `multipart/form-data` under the field `file`, and returns
**immediately**. Indexing runs in the background: embedding a long document
takes about a minute, and a request held open that long is killed by every
proxy between the browser and here.

- Authentication: required · CSRF header: required
- `201` with the document, `status: "processing"`, `stage: "parsing"`

| Code | When | What the client should say |
| ---- | ---- | -------------------------- |
| `400` | The file is empty | "This file is empty" |
| `409` | The same content was already uploaded **by this user** | "Already uploaded" |
| `413` | Over 20 MB | "Too large, 20 MB maximum" |
| `415` | Not a PDF and not UTF-8 text | "Only PDF and text files" |
| `429` | More than `KP_UPLOAD_REQUESTS_PER_MINUTE` uploads and retries this minute; `Retry-After` in seconds | "Too many uploads, try again in ..." |
| `503` | The embedding model is not loaded | "Try again in a moment" |

The type is detected **from the first bytes of the content**, never from the
extension or from the declared `Content-Type`: both are chosen by whoever
uploads. Deduplication is per user and based on a SHA-256 of the content, so
renaming a file does not make it new — and a hash belonging to another account
is never consulted, which would turn it into an oracle.

The client polls `GET /api/documents` while any document is `processing`, and
follows the real `stage`.

### `DELETE /api/documents/{id}`

- Authentication: required · CSRF header: required
- `204` with an empty body
- `404` when it does not exist **or is not yours** — indistinguishable

Deletes the document, its passages (by cascade, in the same transaction) and
its bytes. The file is removed after the response, so a rolled-back deletion
never leaves a row pointing at bytes that are gone.

### `POST /api/documents/{id}/retry`

- Authentication: required · CSRF header: required
- `202` with the document back in `processing`
- `404` when it does not exist or is not yours
- `409` when the document is not `failed`, or failed with `retryable: false`
- `429` with `Retry-After`: shares the per-minute upload limit

Only the backend decides whether a second attempt can succeed. A scanned page
will never become readable; a full disk might have been emptied.

### `POST /api/chat`

- Authentication: required - CSRF header: required
- Body: `{ "question": string }` and nothing else
- `200` with `{ answer, citations, is_grounded, not_in_documents }`
- `422` when the question is blank or longer than 1000 characters
- `429` with a `Retry-After` header in seconds, when YOUR budget (or the
  service daily budget) is spent, or after more than
  `KP_CHAT_REQUESTS_PER_MINUTE` requests this minute - counted before any
  retrieval, and shared with `/api/chat/stream`
- `503` with a `Retry-After` header when the language model provider is
  saturated (its per-minute or daily limit): the question is refunded, and
  the client says "busy", never "you have used your questions"
- `503` without `Retry-After` when answering is disabled or the provider failed

The body carries a question and only a question. There is no owner field,
no document filter and no model name: the owner comes from the session
cookie, so the wrong value does not exist in the handler for anyone to
take by mistake.

`is_grounded: false` is a SUCCESS, not an error. It means either that no
passage of this user was close enough to the question, or that the model
declined to answer from what it was given. `answer` is then empty and
`citations` is empty, and the interface says so without an error style -
the browser already knows whether the user owns any document, so it picks
the right sentence.

Each citation is `{ number, document_id, filename, page_number, snippet }`.
`number` is what the model wrote between brackets; everything else was
attached by the server afterwards. The model is shown the passages as
`[1]` to `[n]` and never sees a filename or a page, so a citation it could
not have produced is not merely detected - it is inexpressible.

`not_in_documents` lists what the question asks about and no passage names
(ADR-0018): `["rétroviseur"]` when the contract only mentions glass
breakage. The answer may still be right - a broader category may cover the
thing - so it is given, and the interface shows the list above it. It is a
fact about words, checked by the database, never the model's opinion.
Always empty when `is_grounded` is false; empty on almost every answer.

A question is charged against two daily budgets, per user and per service.
It is refunded when the provider was unreachable, timed out or refused us,
because an outage must not cost a user part of their day. A grounded
refusal is NOT refunded: the call happened and the tokens were spent.

Nothing is charged when retrieval finds nothing, because nothing is sent.

### `POST /api/chat/stream`

The same question, the same rules, the same answer - delivered as
Server-Sent Events (`Content-Type: text/event-stream`) while it is generated.

- Authentication: required - CSRF header: required
- Body: `{ "question": string }`, exactly as `POST /api/chat`
- `401`, `422`, `429` (with `Retry-After`) and `503` are returned as status
  codes, BEFORE the stream opens: authentication, validation, retrieval and
  the quota are all settled first
- `200` with a stream of events:

| Event | Data | Meaning |
| --- | --- | --- |
| `stage` | `{"stage": "generating"}` | retrieval is over, the model is called |
| `token` | `{"text": "..."}` | verified text to APPEND to what is shown |
| `done` | same body as `POST /api/chat` | the authoritative answer; REPLACES what was shown |
| `error` | `{"kind": "server"}` or `{"kind": "busy", "retry_after": 120}` | sent instead of `done` when the provider fails or is saturated mid-way |

Nothing the guards would reject is ever sent. The server holds the text back
until the first valid citation `[n]` has been generated: from that point the
answer can no longer be judged "unsourced". An answer with no valid citation,
and the refusal word, are never streamed at all. A short answer whose only
citation is at its end therefore arrives in one piece.

If the refusal word appears AFTER a citation, generation is stopped and
`done` carries `is_grounded: false`: the client replaces the text it showed.

`EventSource` cannot send a POST body or the CSRF header, so the client reads
the stream with `fetch`. A client that disconnects mid-answer closes the
provider's stream too, and is not refunded - the tokens were spent.
