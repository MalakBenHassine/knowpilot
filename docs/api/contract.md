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

### Not specified yet

`POST /api/documents`, `DELETE /api/documents/{id}`, `POST /api/chat` and the
auth endpoints are designed in their own feature. The frontend currently runs
against a mock implementing this same contract (`VITE_API_MODE=mock`).
