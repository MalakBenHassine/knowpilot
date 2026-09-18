# ADR-0009: No state management library for now

- **Status:** Accepted
- **Date:** 2026-09-17

## Context

The frontend holds three kinds of state: the session (global, rarely changes),
the document list (server state, refreshed while indexing runs) and local UI
state (form fields, open panels). The temptation on a portfolio project is to
add Redux or TanStack Query because they are well known.

## Options considered

| Option | Pros | Cons |
| ------ | ---- | ---- |
| **React state + two contexts** | No dependency; explicit; easy to test | Caching, retries and invalidation are written by hand if we ever need them |
| TanStack Query | Caching, deduplication, background refetch, retries | A concept to learn and configure for two endpoints |
| Redux Toolkit | Predictable global store; devtools | Heavy for state that is almost entirely server data; boilerplate |

## Decision

Two React contexts — one for the session, one for the document list, shared
because two screens need it — plus local state everywhere else. Polling runs
only while at least one document is being indexed.

## Consequences

- Nothing to learn or configure, and the data flow is readable end to end.
- If several screens later need the same server data with caching and
  invalidation, we will be writing a small query library by hand, which is the
  signal to stop and adopt one.
- Derived values (for example "can a question be asked?") are computed during
  render, never stored, so they cannot go stale.

## Revisit when

A third consumer needs the same server data, or we start writing cache
invalidation, request deduplication or retry logic ourselves. That is the moment
for TanStack Query — adopted because we met the problem it solves.
