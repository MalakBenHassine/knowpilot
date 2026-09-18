"""Cut a parsed document into the passages that will be searched.

A chunk is the unit of retrieval: whatever we index here is the finest thing
the assistant will ever be able to quote. Too small and a passage no longer
holds a complete idea; too large and its embedding becomes an average of
several ideas, close to no question in particular.

Chunking runs page by page, which keeps `page_number` exact - a citation the
user can verify is the whole promise of the product. The cost is that a
paragraph spanning a page break is split there, which is acceptable: a page
break is already a hard boundary in the source.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.rag.parsing import ParsedDocument

# One to two paragraphs: long enough to carry a complete idea, short enough for
# the embedding to stay specific. BGE-M3 accepts far more, but "the model
# accepts it" and "retrieval stays precise" are two different limits.
CHUNK_SIZE = 1000

# Insurance against a cut landing between a rule and its exception, which would
# produce a wrong answer carrying a valid citation - the worst failure mode of
# a RAG system, because nothing raises. 15% is cheap; 50% would double the
# index and feed the model duplicated text.
CHUNK_OVERLAP = 150

# Tried in order: cut on a boundary the author wrote rather than on an
# arbitrary character. A paragraph break is a human saying "new idea here".
SEPARATORS = ("\n\n", "\n", ". ", " ")


@dataclass(frozen=True)
class Chunk:
    """A searchable passage.

    It deliberately knows nothing about documents, owners or storage: those
    belong to the persistence layer, which wraps this value later on.
    """

    index: int  # position in the document, for ordering and for debugging
    text: str
    page_number: int  # what makes "contract.pdf, page 7" possible


def _best_cut(text: str, start: int, end: int) -> int:
    """Return the offset to cut at, preferring the latest natural boundary.

    Only the second half of the window is considered: accepting the first
    separator found would produce chunks far below the target size, and a
    stream of tiny chunks is exactly what we are trying to avoid.
    """
    floor = start + (end - start) // 2
    for separator in SEPARATORS:
        position = text.rfind(separator, floor, end)
        if position != -1:
            return position + len(separator)
    # No boundary at all - a table, a long identifier, dense CJK text. Cutting
    # on size is worse than cutting on meaning, but losing the passage is worse
    # than both.
    return end


def split_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split one page of text into overlapping passages of at most `size`."""
    if size <= 0:
        raise ValueError("size must be positive")
    if not 0 <= overlap < size:
        raise ValueError("overlap must be between 0 and size")

    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = start + size
        if end >= len(text):
            # The tail always carries the overlap, so it is never a stub.
            pieces.append(text[start:].strip())
            break

        cut = _best_cut(text, start, end)
        pieces.append(text[start:cut].strip())
        # `max` guarantees forward progress whatever the overlap: without it,
        # an extreme value would loop for ever on the same offset.
        start = max(cut - overlap, start + 1)

    return [piece for piece in pieces if piece]


def chunk_document(
    document: ParsedDocument,
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[Chunk]:
    """Turn a parsed document into ordered chunks that remember their page."""
    chunks: list[Chunk] = []
    for page in document.pages:
        for piece in split_text(page.text, size, overlap):
            chunks.append(Chunk(index=len(chunks), text=piece, page_number=page.number))
    return chunks
