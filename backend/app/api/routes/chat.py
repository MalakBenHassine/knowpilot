"""The chat endpoints: the only place where every piece meets.

Two routes, one flow:

    POST /api/chat          the whole answer as JSON
    POST /api/chat/stream   the same answer as Server-Sent Events

Both run the same first steps through `_prepare` - owner, retrieval, quota - so
the two cannot drift apart on who pays and whose passages are searched. They
differ only in how the answer leaves the building.

The LangChain pieces - an owner-scoped retriever and an LCEL answer chain -
are deliberately NOT composed into one `retriever | prompt | model` chain.
Between retrieval and generation sit two decisions a chain cannot express
cleanly: skip the model entirely when nothing was found, and reserve a paid
quota before calling it. Two runnables with the policy in between is the
honest shape of this flow.

The owner is read from the session and from nowhere else. `ChatRequest` has a
single field, so there is no other owner in scope to take by mistake.
"""

import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from langchain_core.documents import Document

from app.api.deps import AnswerChain, CsrfProtected, Quota, Retrievers
from app.core.clock import today
from app.core.quota import QuotaExceededError, QuotaTracker
from app.rag.generation import Delta, answer_question, stream_answer
from app.rag.llm import LanguageModelError, QuotaExhaustedError
from app.rag.retrieval import RetrieverFactory
from app.schemas.chat import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

UNGROUNDED_RESPONSE = ChatResponse(answer="", citations=[], is_grounded=False)


async def _prepare(
    question: str, owner_id: str, retrievers: RetrieverFactory, quota: QuotaTracker
) -> Sequence[Document]:
    """Steps 1 to 4, shared by both routes. Empty means: answer "not found"."""
    # 1. Retrieve BEFORE reserving the budget. The hybrid retriever embeds the
    #    question with the very model that indexed the chunks - in a thread, so
    #    the event loop keeps serving everyone else - while PostgreSQL runs the
    #    keyword search, and both search this owner's passages only. It is built
    #    per request because the owner is part of it: there is no shared
    #    retriever that could serve the wrong tenant.
    try:
        passages = await retrievers.for_owner(owner_id).ainvoke(question)
    except Exception:
        # The model or the database failed. The traceback goes to the logs;
        # the user learns only that answering is unavailable right now.
        logger.exception("retrieval failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Answering is unavailable"
        ) from None

    # 2. Nothing close enough - including a user with no document yet. Not an
    #    error: "I do not have that information" is an answer, and the
    #    strongest guard of the feature is that the model is never called.
    #    Nothing was sent to the provider, so nothing is charged.
    if not passages:
        return passages

    # 3. Reserve now, because the next step spends tokens that cannot be
    #    reclaimed. Incrementing after a successful answer would let two
    #    simultaneous requests both pass the check; charging for an answer the
    #    provider fails to deliver is undone by a refund. Of the two defects,
    #    this is the reversible one.
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
    return passages


@router.post("", summary="Ask a question about your own documents")
async def ask(
    payload: ChatRequest,
    # CSRF rather than the plain session: this is a POST that spends a budget
    # and money. A third-party page must not be able to trigger it with the
    # cookie the browser sends on its own.
    session: CsrfProtected,
    retrievers: Retrievers,
    chain: AnswerChain,
    quota: Quota,
) -> ChatResponse:
    # The owner: `sub` is the Keycloak subject, stable even when the user
    # changes their email, and the only identity we trust, because it came out
    # of a cookie we signed ourselves.
    owner_id = session.sub
    passages = await _prepare(payload.question, owner_id, retrievers, quota)
    if not passages:
        return UNGROUNDED_RESPONSE

    # 4. Generate. Every provider failure arrives as one of our own exception
    #    types (llm.GuardedChain), so this handler never has to know that Groq
    #    exists.
    try:
        answer = await answer_question(payload.question, passages, chain, today=today())
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

    # 5. A refusal still costs a call, so it is not refunded: the tokens were
    #    spent, and the honest answer is the product working, not failing.
    return ChatResponse.of(answer)


def _event(name: str, data: dict[str, Any]) -> str:
    """One Server-Sent Event.

    JSON in `data`, always on one line: json.dumps escapes newlines, so an
    answer containing a blank line cannot end the event early - the SSE
    equivalent of the passage that tried to close its <passages> block.
    """
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/stream", summary="Ask a question; receive the answer as it is generated")
async def ask_streaming(
    payload: ChatRequest,
    session: CsrfProtected,
    retrievers: Retrievers,
    chain: AnswerChain,
    quota: Quota,
) -> StreamingResponse:
    """The same answer as POST /api/chat, delivered as Server-Sent Events.

    Everything that can fail with a status code fails BEFORE the stream opens:
    authentication, validation, retrieval and the quota answer 401, 422, 503
    and 429 exactly like the JSON route. Once the 200 is sent, the status can
    no longer change, so later failures travel as an `error` event.

    Events, in order:
        stage   {"stage": "generating"}   retrieval is done, the model is called
        token   {"text": "..."}           verified text to append (0..n times)
        done    ChatResponse              the authoritative answer; replaces
                                          whatever was shown
        error   {"kind", "retry_after"?}  instead of `done`, on a failure
    """
    owner_id = session.sub
    passages = await _prepare(payload.question, owner_id, retrievers, quota)

    async def events() -> AsyncIterator[str]:
        if not passages:
            yield _event("done", UNGROUNDED_RESPONSE.model_dump(mode="json"))
            return
        # A real stage, reported by the server - the interface used to guess
        # it with a timer.
        yield _event("stage", {"stage": "generating"})
        try:
            async for event in stream_answer(payload.question, passages, chain, today=today()):
                if isinstance(event, Delta):
                    yield _event("token", {"text": event.text})
                else:
                    response = ChatResponse.of(event.answer)
                    yield _event("done", response.model_dump(mode="json"))
        except QuotaExhaustedError as exc:
            await quota.refund(owner_id=owner_id)
            yield _event("error", {"kind": "rate_limited", "retry_after": int(exc.retry_after)})
        except LanguageModelError as exc:
            await quota.refund(owner_id=owner_id)
            logger.warning("streamed generation failed: %s", type(exc).__name__)
            yield _event("error", {"kind": "server"})
        # A client that disconnects mid-answer is NOT refunded: the tokens up
        # to that point were spent, exactly like a refusal. The cancellation
        # propagates into stream_answer, which closes the provider's stream -
        # generation stops, and so does the bill.

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            # A cached or transformed stream is a broken stream.
            "Cache-Control": "no-cache, no-transform",
            # Tells a buffering reverse proxy (nginx) to pass bytes through as
            # they come, instead of holding the answer until it is complete -
            # which would silently turn streaming back into waiting.
            "X-Accel-Buffering": "no",
        },
    )
