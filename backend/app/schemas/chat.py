"""The chat contract.

Notice what `ChatRequest` does NOT have: an owner, a document identifier, a
model name, a number of passages. Every one of those would be a value the
client gets to choose, and every one of them would have to be validated,
rate-limited or ignored. The safest input is the one that was never accepted.
"""

import uuid
from collections.abc import Sequence
from typing import Annotated, Protocol

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

# Enough of the passage to recognise it without opening the document, short
# enough that five of them do not turn a small answer into a large payload.
SNIPPET_CHARACTERS = 240


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


class SourceChunk(Protocol):
    """What a citation needs beyond what the model wrote.

    A Protocol rather than an import of RetrievedChunk: a schema describing
    what leaves the server has no business depending on the module that talks
    to the database. Anything carrying these two attributes fits.
    """

    # Declared as read-only properties rather than plain attributes: a bare
    # annotation in a Protocol means a MUTABLE attribute, and a frozen
    # dataclass - which every value object in this codebase is - would not
    # satisfy it. mypy caught exactly that.
    @property
    def document_id(self) -> uuid.UUID: ...

    @property
    def text(self) -> str: ...


def _snippet(text: str) -> str:
    """Cut on a word boundary, so the quote does not end mid-syllable."""
    if len(text) <= SNIPPET_CHARACTERS:
        return text
    cut = text.rfind(" ", 0, SNIPPET_CHARACTERS)
    return text[: cut if cut > 0 else SNIPPET_CHARACTERS].rstrip() + "..."


class CitationResponse(BaseModel):
    """What the user needs to check the answer for themselves.

    `number` is what the model wrote between brackets; everything else was
    attached by us afterwards. The model never saw a filename, a page or an
    identifier, which is why it could not have invented them.
    """

    number: int
    # So the interface can link straight to the document it came from.
    document_id: uuid.UUID
    filename: str
    page_number: int
    # The reason a citation is worth anything: the user reads the sentence the
    # answer came from without leaving the page. A citation nobody can check is
    # decoration.
    snippet: str


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
    def of(cls, answer: Answer, sources: Sequence[SourceChunk]) -> "ChatResponse":
        """Built field by field, never with from_attributes.

        An explicit mapping cannot start leaking a field that somebody adds to
        the dataclass later. A response schema is a promise about what leaves
        the server, and promises are written down.

        `sources` is the very list that built the prompt, in the same order, so
        citation number n refers to sources[n - 1]. Those numbers were already
        checked against that length in `generation`, which is what makes this
        indexing safe rather than hopeful.
        """
        return cls(
            answer=answer.text,
            citations=[
                CitationResponse(
                    number=citation.number,
                    document_id=sources[citation.number - 1].document_id,
                    filename=citation.filename,
                    page_number=citation.page_number,
                    snippet=_snippet(sources[citation.number - 1].text),
                )
                for citation in answer.citations
            ],
            is_grounded=answer.is_grounded,
        )
