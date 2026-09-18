# ADR-0001: React + TypeScript + Vite for the frontend

- **Status:** Accepted
- **Date:** 2026-09-17

## Context

KnowPilot needs a web interface for a private, multi-user RAG assistant. All the
domain logic — retrieval, prompts, LLM calls, authorisation — lives in a FastAPI
backend. The interface is served behind a login, so it has no public pages and
no SEO requirement. The author is building this as a portfolio project and wants
skills that appear in job postings.

## Options considered

| Option | Pros | Cons |
| ------ | ---- | ---- |
| **React + TypeScript + Vite** | Most requested combination; SPA fits an existing API; minimal tooling; rich ecosystem for chat and markdown | Routing, data fetching and structure must be chosen by us |
| Next.js | Very popular; batteries included | Brings its own server, which duplicates FastAPI; SSR and SEO are useless behind a login; more concepts to learn for no benefit here |
| Angular | Strong in large enterprises | Heavier learning curve; smaller ecosystem for this kind of product |
| Vue 3 | Simple and pleasant | Less demanded; smaller AI-related ecosystem |

## Decision

React with TypeScript, built by Vite, as a single-page application consuming the
FastAPI backend over HTTP.

## Consequences

- The frontend stays thin: it renders state and calls an API.
- No server-side rendering. Acceptable: there is nothing to index.
- We own the structure (pages, components, hooks, services), which is the point:
  those decisions are what makes the project worth discussing.
- If public marketing pages are ever needed, they would be a separate concern,
  and Next.js would be reconsidered for them alone.

## Revisit when

The product needs indexable public pages, or server-side rendering for
first-paint performance on low-end devices.
