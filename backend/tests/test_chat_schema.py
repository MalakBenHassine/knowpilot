"""The snippet shown under an answer.

Found by testing the product by hand: a question about holidays cited a
passage whose quote was the title page of the document. The snippet exists so
a reader can check the answer without opening the file, and one that does not
show the evidence is worse than none - it looks like provenance and is not.
"""

from app.schemas.chat import SNIPPET_CHARACTERS, _snippet

PASSAGE = (
    "Manuel du collaborateur, NovaTech Solutions SARL. Version 3.2, applicable a "
    "compter du 1er janvier 2026. Ce manuel annule et remplace toutes les versions "
    "anterieures et a ete valide par la direction generale le 12 decembre 2025. "
    "Article 1, horaires de travail. La duree hebdomadaire est fixee a 37 heures. "
    "Article 2, conges payes. Chaque collaborateur beneficie de 27 jours ouvres de "
    "conges payes par annee complete de travail effectif. A partir de cinq annees "
    "d anciennete, deux jours supplementaires sont accordes."
)


def test_the_quote_shows_the_sentence_the_answer_came_from() -> None:
    answer = "Un collaborateur beneficie de 27 jours ouvres de conges payes par an [1]."

    snippet = _snippet(PASSAGE, answer)

    assert "27 jours" in snippet
    # The failure this replaces: the first 240 characters were the title page.
    assert "Version 3.2" not in snippet


def test_a_quote_that_starts_mid_passage_says_so() -> None:
    answer = "Un collaborateur beneficie de 27 jours ouvres de conges payes par an [1]."

    # Otherwise a reader assumes the document begins here.
    assert _snippet(PASSAGE, answer).startswith("...")


def test_accents_do_not_break_the_match() -> None:
    """The case that makes this work on real French documents.

    A PDF text layer routinely loses accents; the model restores them in its
    answer. Comparing remboursés with rembourses without folding would find no
    overlap at all, and the snippet would fall back to the title page.
    """
    answer = "Chaque collaborateur bénéficie de 27 jours ouvrés de congés payés [1]."

    assert "27 jours" in _snippet(PASSAGE, answer)


def test_a_short_passage_is_quoted_whole() -> None:
    short = "Les repas sont rembourses dans la limite de 19 euros."

    assert _snippet(short, "19 euros par repas [1].") == short


def test_the_quote_stays_within_its_budget() -> None:
    # Five of these travel with every answer; an unbounded quote would turn a
    # two-line reply into a page.
    snippet = _snippet(PASSAGE, "conges payes")

    assert len(snippet) <= SNIPPET_CHARACTERS + 6


def test_an_answer_sharing_nothing_falls_back_to_the_beginning() -> None:
    """No match is not an error: quoting the start is a reasonable default."""
    snippet = _snippet(PASSAGE, "[1]")

    assert snippet.startswith("Manuel du collaborateur")
