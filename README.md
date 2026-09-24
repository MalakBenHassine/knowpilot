# KnowPilot

> A private, multi-user AI assistant that answers questions from your own documents, with source citations.

**Status:** feature-complete v1, deployable to a single VM (docs/deploy.md).

## Features

- Upload PDF and text documents to a private space (text-layer PDFs: OCR for scans
  is designed in ADR-0010 but not wired yet)
- Ask questions in natural language, and read the answer as it is generated
- Every statement cites the passage it comes from, and each source card quotes it
- An explicit "not found" when the documents do not contain the answer - and a
  notice when an answer rests on something the documents never name
- Accounts through Keycloak; every document, passage and question is scoped to
  its owner, down to the SQL
- Daily question budgets and per-minute limits
- Prometheus metrics from both processes, a Grafana dashboard kept in the
  repository, and alert rules that say what to do

## Tech stack

| Layer          | Technology                                           |
| -------------- | ---------------------------------------------------- |
| Frontend       | React, TypeScript, Vite, Tailwind CSS                |
| Backend        | FastAPI (Python 3.12)                                |
| Authentication | Keycloak (OpenID Connect), Backend-for-Frontend      |
| RAG            | LangChain, BGE-M3 embeddings, pgvector, Groq LLM     |
| Data           | PostgreSQL, Redis                                    |
| DevSecOps      | GitHub Actions, SonarQube Cloud, CodeQL, Trivy, gitleaks, Dependabot |
| Infrastructure | Docker Compose, Caddy, Let's Encrypt                 |
| Observability  | Prometheus, Grafana, alert rules                     |

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

## Running the tests

```bash
cd backend
uv run pytest                          # offline: no database, no key, no network
KP_RUN_DB_TESTS=1 uv run pytest        # also the PostgreSQL tests (docker compose up -d postgres)
```

The database tests are the ones that matter most - tenant isolation through
the real PGVectorStore, the keyword search, the context window, the subject
check - so they are opt-in locally and MANDATORY in CI: the `database` job of
the backend workflow starts pgvector, applies the migrations, checks that
every migration can be rolled back and that the models match them, then runs
the whole suite and fails if a single database test was skipped.

## Evaluating the assistant

```bash
cd backend
uv run python -m evals.run     # real embeddings, real pgvector, real GroqCloud
```

The test suite proves the code behaves; it says nothing about whether the
ASSISTANT behaves, because it runs against a fake model. This measures the
thing the tests cannot: twenty-one questions over documents written for the
purpose, checking that it answers what it can, refuses what it cannot,
ignores an instruction planted inside a document, and never sees another
account.

It is an evaluation, not a test. A failure here is a conversation, not a
broken build: the quality it measures can move without a line of code
changing - a new model version, a tuned threshold, a differently worded
document. For the same reason it does not run in CI: it costs about nineteen
questions of a daily budget of eighty-five, and it needs a real API key.

```bash
uv run python -m evals.run a-value-read-with-its-date   # one case, to check one fix cheaply
```

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

By default the UI runs against an **in-memory mock backend** that implements
the documented contract (`VITE_API_MODE=mock`), so the interface can be worked
on without the backend. `VITE_API_MODE=http` talks to the real API through the
Vite proxy - no component or hook knows the difference.

The interface models the four outcomes of a question explicitly: **loading**
(retrieving, then generating), **answered with sources**, **insufficient
evidence**, and **error**. "Insufficient evidence" is a valid answer, not a
failure, and is styled accordingly.

## Quality and security checks

Every pull request runs these, and each one blocks the merge:

| Check | Tool | A failure means |
| --- | --- | --- |
| Lint, format, types | ruff, mypy, ESLint, tsc | the code does not match the rules the rest of the code follows |
| Tests | pytest offline, pytest against a real pgvector, vitest | behaviour changed |
| Quality gate | SonarQube Cloud | new code under 80% coverage, a bug, a smell, or duplication |
| SAST | CodeQL, `security-extended` | a value reaches a dangerous sink - injection, path traversal |
| Dependencies | Trivy (lock files), Dependabot | a known HIGH or CRITICAL vulnerability that has a fix |
| Configuration | Trivy (Compose, Dockerfiles) | an insecure setting in the infrastructure files |
| Secrets | gitleaks, over the whole history | a credential in any commit, including one later removed |

Sonar and CodeQL are not redundant: Sonar grades the code, CodeQL follows
data through it. The first finds what is badly written, the second finds what
is exploitable.

Snyk is deliberately absent. Trivy already reads the same lock files and
Dependabot already opens the upgrade pull requests; a third scanner over the
same dependencies produces duplicate findings, not more safety.

And on `main`, before an image can be released:

| Check | Tool | A failure means |
| --- | --- | --- |
| Image vulnerabilities | Trivy, on each architecture's digest | a HIGH or CRITICAL with a fix reached an image |
| Signature | cosign, keyless, verified in the same job | the release cannot prove what it built |

The images are pushed **by digest, without a tag**: nothing can pull them
until the scan passes and the tags are written. The tag is the gate, not the
push. What the gate accepts anyway lives in
[.trivyignore.yaml](.trivyignore.yaml) - each entry names a CVE, says why this
deployment can carry it, and **expires**, so it comes back for review instead
of being forgotten.

## Watching it

```bash
docker compose -f docker-compose.prod.yml up -d prometheus grafana
```

Two scrape targets, because KnowPilot is two processes that fail
independently: the API serves its own `/metrics`, and the worker opens a
second exporter. **That second one is the point** - the API can answer every
request perfectly while the worker is dead and every upload rots in
`processing` behind a spinner nobody can stop. No HTTP metric of the API
would ever say so.

The data source, the dashboard and the seven alert rules are **files**
([infra/grafana/](infra/grafana/), [infra/prometheus/](infra/prometheus/)),
mounted read-only: a Grafana that loses its volume comes back identical, and
what it shows is reviewable in a pull request. The dashboard JSON is
generated by [infra/grafana/dashboard.py](infra/grafana/dashboard.py) rather
than exported from the interface.

Neither container is reachable from the internet; both publish on
`127.0.0.1`, so reading a dashboard means an SSH tunnel
([docs/deploy.md](docs/deploy.md), section 7). The reasoning, including why
they sit on their own network and why there is no Alertmanager yet, is in
[ADR-0021](docs/adr/0021-monitoring-stack.md).

## Deploying

One VM, Docker Compose, Caddy for HTTPS: [docs/deploy.md](docs/deploy.md) is
the full procedure, and [ADR-0020](docs/adr/0020-single-vm-compose-deployment.md)
the reasoning. Images are built for x86 and ARM by the Release workflow, which
runs on a **version tag** and not on every commit: publishing is a decision,
not a side effect of merging. They are pulled by commit sha, so production
runs exactly what CI built - and their signature says so.

The same system also exists as Kubernetes objects, in [k8s/](k8s/): one
Kustomize base, a local overlay, and a script that **proves** the
NetworkPolicies by opening connections from inside the cluster - because a
policy applies cleanly and blocks nothing on a CNI that ignores them.
Production stays on Compose, and
[ADR-0022](docs/adr/0022-kubernetes-manifests.md) says why, including the
one thing Compose expresses that Kubernetes has no equivalent for.
