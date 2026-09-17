# KnowPilot

> A private, multi-user AI assistant that answers questions from your own documents, with source citations.

**Status:** 🚧 Early development. The application is not usable yet.

## Planned features

- Upload PDF and text documents to a private space
- Ask questions in natural language
- Get answers grounded in your documents, with citations to the exact source
- Get an explicit "not found" answer when your documents do not contain the information

## Planned tech stack

| Layer          | Technology                                           |
| -------------- | ---------------------------------------------------- |
| Frontend       | React, TypeScript, Vite, Tailwind CSS                |
| Backend        | FastAPI (Python 3.12)                                |
| Authentication | Keycloak (OpenID Connect), Backend-for-Frontend      |
| RAG            | BGE-M3 embeddings, Chroma, Groq LLM                  |
| Data           | PostgreSQL, Redis                                    |
| DevSecOps      | GitHub Actions, SonarCloud, Snyk, Trivy, gitleaks    |
| Infrastructure | Docker Compose, Kubernetes (k3s), Nginx, Let's Encrypt |

## Running the backend

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

- `GET /api/health/live` — liveness: is the process alive? Checks nothing else.
- `GET /api/health/ready` — readiness: are the dependencies reachable? Returns
  `503` when one is not, so an orchestrator stops sending traffic instead of
  restarting the container.
- Interactive docs at `/api/docs`, disabled when `KP_ENVIRONMENT=production`.

The API contract lives in [docs/api/contract.md](docs/api/contract.md).

## Running the frontend

```bash
cd frontend
npm install
npm run dev     # http://localhost:5173
```

The API does not exist yet, so the UI runs against an **in-memory mock backend**
that implements the documented contract (`VITE_API_MODE=mock`, the default).
Switching to the real API is a single environment variable — no component or
hook knows the difference.

The interface models the four outcomes of a question explicitly: **loading**
(retrieving, then generating), **answered with sources**, **insufficient
evidence**, and **error**. "Insufficient evidence" is a valid answer, not a
failure, and is styled accordingly.
