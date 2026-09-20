"""Parsing, including the OCR fallback.

The OCR engine is faked: a real Tesseract would make these tests slow,
non-deterministic, and impossible to break on purpose.
"""

import logging
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.rag.parsing import (
    DocumentTooLargeError,
    NoTextFoundError,
    ParsedDocument,
    UnsupportedFormatError,
    normalize,
    parse_document,
    parse_pdf,
    parse_txt,
)

# --- Fixtures built in memory, never committed as opaque binaries ----------


def write_pdf(path: Path, pages: list[str]) -> Path:
    """A normal PDF: real text in its text layer."""
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    for text in pages:
        pdf.drawString(50, 750, text)
        pdf.showPage()
    pdf.save()
    path.write_bytes(buffer.getvalue())
    return path


def write_scanned_pdf(path: Path, pages: int = 1) -> Path:
    """A scanned PDF: a picture, no text layer at all."""
    picture = BytesIO()
    Image.new("RGB", (120, 120), "white").save(picture, format="PNG")
    picture.seek(0)

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    for _ in range(pages):
        pdf.drawImage(ImageReader(picture), 50, 50, width=200, height=200)
        pdf.showPage()
    pdf.save()
    path.write_bytes(buffer.getvalue())
    return path


class FakeOcr:
    """Returns a fixed sentence and counts how often it was called."""

    def __init__(self) -> None:
        self.calls = 0

    def image_to_text(self, page_image: bytes) -> str:
        self.calls += 1
        return "Recovered by optical character recognition. " * 4


class ExplodingOcr:
    def image_to_text(self, page_image: bytes) -> str:
        raise RuntimeError("tesseract is not installed")


# --- normalize -------------------------------------------------------------


def test_normalize_rejoins_a_word_split_across_lines() -> None:
    # The whole point: "embed-\ndings" and "embeddings" must embed the same.
    assert normalize("embed-\ndings are vectors") == "embeddings are vectors"


def test_normalize_collapses_whitespace_and_folds_ligatures() -> None:
    assert normalize("  the   ﬁle \n\n\n\n next  ") == "the file\n\nnext"


def test_normalize_keeps_case_and_punctuation() -> None:
    # Lowercasing or stripping punctuation would destroy meaning the embedding
    # model relies on. This test exists to stop a "helpful" future refactor.
    assert normalize("Who wrote it? Malak did.") == "Who wrote it? Malak did."


# --- parse_txt -------------------------------------------------------------


def test_parse_txt_reads_utf8(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("Un café à Paris. " * 20, encoding="utf-8")

    document = parse_txt(path)

    assert document.page_count == 1
    assert "café" in document.text


def test_parse_txt_falls_back_to_cp1252(tmp_path: Path) -> None:
    path = tmp_path / "windows.txt"
    path.write_bytes(("Une génération complète. " * 20).encode("cp1252"))

    assert "génération" in parse_txt(path).text


def test_parse_txt_rejects_a_nearly_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "tiny.txt"
    path.write_text("hello", encoding="utf-8")

    with pytest.raises(NoTextFoundError):
        parse_txt(path)


# --- parse_pdf: the happy path --------------------------------------------


def test_parse_pdf_reads_the_text_layer_without_ocr(tmp_path: Path) -> None:
    ocr = FakeOcr()
    path = write_pdf(tmp_path / "doc.pdf", ["A first page of text. " * 5])

    document = parse_pdf(path, ocr=ocr)

    assert document.page_count == 1
    assert "first page" in document.text
    # The core of the design: a readable page never pays for OCR.
    assert ocr.calls == 0
    assert document.ocr_page_count == 0


def test_parse_pdf_numbers_pages_from_one(tmp_path: Path) -> None:
    path = write_pdf(tmp_path / "doc.pdf", ["Page one text. " * 5, "Page two text. " * 5])

    document = parse_pdf(path)

    assert [page.number for page in document.pages] == [1, 2]


def test_parse_pdf_refuses_a_document_over_the_page_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.rag.parsing.MAX_PAGES", 1)
    path = write_pdf(tmp_path / "big.pdf", ["Text. " * 10, "More. " * 10])

    with pytest.raises(DocumentTooLargeError):
        parse_pdf(path)


# --- parse_pdf: the fallback ----------------------------------------------


def test_scanned_pdf_without_ocr_reports_no_text(tmp_path: Path) -> None:
    path = write_scanned_pdf(tmp_path / "scan.pdf")

    with pytest.raises(NoTextFoundError):
        parse_pdf(path, ocr=None)


def test_scanned_pdf_is_recovered_by_ocr(tmp_path: Path) -> None:
    ocr = FakeOcr()
    path = write_scanned_pdf(tmp_path / "scan.pdf")

    document = parse_pdf(path, ocr=ocr)

    assert ocr.calls == 1
    assert "Recovered" in document.text
    # `source` is what tells us, in the logs, that this upload cost CPU.
    assert document.ocr_page_count == 1


def test_ocr_stays_within_its_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.rag.parsing.MAX_OCR_PAGES", 2)
    ocr = FakeOcr()
    path = write_scanned_pdf(tmp_path / "scan.pdf", pages=5)

    document = parse_pdf(path, ocr=ocr)

    # Without the budget, a 300-page scan would freeze a worker for minutes.
    assert ocr.calls == 2
    assert document.page_count == 5
    assert len(document.pages) == 2


def test_a_broken_ocr_engine_degrades_instead_of_crashing(tmp_path: Path) -> None:
    path = write_scanned_pdf(tmp_path / "scan.pdf")

    # The user gets an honest "no text found", not a 500.
    with pytest.raises(NoTextFoundError):
        parse_pdf(path, ocr=ExplodingOcr())


# --- parse_document --------------------------------------------------------


def test_parse_document_routes_on_the_mime_type(tmp_path: Path) -> None:
    pdf = write_pdf(tmp_path / "doc.pdf", ["Routed to the pdf parser. " * 5])
    txt = tmp_path / "doc.txt"
    txt.write_text("Routed to the text parser. " * 10, encoding="utf-8")

    assert isinstance(parse_document(pdf, "application/pdf"), ParsedDocument)
    assert isinstance(parse_document(txt, "text/plain"), ParsedDocument)


def test_parse_document_rejects_an_unsupported_type(tmp_path: Path) -> None:
    path = tmp_path / "archive.zip"
    path.write_bytes(b"PK\x03\x04")

    with pytest.raises(UnsupportedFormatError):
        parse_document(path, "application/zip")


def test_a_mostly_unreadable_document_says_so_in_the_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Found on a real upload: four pages, three of them pictures.

    The document passed every check - one readable page carried more than
    MIN_CHARACTERS_PER_DOCUMENT - and was reported as fully indexed while three
    quarters of it were unreachable. A document-level threshold cannot see a
    page-level problem, so until OCR is wired in, the logs must say it out loud.
    """
    path = tmp_path / "mixed.pdf"
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(50, 750, "Une seule page lisible. " * 8)
    pdf.showPage()
    for _ in range(3):  # three pages with nothing on them at all
        pdf.showPage()
    pdf.save()
    path.write_bytes(buffer.getvalue())

    with caplog.at_level(logging.WARNING, logger="app.rag.parsing"):
        document = parse_pdf(path)

    assert document.page_count == 4
    assert len(document.pages) == 1
    assert "3 of 4 pages produced no text" in caplog.text
    # The generated identifier, never the name the user chose.
    assert "mixed.pdf" in caplog.text
