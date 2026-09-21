"""Cut page documents into the passages that will be searched.

A chunk is the unit of retrieval: whatever we index here is the finest thing
the assistant will ever be able to quote. Too small and a passage no longer
holds a complete idea; too large and its embedding becomes an average of
several ideas, close to no question in particular.

The splitting itself is LangChain's `RecursiveCharacterTextSplitter`: it tries
the separators in order - paragraph, line, sentence, word - and only falls
back to the next one when a piece is still too long. `split_documents` copies
the metadata of each page onto every chunk cut from it, so `page_number`
survives chunking without a line of our code, and a citation stays exact.

Splitting runs page by page (each page is one Document), so a chunk never
spans a page break. A paragraph that crosses one is cut there, which is
acceptable: a page break is already a hard boundary in the source.
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

# One paragraph or so: long enough to carry a complete idea, short enough for
# the embedding to stay specific. BGE-M3 accepts far more, but "the model
# accepts it" and "retrieval stays precise" are two different limits.
#
# 600 rather than the original 1000, and the number is measured rather than
# preferred. A real upload put an amendment, an energy rating and a phone
# number on one page; at 1000 they landed in a single chunk whose vector was
# their average, and a question about the phone number scored 0.630 against a
# ceiling of 0.60. The answer was in the database and never came out.
#
#   size   chunks   best distance for that question
#   1000     1        0.630   missed
#    600     3        0.478   found, widest margin
#    500     3        0.511   found
#    400     3        0.490   found
#    300     5        0.525   found, but worse than 600
#
# The curve has a minimum, which is the whole point: 300 is not better than
# 600. Cutting too finely separates a sentence from the context that gives it
# meaning. There is no correct chunk size - only a measured compromise for a
# given corpus, which is why the measurement is written down here rather than
# the conclusion alone.
#
# Measured with our first, hand-written splitter. LangChain's splitter cuts in
# different places, so the value was re-checked against the evaluation harness
# (evals/run.py) before the switch was committed: a number measured with one
# algorithm is only a hypothesis for another.
#
# Changing this invalidates every chunk already indexed. Existing documents
# keep their old cut until they are re-indexed: see scripts/reindex.py.
CHUNK_SIZE = 600

# Insurance against a cut landing between a rule and its exception, which would
# produce a wrong answer carrying a valid citation - the worst failure mode of
# a RAG system, because nothing raises. 50% would double the index and feed the
# model duplicated text.
CHUNK_OVERLAP = 100

# Tried in order: cut on a boundary the author wrote rather than on an
# arbitrary character. A paragraph break is a human saying "new idea here".
#
# Sentence BEFORE line, and the order is measured. In text extracted from a
# PDF, "\n" is not the author's line break but the layout's: every visual line
# ends with one. Line-first, the splitter cut "...mise en circulation le 12" |
# "mars 2023 et immatriculée GH-482-KT": the car and its plate landed in two
# chunks, and the model rightly refused to connect them. And the overlap did
# not save it - LangChain overlaps whole pieces, and a 105-character line does
# not fit in a 100-character overlap, so the overlap was zero.
#
#   on the real 4-page contract   chunks   cut mid-sentence   plate with car
#   line first                      11            7               no
#   sentence first                  12            2               yes
#
# The two remaining cuts are table rows, which have no full stop to cut on.
# The empty string last guarantees termination: a table or a long identifier
# with no boundary at all is still cut, rather than kept as one huge chunk.
SEPARATORS = ["\n\n", ". ", "\n", " ", ""]


def build_splitter(
    size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> RecursiveCharacterTextSplitter:
    """The one splitter configuration of the project.

    `keep_separator="end"` keeps ". " with the sentence it closes. The default
    moves it to the START of the next chunk, which produces passages beginning
    with a full stop - harmless to a model, ugly in a citation snippet.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if not 0 <= overlap < size:
        raise ValueError("overlap must be between 0 and size")
    return RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        separators=SEPARATORS,
        keep_separator="end",
        strip_whitespace=True,
    )


def split_pages(
    pages: Sequence[Document],
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[Document]:
    """Turn page documents into ordered chunks that remember their page.

    `chunk_index` is the position in the whole document. It orders the chunks,
    it is half of the unique key in the database, and it is what makes chunk
    identifiers deterministic - so indexing the same document twice overwrites
    rows instead of duplicating them.
    """
    chunks = build_splitter(size, overlap).split_documents(list(pages))
    kept = [chunk for chunk in chunks if chunk.page_content.strip()]
    for index, chunk in enumerate(kept):
        chunk.metadata["chunk_index"] = index
    return kept
