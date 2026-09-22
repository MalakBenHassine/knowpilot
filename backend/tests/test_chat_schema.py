"""The snippet shown under an answer.

Found by testing the product by hand: a question about holidays cited a
passage whose quote was the title page of the document. The snippet exists so
a reader can check the answer without opening the file, and one that does not
show the evidence is worse than none - it looks like provenance and is not.
"""

from app.schemas.chat import SNIPPET_CHARACTERS, _snippet, cited_by

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


# --- Found by the manual test of a phone contract ------------------------------

# A price table as pypdf extracts it: one cell per line, not a full stop in
# sight, and the sentence the answer came from glued to its end.
TARIFFS = (
    "Article 4 — Tarifs mensuels\n"
    "Les prix sont exprimés toutes taxes comprises, par mois :\n"
    "Élément\nDétail\nPrix mensuel\n"
    "Fibre Max\n2 Gbit/s, téléphonie fixe illimitée\n34,99 €\n"
    "Forfait mobile 5G\n80 Go en France, appels et SMS illimités\n12,99 €\n"
    "Option TV Essentiel\n140 chaînes, décodeur Nova Vision\n5,00 €\n"
    "Location du répéteur Wi-Fi\nInclus dans l'offre\n0,00 €\n"
    "Les frais de mise en service, d'un montant de 39 euros, ont été facturés une seule\n"
    "fois sur la première facture. Le prélèvement a lieu le 10 de chaque mois."
)


def test_a_table_without_full_stops_is_quoted_from_the_right_line() -> None:
    answer = "Vous avez payé 39 € pour la mise en service, facturés une seule fois [1]."

    snippet = _snippet(TARIFFS, answer)

    assert "39 euros" in snippet
    assert snippet.startswith("...")


def test_each_card_is_quoted_for_the_sentence_that_cites_it() -> None:
    """Two documents, two claims: card [2] proves the lease sentence only."""
    answer = (
        "Dans le contrat télécom : 49 € de dépôt, rendus sous 30 jours [1].\n"
        "Dans le bail : deux mois de loyer, soit 1 780 €, rendus après la remise des clés [2]."
    )

    assert cited_by(answer, 2) == (
        "Dans le bail : deux mois de loyer, soit 1 780 €, rendus après la remise des clés [2]."
    )
    assert "49 €" in cited_by(answer, 1)


def test_a_citation_repeated_in_one_sentence_keeps_the_whole_claim() -> None:
    answer = "Le loyer est de 890 € [1][2]. Le parking est inclus."

    assert cited_by(answer, 2) == "Le loyer est de 890 € [1][2]."


def test_a_number_that_is_only_a_prefix_is_not_a_citation() -> None:
    # [1] must not pick up the sentence citing [12].
    answer = "Premier point [12]. Second point [1]."

    assert cited_by(answer, 1) == "Second point [1]."


def test_no_sentence_with_the_marker_falls_back_to_the_whole_answer() -> None:
    assert cited_by("Une réponse sans marqueur.", 3) == "Une réponse sans marqueur."
