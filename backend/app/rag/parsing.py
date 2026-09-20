"""Turn an uploaded file into plain text.

The pipeline is deliberately two-tiered (ADR-0010): we read the text layer of
the PDF first, and only fall back to OCR on the pages that came back empty.
OCR is slow, CPU-hungry and imprecise - running it on every page of every
document would waste resources on files that never needed it.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pypdf import PageObject, PdfReader
from pypdf.errors import PdfReadError

logger = logging.getLogger(__name__)


class ParsingError(Exception):
    """Base class: every failure here maps to a `failure_reason` in the API."""


class UnsupportedFormatError(ParsingError):
    """The MIME type is not one we can read."""


class NoTextFoundError(ParsingError):
    """Neither the text layer nor OCR produced anything usable."""


class DocumentTooLargeError(ParsingError):
    """More pages than we accept."""


# --- Thresholds ------------------------------------------------------------
# These are policy, not magic: each one answers a question we had to decide.

# Below this, a page is "empty enough" to deserve OCR. A page holding only a
# header or a page number sits well under 40 characters.
MIN_CHARACTERS_PER_PAGE = 40

# Below this, the whole document is unusable: indexing it would only add noise
# to the vector store and invite hallucinated answers.
MIN_CHARACTERS_PER_DOCUMENT = 100

# A hard stop before memory blows up. A 500-page PDF is already an outlier.
MAX_PAGES = 500

# OCR costs seconds per page. We refuse to spend minutes on a single upload.
MAX_OCR_PAGES = 30


class OcrEngine(Protocol):
    """What we need from an OCR backend - nothing more.

    Declaring a Protocol instead of importing Tesseract means `parse_pdf` has
    no idea which engine runs, and the tests can pass a fake one. It is also
    why OCR stays optional: `None` simply disables the fallback.
    """

    def image_to_text(self, page_image: bytes) -> str: ...


@dataclass(frozen=True)
class ParsedPage:
    number: int  # 1-based, the way a human counts pages
    text: str
    source: Literal["text", "ocr"]  # kept for diagnostics and for the logs


@dataclass(frozen=True)
class ParsedDocument:
    pages: list[ParsedPage]
    page_count: int

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    @property
    def ocr_page_count(self) -> int:
        return sum(1 for page in self.pages if page.source == "ocr")


# --- Normalisation ---------------------------------------------------------

# "embed-\ndings" -> "embeddings": PDF line breaks split words, and the two
# halves would otherwise become two meaningless tokens.
_HYPHENATED_LINEBREAK = re.compile(r"(\w)-[ \t]*\n[ \t]*(\w)")
_MULTIPLE_SPACES = re.compile(r"[ \t]{2,}")
_SPACE_AROUND_NEWLINE = re.compile(r"[ \t]*\n[ \t]*")
# Three or more blank lines carry no more meaning than one paragraph break.
_EXTRA_NEWLINES = re.compile(r"\n{3,}")


def normalize(raw: str) -> str:
    """Clean extracted text so that equal content produces equal embeddings."""
    # NFKC folds ligatures and full-width forms: "ﬁ" becomes "fi", "Ａ" becomes
    # "A". Without it the same word can yield two different vectors.
    text = unicodedata.normalize("NFKC", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Soft hyphens and NULs are invisible and break tokenisation.
    text = text.replace("\u00ad", "").replace("\x00", "")
    text = _HYPHENATED_LINEBREAK.sub(r"\1\2", text)
    text = _MULTIPLE_SPACES.sub(" ", text)
    text = _SPACE_AROUND_NEWLINE.sub("\n", text)
    text = _EXTRA_NEWLINES.sub("\n\n", text)
    return text.strip()


# --- Plain text ------------------------------------------------------------


def parse_txt(path: Path) -> ParsedDocument:
    """Read a text file, guessing the encoding conservatively."""
    raw = path.read_bytes()
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Files exported from Windows are often cp1252. `replace` keeps the
        # document usable instead of rejecting it over one stray byte.
        decoded = raw.decode("cp1252", errors="replace")
        logger.info("decoded %s as cp1252", path.name)

    text = normalize(decoded)
    if len(text) < MIN_CHARACTERS_PER_DOCUMENT:
        raise NoTextFoundError("text file holds too little content")

    # A text file has no pages. One page keeps the rest of the pipeline simple.
    return ParsedDocument(
        pages=[ParsedPage(number=1, text=text, source="text")],
        page_count=1,
    )


# --- PDF -------------------------------------------------------------------


def _ocr_page(page: PageObject, ocr: OcrEngine) -> str:
    """Run OCR over the images embedded in one page.

    A scanned page is a single full-page image, so OCRing the embedded images
    covers the case we actually care about - without rendering the page, which
    would drag in a whole rasterisation stack.

    Every failure is swallowed on purpose: OCR is a *best effort*. If it breaks,
    the page simply stays empty and the caller decides what that means.
    """
    try:
        images = list(page.images)
    except Exception:  # pypdf raises several unrelated types here
        logger.warning("could not read images from the page", exc_info=True)
        return ""

    pieces: list[str] = []
    for image in images:
        try:
            pieces.append(ocr.image_to_text(image.data))
        except Exception:
            logger.warning("ocr failed on one image", exc_info=True)

    return normalize("\n".join(pieces))


def parse_pdf(path: Path, ocr: OcrEngine | None = None) -> ParsedDocument:
    """Extract the text layer, falling back to OCR page by page.

    `ocr=None` disables the fallback entirely, which is what the API does today.
    """
    try:
        reader = PdfReader(path)
    except PdfReadError as exc:
        raise ParsingError("unreadable pdf") from exc

    # We will not ask the user for a password, and we will not guess one.
    if reader.is_encrypted:
        raise ParsingError("encrypted pdf")

    total_pages = len(reader.pages)
    if total_pages > MAX_PAGES:
        raise DocumentTooLargeError(f"{total_pages} pages, limit is {MAX_PAGES}")

    pages: list[ParsedPage] = []
    ocr_budget = MAX_OCR_PAGES

    for number, page in enumerate(reader.pages, start=1):
        # Level 1: the text layer. Free, instant, accurate.
        try:
            text = normalize(page.extract_text() or "")
        except Exception:
            logger.warning("text extraction failed on page %s", number, exc_info=True)
            text = ""
        source: Literal["text", "ocr"] = "text"

        # Level 2: OCR, only for a page that came back nearly empty, and only
        # while the budget lasts. This is the whole point of the design.
        if len(text) < MIN_CHARACTERS_PER_PAGE and ocr is not None and ocr_budget > 0:
            ocr_budget -= 1
            recovered = _ocr_page(page, ocr)
            # Keep OCR output only if it beats what we already had: a bad scan
            # can produce less than the header we extracted normally.
            if len(recovered) > len(text):
                text, source = recovered, "ocr"

        # A genuinely empty page (a separator, a blank verso) is dropped rather
        # than indexed as noise. `page_count` still reflects the real document.
        if text:
            pages.append(ParsedPage(number=number, text=text, source=source))

    # A document-level threshold hides a page-level problem: four pages of
    # which three are pictures still passes, because the one readable page
    # carries enough characters. The document is then declared ready while most
    # of it is unreachable, and the assistant honestly answers "not found"
    # about a page the user can see. Until an OCR engine is plugged into the
    # protocol above, the least we owe is a loud line in the logs.
    unreadable = total_pages - len(pages)
    if unreadable and unreadable * 2 >= total_pages:
        logger.warning(
            "%s of %s pages produced no text in %s; most of this document is "
            "not searchable. An OCR engine would recover them.",
            unreadable,
            total_pages,
            # The generated identifier, never the name the user chose: a
            # filename is attacker-controlled and has no place in a log line.
            path.name,
        )

    document = ParsedDocument(pages=pages, page_count=total_pages)
    if len(document.text) < MIN_CHARACTERS_PER_DOCUMENT:
        raise NoTextFoundError("no usable text, even after ocr")

    return document


# --- Entry point -----------------------------------------------------------

PDF_MIME_TYPE = "application/pdf"
TEXT_MIME_TYPES = frozenset({"text/plain", "text/markdown"})


def parse_document(path: Path, mime_type: str, ocr: OcrEngine | None = None) -> ParsedDocument:
    """Route to the right parser. The only function the rest of the app calls.

    The MIME type is detected from the file *content* upstream, never from the
    extension: `invoice.pdf` renaming a ZIP must not reach the PDF parser.
    """
    if mime_type == PDF_MIME_TYPE:
        return parse_pdf(path, ocr)
    if mime_type in TEXT_MIME_TYPES:
        return parse_txt(path)
    raise UnsupportedFormatError(mime_type)
