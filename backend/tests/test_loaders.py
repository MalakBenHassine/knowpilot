"""UploadedFileLoader: our parser behind LangChain's BaseLoader interface."""

from pathlib import Path

import pytest

from app.rag.loaders import UploadedFileLoader, pages_of
from app.rag.parsing import NoTextFoundError, ParsedDocument, ParsedPage, UnsupportedFormatError


def test_one_document_per_page_with_citation_metadata() -> None:
    parsed = ParsedDocument(
        pages=[
            ParsedPage(number=1, text="Page un.", source="text"),
            ParsedPage(number=3, text="Page trois.", source="ocr"),
        ],
        page_count=3,
    )

    documents = list(pages_of(parsed, filename="bail.pdf"))

    assert [doc.page_content for doc in documents] == ["Page un.", "Page trois."]
    # The page number is the real one, not the position in the list: page two
    # had no text, and a citation must still say "page 3".
    assert [doc.metadata["page_number"] for doc in documents] == [1, 3]
    assert all(doc.metadata["total_pages"] == 3 for doc in documents)
    assert all(doc.metadata["source"] == "bail.pdf" for doc in documents)
    assert [doc.metadata["extraction"] for doc in documents] == ["text", "ocr"]


def test_a_text_file_is_loaded(tmp_path: Path) -> None:
    path = tmp_path / "stored-under-a-random-name"
    path.write_text("Les conges payes sont de 25 jours. " * 10)

    documents = UploadedFileLoader(path, "text/plain", filename="conges.txt").load()

    assert len(documents) == 1
    # The name the user chose, never the path on disk.
    assert documents[0].metadata["source"] == "conges.txt"
    assert str(tmp_path) not in str(documents[0].metadata)


def test_parsing_errors_are_not_swallowed(tmp_path: Path) -> None:
    # The pipeline turns these into a failure reason the user can read. A
    # loader that returned [] instead would index an empty document as ready.
    blank = tmp_path / "blank.txt"
    blank.write_text("   ")

    with pytest.raises(NoTextFoundError):
        UploadedFileLoader(blank, "text/plain", filename="blank.txt").load()
    with pytest.raises(UnsupportedFormatError):
        UploadedFileLoader(blank, "application/zip", filename="x.zip").load()
