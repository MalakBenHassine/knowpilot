"""Chunking, through LangChain's RecursiveCharacterTextSplitter.

We do not test that LangChain can split a string - LangChain tests that. We
test OUR configuration of it: that it splits where a human would have split,
that a rule stays with its exception, and that the metadata a citation needs
survives the cut.
"""

import pytest
from langchain_core.documents import Document

from app.rag.chunking import CHUNK_OVERLAP, CHUNK_SIZE, build_splitter, split_pages


def pages(*texts: str) -> list[Document]:
    return [
        Document(page_content=text, metadata={"page_number": number, "source": "f.pdf"})
        for number, text in enumerate(texts, start=1)
    ]


def split(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    return build_splitter(size, overlap).split_text(text)


SENTENCES = " ".join(f"Phrase numero {number}." for number in range(300))


# --- The basics ------------------------------------------------------------


def test_a_short_text_stays_in_one_piece() -> None:
    assert split("Une seule idee, courte.") == ["Une seule idee, courte."]


def test_no_piece_exceeds_the_requested_size() -> None:
    pieces = split(SENTENCES)

    assert len(pieces) > 1
    assert all(len(piece) <= CHUNK_SIZE for piece in pieces)
    assert all(piece.strip() for piece in pieces)


def test_nothing_from_the_source_is_lost() -> None:
    pieces = split(SENTENCES)

    # A splitter that silently drops content would make the assistant answer
    # "not in your documents" about a passage the user can read themselves.
    for number in range(300):
        assert any(f"Phrase numero {number}." in piece for piece in pieces)


# --- Cutting where a human would -------------------------------------------


def test_it_cuts_on_a_paragraph_break_when_there_is_one() -> None:
    first = "a" * 600
    second = "b" * 600

    pieces = split(f"{first}\n\n{second}", size=1000, overlap=150)

    # The paragraph break is where the author changed subject, so we use it.
    assert pieces[0] == first


def test_a_sentence_keeps_its_full_stop() -> None:
    # keep_separator="end": the ". " closes the sentence it belongs to, instead
    # of opening the next chunk - which would make every snippet start with a
    # full stop.
    pieces = split(SENTENCES, size=200, overlap=0)

    assert all(not piece.startswith(".") for piece in pieces)
    assert pieces[0].endswith(".")


def test_it_falls_back_to_a_hard_cut_when_there_is_no_boundary() -> None:
    # A table, a long identifier, a language without spaces: nothing to cut
    # on. Losing the passage would be worse than cutting badly.
    pieces = split("x" * 2500, size=1000, overlap=150)

    assert all(len(piece) <= 1000 for piece in pieces)
    assert "".join(pieces).count("x") >= 2500


def test_consecutive_pieces_share_their_overlap() -> None:
    pieces = split(SENTENCES, size=1000, overlap=150)

    # The last sentence of a chunk opens the next one.
    last_sentence = pieces[0].rsplit("Phrase", 1)[1]
    assert f"Phrase{last_sentence}" in pieces[1]


# --- The reason the overlap exists -----------------------------------------

RULE = "Le remboursement est possible sous 30 jours."
EXCEPTION = "Passe ce delai, aucun remboursement n'est accorde."
FILLER = "Remplissage. " * 12
CONTRACT = f"{FILLER}{RULE} {EXCEPTION}"
# Sized so the window ends right after the rule - the worst possible place.
# The +1 is the space the splitter keeps after the full stop. Measured, and
# instructive: one character less and the rule no longer fits, so the splitter
# pushes it whole into the next chunk, WITH its exception. The recursive
# splitter avoids most bad cuts on its own; the overlap covers the one it
# cannot see coming - a window that ends exactly on a sentence boundary.
BAD_CUT_SIZE = len(FILLER) + len(RULE) + 1


def test_without_overlap_a_rule_is_separated_from_its_exception() -> None:
    pieces = split(CONTRACT, size=BAD_CUT_SIZE, overlap=0)

    # The failure we buy insurance against: asked "can I be refunded after 40
    # days?", retrieval returns the piece holding the rule, the model answers
    # "yes, within 30 days", and it cites a real source.
    assert not any(RULE in piece and EXCEPTION in piece for piece in pieces)


def test_the_overlap_keeps_a_rule_with_its_exception() -> None:
    pieces = split(CONTRACT, size=BAD_CUT_SIZE, overlap=80)

    assert any(RULE in piece and EXCEPTION in piece for piece in pieces)


# --- Guards ----------------------------------------------------------------


@pytest.mark.parametrize("overlap", [-1, 1000, 1500])
def test_an_overlap_that_cannot_work_is_refused(overlap: int) -> None:
    with pytest.raises(ValueError):
        build_splitter(1000, overlap)


def test_a_size_of_zero_is_refused() -> None:
    with pytest.raises(ValueError):
        build_splitter(0, 0)


# --- split_pages: the metadata a citation needs ----------------------------


def test_each_chunk_remembers_the_page_it_came_from() -> None:
    chunks = split_pages(pages("Texte de la page un.", "Texte de la page deux."))

    assert [(c.page_content, c.metadata["page_number"]) for c in chunks] == [
        ("Texte de la page un.", 1),
        ("Texte de la page deux.", 2),
    ]
    # split_documents copies every metadata key, not only the page.
    assert all(chunk.metadata["source"] == "f.pdf" for chunk in chunks)


def test_a_chunk_never_spans_two_pages() -> None:
    chunks = split_pages(pages("Fin de la page un.", "Debut de la page deux."))

    assert not any("un." in c.page_content and "deux" in c.page_content for c in chunks)


def test_indexes_stay_sequential_across_pages() -> None:
    chunks = split_pages(pages(SENTENCES, SENTENCES))

    assert [chunk.metadata["chunk_index"] for chunk in chunks] == list(range(len(chunks)))
    assert {chunk.metadata["page_number"] for chunk in chunks} == {1, 2}


def test_a_document_without_usable_text_produces_no_chunk() -> None:
    # An empty chunk would be indexed as a vector meaning nothing, and then be
    # retrieved for questions it cannot answer.
    assert split_pages(pages("", "   \n\n  \t ")) == []


def test_the_pages_given_are_not_modified() -> None:
    source = pages("Texte de la page un.")

    split_pages(source)

    # chunk_index is written on the chunks, never on the caller's pages.
    assert "chunk_index" not in source[0].metadata


def test_a_sentence_wrapped_by_the_pdf_layout_stays_whole() -> None:
    """The car and its plate, cut apart by a visual line break.

    Text extracted from a PDF ends every visual line with "\n". Splitting on
    lines before sentences cut "...mise en circulation le 12" | "mars 2023 et
    immatriculée GH-482-KT" - and the model rightly refused to connect them.
    The text below is the real page, wrapped exactly as pypdf returned it.
    """
    page = (
        "Contrat d'assurance automobile — Horizon Mutuelle\n"
        "Conditions particulières — Formule Tous Risques Confort\n"
        "Numéro de contrat : HZ-2026-448217. Le présent contrat est conclu entre la société "
        "Horizon Mutuelle,\n"
        "société d'assurance mutuelle dont le siège est situé 18 quai Perrache, 69002 Lyon, "
        "ci-après « l'assureur »,\n"
        "et Monsieur Julien Moreau, demeurant 7 rue des Tanneurs, 69007 Lyon, ci-après "
        "« l'assuré ».\n"
        "Article 1 — Véhicule assuré\n"
        "Le véhicule assuré est une Peugeot 308 SW, motorisation hybride rechargeable, mise en "
        "circulation le 12\n"
        "mars 2023 et immatriculée GH-482-KT. Le véhicule est garé la nuit dans un parking "
        "souterrain fermé. Il\n"
        "est utilisé pour les déplacements privés et le trajet domicile-travail ; tout usage "
        "professionnel (tournées,\n"
        "livraisons, transport de personnes à titre onéreux) est exclu.\n"
    )

    chunks = split(page)

    assert any("Peugeot 308 SW" in c and "GH-482-KT" in c for c in chunks)
