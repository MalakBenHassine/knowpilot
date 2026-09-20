"""The chat contract.

Notice what `ChatRequest` does NOT have: an owner, a document identifier, a
model name, a number of passages. Every one of those would be a value the
client gets to choose, and every one of them would have to be validated,
rate-limited or ignored. The safest input is the one that was never accepted.
"""

from typing import Annotated

from pydantic import BaseModel, StringConstraints

from app.rag.generation import MAX_QUESTION_CHARACTERS, Answer

# Stripped BEFORE the length is checked, which is the whole point. A question of
# three spaces satisfies min_length=1, reaches the encoder, and becomes an empty
# string there - a 500 caused by a value that was never a question. Empty and
# blank are different, and blank is the one that gets through; the place to
# settle that difference is the boundary, once, not in every function that
# later receives the value.
Question = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARACTERS),
]


class ChatRequest(BaseModel):
    """One field, on purpose.

    The owner comes from the session cookie and from nowhere else, so a handler
    cannot take it from here by mistake: the wrong value does not exist in the
    scope. That is a stronger guarantee than remembering not to use it.
    """

    # Validated here rather than in the handler, so a malformed or oversized
    # question is rejected by FastAPI with a 422 before it reaches any of our
    # code - and long before it could be turned into tokens somebody pays for.
    question: Question


class CitationResponse(BaseModel):
    """What the user needs to check the answer for themselves.

    `number` is what the model wrote between brackets; the filename and page
    were attached by us afterwards. The model never saw either, which is why it
    could not have invented them.
    """

    number: int
    filename: str
    page_number: int


class ChatResponse(BaseModel):
    # Empty when the answer is not grounded. The frontend shows its own
    # sentence rather than a server-written one, because the right wording
    # depends on something only the browser knows: whether this user has any
    # documents at all.
    answer: str
    citations: list[CitationResponse]
    # The field the interface actually branches on. Kept explicit rather than
    # inferred from an empty citation list, so the meaning stays in one place.
    is_grounded: bool

    @classmethod
    def of(cls, answer: Answer) -> "ChatResponse":
        """Built field by field, never with from_attributes.

        An explicit mapping cannot start leaking a field that someone adds to
        the dataclass later. A response schema is a promise about what leaves
        the server, and promises are written down.
        """
        return cls(
            answer=answer.text,
            citations=[
                CitationResponse(
                    number=citation.number,
                    filename=citation.filename,
                    page_number=citation.page_number,
                )
                for citation in answer.citations
            ],
            is_grounded=answer.is_grounded,
        )
