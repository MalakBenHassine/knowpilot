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

## Running the services

```bash
cp .env.example .env          # then replace every value (openssl rand -base64 24)
docker compose up -d
docker compose ps             # all three must be "healthy"
```

PostgreSQL (application database + Keycloak's own, isolated, database), Redis
(BFF sessions) and Keycloak. Only Keycloak and PostgreSQL publish a port, bound
to `127.0.0.1` so nothing is reachable from the local network. Keycloak's admin
console: <http://localhost:8080>.

`docker compose down` keeps the data, `docker compose down -v` deletes it.

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

The API contract lives in [docs/api/contract.md](docs/api/contract.md), and the
reasoning behind the main technical choices in
[docs/adr/](docs/adr/README.md).

## Evaluating the assistant

```bash
cd backend
uv run python -m evals.run     # real embeddings, real pgvector, real GroqCloud
```

The test suite proves the code behaves; it says nothing about whether the
ASSISTANT behaves, because it runs against a fake model. This measures the
thing the tests cannot: nine questions over documents written for the
purpose, checking that it answers what it can, refuses what it cannot,
ignores an instruction planted inside a document, and never sees another
account.

It is an evaluation, not a test. A failure here is a conversation, not a
broken build: the quality it measures can move without a line of code
changing - a new model version, a tuned threshold, a differently worded
document. For the same reason it does not run in CI: it costs about six
questions of a daily budget of eighty-five, and it needs a real API key.

It creates its own documents and deletes them afterwards, including after a
failure.

## Running the ingestion worker

```bash
cd backend
uv run arq app.worker.WorkerSettings
```

A **second process**, required for uploads to be indexed (ADR-0013). Without
it the API still accepts files: they are stored, the job waits in Redis, and
the documents stay `processing` until a worker starts and drains the queue.
That is the point of a queue - the work is written down, not lost.

Parsing and embedding are CPU-bound, and they used to run inside the API
process, where a thirty-page PDF held the event loop away from everyone
asking a question. They now run here, where the only thing they compete with
is another ingestion. In production this becomes a second container sharing
the database, Redis and the uploads volume; it is deliberately the same
command either way.

It loads the embedding model at startup (~20 s, 2.2 GB), so give it a moment
before the first upload.

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
