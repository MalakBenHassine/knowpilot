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
| Frontend       | React, TypeScript, Vite                              |
| Backend        | FastAPI (Python 3.12)                                |
| Authentication | Keycloak (OpenID Connect), Backend-for-Frontend      |
| RAG            | BGE-M3 embeddings, Chroma, Groq LLM                  |
| Data           | PostgreSQL, Redis                                    |
| DevSecOps      | GitHub Actions, SonarCloud, Snyk, Trivy, gitleaks    |
| Infrastructure | Docker Compose, Kubernetes (k3s), Nginx, Let's Encrypt |
