"""The chat contract.

Notice what `ChatRequest` does NOT have: an owner, a document identifier, a
model name, a number of passages. Every one of those would be a value the
client gets to choose, and every one of them would have to be validated,
rate-limited or ignored. The safest input is the one that was never accepted.
"""

import re
import unicodedata
import uuid
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


_SENTENCE = re.compile(r"(?<=[.;:!?])\s+")
# Citation markers are stripped before the comparison. They are ours, not the
# vocabulary of the answer, and the digit inside [1] happily matches a version
# number or a date in the passage - biasing the quote towards a sentence that
# has nothing to do with the question.
_MARKER = re.compile(r"\[\d+\]")
# Four letters or more: shorter tokens are articles and prepositions, which
# every sentence shares and which therefore tell us nothing about which one
# carries the evidence.
_WORD = re.compile(r"[^\W\d_]{4,}|\d+", re.UNICODE)


def _fold(text: str) -> str:
    """Lowercase and strip accents, so remboursés matches rembourses.

    A PDF text layer routinely loses accents that the model then restores in
    its answer. Comparing the two without folding would find almost no overlap
    on exactly the French documents this product is for.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _units(text: str) -> list[str]:
    """Sentences, and the lines of anything too long to be one.

    A table extracted from a PDF has no full stops: "Fibre Max ... 34,99 €
    Forfait mobile ... Les frais de mise en service, d'un montant de 39 euros"
    was ONE sentence of five hundred characters. The quote started at the
    table and was cut before the 39 euros the answer came from - found by a
    manual test. Its lines are its real units, so it is split on them. Prose
    keeps its sentences: splitting a wrapped paragraph on its lines would
    start quotes in the middle of a sentence for no reason.
    """
    units: list[str] = []
    for sentence in _SENTENCE.split(text):
        sentence = sentence.strip()
        if len(sentence) <= SNIPPET_CHARACTERS:
            units.append(sentence)
        else:
            units.extend(line.strip() for line in sentence.splitlines())
    return [unit for unit in units if unit]


def cited_by(answer: str, number: int) -> str:
    """What the answer says WITH citation [number] - the claim it must prove.

    An answer covering two documents cites each for different sentences.
    Scored against the whole answer, a card quoted whatever shared the most
    words with ANY of them: under a lease sentence cited [2][6], the card for
    [6] showed a clause about the phone contract's commitment period - found
    by a manual test. Falls back to the whole answer when no sentence carries
    the marker (it came from a bracket the parser folded, for instance).
    """
    marker = f"[{number}]"
    parts = [part for part in re.split(r"(?<=[.!?])\s+|\n+", answer) if marker in part]
    return " ".join(parts) or answer


def _snippet(text: str, answer: str) -> str:
    """Quote the part of the passage the answer actually came from.

    Found by testing by hand, and obvious in hindsight: taking the first 240
    characters showed the title of the document for a question about holidays.
    The snippet exists for one reason - letting a reader check the answer
    without opening the file - and a snippet that does not show the evidence
    serves no purpose at all.

    The sentences are scored against the ANSWER rather than the question,
    because an answer paraphrases its source and therefore shares far more
    vocabulary with it than a question does. No model call, no embedding: word
    overlap is enough to pick a sentence out of five.
    """
    if len(text) <= SNIPPET_CHARACTERS:
        return text

    sentences = _units(text)
    wanted = set(_WORD.findall(_fold(_MARKER.sub(" ", answer))))

    start = 0
    if sentences and wanted:
        scores = [len(wanted & set(_WORD.findall(_fold(s)))) for s in sentences]
        start = scores.index(max(scores))

    window = " ".join(sentences[start:])[:SNIPPET_CHARACTERS] if sentences else text
    # Cut on a word boundary, so the quote does not end mid-syllable.
    if len(window) >= SNIPPET_CHARACTERS:
        cut = window.rfind(" ")
        window = window[: cut if cut > 0 else SNIPPET_CHARACTERS].rstrip() + "..."
    # An ellipsis in front says the quote starts mid-passage, so a reader is
    # never left thinking the document begins here.
    return ("..." + window) if start else window


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
    # Things the question asks about that no cited passage names (ADR-0018):
    # "rétroviseur" when the contract only speaks of glass breakage. A fact
    # about words, checked by the database, that the interface shows next to
    # the answer - so an inference is never mistaken for a quotation. Empty
    # on almost every answer.
    not_in_documents: list[str] = []

    @classmethod
    def of(cls, answer: Answer) -> "ChatResponse":
        """Built field by field, never with from_attributes.

        An explicit mapping cannot start leaking a field that somebody adds to
        the dataclass later - or a metadata key such as the embedding model or
        the owner, which travel with every LangChain document. A response
        schema is a promise about what leaves the server, and promises are
        written down.
        """
        return cls(
            answer=answer.text,
            citations=[
                CitationResponse(
                    number=citation.number,
                    document_id=citation.document_id,
                    filename=citation.filename,
                    page_number=citation.page_number,
                    snippet=_snippet(citation.passage, cited_by(answer.text, citation.number)),
                )
                for citation in answer.citations
            ],
            is_grounded=answer.is_grounded,
            not_in_documents=list(answer.not_in_documents),
        )
