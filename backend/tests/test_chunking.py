"""Chunking.

The interesting tests are not "does it split": they are "does it split where a
human would have split", and "does a rule stay with its exception". The second
one is the reason the overlap exists at all.
"""

import pytest

from app.rag.chunking import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    Chunk,
    chunk_document,
    split_text,
)
from app.rag.parsing import ParsedDocument, ParsedPage


def document(*pages: str) -> ParsedDocument:
    return ParsedDocument(
        pages=[
            ParsedPage(number=number, text=text, source="text")
            for number, text in enumerate(pages, start=1)
        ],
        page_count=len(pages),
    )


# --- split_text: the basics ------------------------------------------------


def test_a_short_text_stays_in_one_piece() -> None:
    assert split_text("Une seule idee, courte.") == ["Une seule idee, courte."]


def test_an_empty_or_blank_text_produces_nothing() -> None:
    # An empty chunk would be indexed as a vector meaning nothing, and would
    # then be retrieved for questions it cannot answer.
    assert split_text("") == []
    assert split_text("   \n\n  \t ") == []


def test_no_piece_exceeds_the_requested_size() -> None:
    text = " ".join(f"Phrase numero {number}." for number in range(300))

    pieces = split_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)

    assert len(pieces) > 1
    assert all(len(piece) <= CHUNK_SIZE for piece in pieces)
    assert all(piece.strip() for piece in pieces)


def test_nothing_from_the_source_is_lost() -> None:
    text = " ".join(f"Phrase numero {number}." for number in range(300))

    pieces = split_text(text)

    # Every sentence must survive somewhere: a splitter that silently drops
    # content would make the assistant answer "not in your documents" about a
    # passage the user can read with their own eyes.
    for number in range(300):
        assert any(f"Phrase numero {number}." in piece for piece in pieces)


# --- split_text: cutting where a human would -------------------------------


def test_it_cuts_on_a_paragraph_break_when_there_is_one() -> None:
    first = "a" * 600
    second = "b" * 600

    pieces = split_text(f"{first}\n\n{second}", size=1000, overlap=150)

    # 1000 characters would have fallen in the middle of the second paragraph.
    # The paragraph break is where the author changed subject, so we use it.
    assert pieces[0] == first


def test_it_falls_back_to_a_hard_cut_when_there_is_no_boundary() -> None:
    # A table, a long identifier, a language without spaces: there is nothing
    # to cut on. Losing the passage would be worse than cutting badly.
    pieces = split_text("x" * 2500, size=1000, overlap=150)

    assert all(len(piece) <= 1000 for piece in pieces)
    assert "".join(piece for piece in pieces).count("x") >= 2500


def test_consecutive_pieces_share_their_overlap() -> None:
    text = " ".join(f"Phrase numero {number}." for number in range(300))

    pieces = split_text(text, size=1000, overlap=150)

    assert pieces[0][-100:] in pieces[1]


# --- The reason the overlap exists -----------------------------------------

RULE = "Le remboursement est possible sous 30 jours."
EXCEPTION = "Passe ce delai, aucun remboursement n'est accorde."
FILLER = "Remplissage. " * 12
CONTRACT = f"{FILLER}{RULE} {EXCEPTION}"
# Sized so that the window ends just after the full stop that separates the
# rule from its exception - the worst possible place to cut.
BAD_CUT_SIZE = len(FILLER) + len(RULE) + 2


def test_without_overlap_a_rule_is_separated_from_its_exception() -> None:
    pieces = split_text(CONTRACT, size=BAD_CUT_SIZE, overlap=0)

    # This is the failure we are buying insurance against: asked "can I be
    # refunded after 40 days?", retrieval returns the piece holding the rule,
    # the model answers "yes, within 30 days", and it cites a real source.
    assert not any(RULE in piece and EXCEPTION in piece for piece in pieces)


def test_the_overlap_keeps_a_rule_with_its_exception() -> None:
    pieces = split_text(CONTRACT, size=BAD_CUT_SIZE, overlap=80)

    assert any(RULE in piece and EXCEPTION in piece for piece in pieces)


# --- Guards ----------------------------------------------------------------


@pytest.mark.parametrize("overlap", [-1, 1000, 1500])
def test_an_overlap_that_cannot_work_is_refused(overlap: int) -> None:
    # Failing loudly at the boundary beats looping for ever inside the split.
    with pytest.raises(ValueError):
        split_text("some text", size=1000, overlap=overlap)


# --- chunk_document --------------------------------------------------------


def test_each_chunk_remembers_the_page_it_came_from() -> None:
    chunks = chunk_document(document("Texte de la page un.", "Texte de la page deux."))

    assert chunks == [
        Chunk(index=0, text="Texte de la page un.", page_number=1),
        Chunk(index=1, text="Texte de la page deux.", page_number=2),
    ]


def test_indexes_stay_sequential_across_pages() -> None:
    long_page = " ".join(f"Phrase numero {number}." for number in range(300))
    chunks = chunk_document(document(long_page, long_page))

    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert {chunk.page_number for chunk in chunks} == {1, 2}


def test_a_document_without_usable_text_produces_no_chunk() -> None:
    assert chunk_document(document("", "   ")) == []
