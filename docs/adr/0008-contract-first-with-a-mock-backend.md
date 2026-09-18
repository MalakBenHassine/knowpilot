# ADR-0008: Contract-first UI against a mock backend

- **Status:** Accepted
- **Date:** 2026-09-17

## Context

The product interface was built before the FastAPI endpoints existed. Building a
UI with no backend risks two failures: inventing capabilities the backend will
never provide, and writing throwaway code that must be rewritten once the real
API lands.

## Options considered

| Option | Pros | Cons |
| ------ | ---- | ---- |
| Wait for the backend | No rework | Nothing to show or test for weeks; API design happens with no consumer feedback |
| UI with hard-coded data | Fast | The data path is never exercised; wiring the API later means rewriting the screens |
| **Written contract + mock implementing it** | The UI exercises the real parsing and mapping code; the contract is discussed before it is implemented | The contract may still be wrong; the mock is code to maintain |

## Decision

Write the contract in `docs/api/contract.md` first. The mock backend answers in
the **wire format** (snake_case, `items` envelope, `null`), so mock mode and
HTTP mode run the same parsing, validation and mapping code. The switch is one
environment variable, `VITE_API_MODE`.

Assumptions the frontend made before the backend existed are listed explicitly
and must be confirmed or removed — this already led to replacing an invented
progress percentage with the real pipeline stage, and to showing "Try again"
only when the backend marks a failure as retryable.

## Consequences

- Switching to the real API exercises no untested code path.
- The contract was reviewed while it was still cheap to change.
- The mock is real code: it must stay aligned with the contract, and it is
  deleted once every endpoint exists.
- Danger to watch: a mock that is too convenient hides real-world failure modes.
  Ours deliberately produces slow indexing, insufficient evidence and errors.

## Revisit when

All endpoints exist. The mock is then removed, and `VITE_API_MODE` with it.
