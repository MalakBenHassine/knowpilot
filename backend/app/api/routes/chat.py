"""The chat endpoint: the only place where every piece meets.

Read it as seven numbered steps. The order is not a matter of taste - each step
is where it is because of what it would cost somewhere else, and the comments
say which cost.

The owner is read from the session and from nowhere else. `ChatRequest` has a
single field, so there is no other owner in scope to take by mistake.
"""

import logging

import anyio
from fastapi import APIRouter, HTTPException, status

from app.api.deps import Config, CsrfProtected, Db, Llm, Model, Quota
from app.core.quota import QuotaExceededError
from app.db import vector_store
from app.rag.embeddings import EmbeddingError, EmbeddingModel, embed_query
from app.rag.generation import Passage, answer_question
from app.rag.groq import LanguageModelError, QuotaExhaustedError
from app.schemas.chat import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", summary="Ask a question about your own documents")
async def ask(
    payload: ChatRequest,
    # CSRF rather than the plain session: this is a POST that spends a budget
    # and money. A third-party page must not be able to trigger it with the
    # cookie the browser sends on its own.
    session: CsrfProtected,
    database: Db,
    embeddings: Model,
    model: Llm,
    quota: Quota,
    config: Config,
) -> ChatResponse:
    # 1. The owner. `sub` is the Keycloak subject: stable even when the user
    #    changes their email, and it is the only identity we ever trust,
    #    because it came out of a cookie we signed ourselves.
    owner_id = session.sub

    # 2. Embed the question with the very model that indexed the chunks. A
    #    different model would produce a vector in a different space, and the
    #    distances would be meaningless rather than merely wrong.
    #
    #    `embed_query` is CPU-bound and synchronous. FastAPI runs a `def`
    #    dependency in a worker thread, but this handler is `async`, so the
    #    call has to be pushed off the event loop explicitly - otherwise one
    #    question freezes every other request, the health probe included.
    try:
        vector = await _embed(payload.question, embeddings)
    except EmbeddingError:
        logger.exception("failed to embed a question")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Answering is unavailable"
        ) from None

    # 3. Retrieve BEFORE reserving the budget. Nothing is spent at the provider
    #    when there is nothing to send, so charging the user here would bill
    #    them for a request that never leaves the building.
    found = await vector_store.search_chunks(
        database,
        owner_id=owner_id,
        query_vector=vector,
        limit=config.top_k,
        # A library always has a least-bad match. Without a ceiling, a question
        # about cooking would be answered from a passage about Kubernetes, with
        # a citation, and the citation would be real.
        max_distance=config.max_distance,
    )

    # 4. Nothing close enough - including the case of a user who has not
    #    uploaded anything yet. This is 200, not an error: "I do not have that
    #    information" is an answer, and the strongest guard of the whole
    #    feature is that the model is never called at all.
    #
    #    The response says nothing about WHY, because the browser already knows
    #    whether this user owns any document: it is showing the list. Letting
    #    it choose the sentence saves a query and keeps the wording where the
    #    context is.
    if not found:
        return ChatResponse(answer="", citations=[], is_grounded=False)

    # 5. Reserve now, because the next step spends tokens that cannot be
    #    reclaimed. Incrementing after a successful answer would let two
    #    simultaneous requests both pass the check; charging for an answer the
    #    provider fails to deliver is undone by the refund below. Of the two
    #    defects, this is the reversible one.
    try:
        await quota.reserve(owner_id=owner_id)
    except QuotaExceededError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "You have used your questions for today"
            if exc.scope == "user"
            else "The service has used its questions for today",
            # Tells the client WHEN to come back. A 429 without it invites an
            # immediate retry, which is refused again.
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc

    passages = [
        Passage(text=chunk.text, filename=chunk.filename, page_number=chunk.page_number)
        for chunk in found
    ]

    # 6. Generate. Every provider failure is already one of our own exception
    #    types, so this handler never has to know that Groq or httpx exist.
    try:
        answer = await answer_question(payload.question, passages, model)
    except QuotaExhaustedError as exc:
        # Their budget, not ours: our counter was wrong about the real one, so
        # the user keeps the question they could not use.
        await quota.refund(owner_id=owner_id)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "The service has used its questions for today",
            headers={"Retry-After": str(int(exc.retry_after))},
        ) from exc
    except LanguageModelError as exc:
        # An outage of ours must not cost the user part of their day.
        await quota.refund(owner_id=owner_id)
        logger.warning("generation failed: %s", type(exc).__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Answering is unavailable"
        ) from exc

    # 7. A refusal still costs a call, so it is not refunded: the tokens were
    #    spent, and the honest answer is the product working, not failing.
    return ChatResponse.of(answer, found)


async def _embed(question: str, embeddings: EmbeddingModel) -> list[float]:
    """Run the CPU-bound encoder off the event loop.

    Encoding one short question takes tens of milliseconds, which sounds
    harmless until you remember that during those milliseconds the event loop
    runs nothing else: not another question, not an upload, not the readiness
    probe the orchestrator uses to decide whether this container is alive.

    The annotation is `EmbeddingModel`, not the `Model` alias: that alias
    carries a Depends marker and only means something to FastAPI, on a route.
    """
    return await anyio.to_thread.run_sync(embed_query, question, embeddings)
