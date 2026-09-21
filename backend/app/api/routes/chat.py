"""The chat endpoint: the only place where every piece meets.

Read it as six numbered steps. The order is not a matter of taste - each step
is where it is because of what it would cost somewhere else, and the comments
say which cost.

The LangChain pieces - an owner-scoped retriever and an LCEL answer chain -
are deliberately NOT composed into one `retriever | prompt | model` chain.
Between retrieval and generation sit two decisions a chain cannot express
cleanly: skip the model entirely when nothing was found, and reserve a paid
quota before calling it. Two runnables with the policy in between is the
honest shape of this flow.

The owner is read from the session and from nowhere else. `ChatRequest` has a
single field, so there is no other owner in scope to take by mistake.
"""

import logging

from fastapi import APIRouter, HTTPException, status

from app.api.deps import AnswerChain, Config, CsrfProtected, Quota, Vectors
from app.core.quota import QuotaExceededError
from app.rag.generation import answer_question
from app.rag.llm import LanguageModelError, QuotaExhaustedError
from app.rag.retrieval import OwnerScopedRetriever
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
    vectors: Vectors,
    chain: AnswerChain,
    quota: Quota,
    config: Config,
) -> ChatResponse:
    # 1. The owner. `sub` is the Keycloak subject: stable even when the user
    #    changes their email, and the only identity we trust, because it came
    #    out of a cookie we signed ourselves.
    owner_id = session.sub

    # 2. Retrieve BEFORE reserving the budget. The retriever embeds the
    #    question with the very model that indexed the chunks - in a thread,
    #    so the event loop keeps serving everyone else - and searches this
    #    owner's passages only. It is built per request because the owner is
    #    part of it: there is no shared retriever that could serve the wrong
    #    tenant.
    retriever = OwnerScopedRetriever(
        vector_store=vectors,
        owner_id=owner_id,
        k=config.top_k,
        max_distance=config.max_distance,
    )
    try:
        passages = await retriever.ainvoke(payload.question)
    except Exception:
        # The model or the database failed. The traceback goes to the logs;
        # the user learns only that answering is unavailable right now.
        logger.exception("retrieval failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Answering is unavailable"
        ) from None

    # 3. Nothing close enough - including a user with no document yet. This is
    #    200, not an error: "I do not have that information" is an answer, and
    #    the strongest guard of the feature is that the model is never called.
    #    Nothing was sent to the provider, so nothing is charged.
    if not passages:
        return ChatResponse(answer="", citations=[], is_grounded=False)

    # 4. Reserve now, because the next step spends tokens that cannot be
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

    # 5. Generate through the LCEL chain. Every provider failure already
    #    arrives as one of our own exception types (llm.guarded), so this
    #    handler never has to know that Groq exists.
    try:
        answer = await answer_question(payload.question, passages, chain)
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

    # 6. A refusal still costs a call, so it is not refunded: the tokens were
    #    spent, and the honest answer is the product working, not failing.
    return ChatResponse.of(answer)
