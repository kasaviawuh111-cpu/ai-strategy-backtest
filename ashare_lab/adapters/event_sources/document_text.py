"""Deterministic announcement-document embedded-text extraction.

The extractor deliberately supports only embedded text layers and rejects any
provider-reported page with no extractable text.  Passing that gate proves page
coverage and reproducibility, not semantic completeness: a damaged page that
contains only a header or page number can still have a non-empty text layer.
Production use therefore remains unavailable until a stronger source or an
audited document-coverage policy exists.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Literal, Protocol, cast

_PAGE_SEPARATOR = "\n\f\n"
_NORMALIZATION = "unicode_nfkc_lf_strip.v1"
_EXTRACTOR_NAME = "pypdfium2_text_layer"
_EXTRACTOR_VERSION = "1"
DOCUMENT_TEXT_INCOMPLETE_EXIT_CODE = 3


class DocumentTextExtractionError(RuntimeError):
    """A document-text extractor dependency or operation failed."""


class DocumentTextIncompleteError(DocumentTextExtractionError):
    """The source document cannot prove complete, page-covering text."""


class _PdfTextPage(Protocol):
    def get_text_range(self) -> str: ...

    def close(self) -> None: ...


class _PdfPage(Protocol):
    def get_textpage(self) -> _PdfTextPage: ...

    def close(self) -> None: ...


class _PdfDocument(Protocol):
    def __len__(self) -> int: ...

    def __getitem__(self, index: int) -> _PdfPage: ...

    def close(self) -> None: ...


class _PdfiumModule(Protocol):
    def PdfDocument(self, value: bytes) -> _PdfDocument: ...


type _PdfiumLoader = Callable[[], _PdfiumModule]


@dataclass(frozen=True, slots=True)
class ExtractedTextPage:
    """One normalized, non-empty PDF or announcement page."""

    page_number: int
    text: str
    text_sha256: str
    character_count: int
    non_whitespace_character_count: int


@dataclass(frozen=True, slots=True)
class ExtractedDocumentText:
    """Content-addressed text tied to the untouched source document hash."""

    source_format: Literal["notice_content", "pdf"]
    source_document_sha256: str
    pages: tuple[ExtractedTextPage, ...]
    text: str
    text_sha256: str
    extractor_name: str
    extractor_version: str
    extractor_library_version: str | None = None
    normalization: str = _NORMALIZATION
    quality_status: str = "complete_text_layer"

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def character_count(self) -> int:
        return len(self.text)

    @property
    def non_whitespace_character_count(self) -> int:
        return sum(not character.isspace() for character in self.text)

    def pages_json(self) -> str:
        """Return canonical per-page text and quality evidence for snapshots."""

        return json.dumps(
            [
                {
                    "pageNumber": page.page_number,
                    "text": page.text,
                    "textSha256": page.text_sha256,
                    "characterCount": page.character_count,
                    "nonWhitespaceCharacterCount": page.non_whitespace_character_count,
                }
                for page in self.pages
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


class AnnouncementDocumentTextExtractor:
    """Extract reproducible embedded text from announcement content or PDF bytes.

    ``pypdfium2`` is imported lazily so ordinary event ingestion has no PDF
    dependency. Callers opt in explicitly and receive a fail-closed error when
    the dependency or a non-empty text layer for every provider page is unavailable.
    This adapter does not claim that non-empty text is the semantic whole document.
    """

    def __init__(self, *, pdfium_loader: _PdfiumLoader | None = None) -> None:
        self._pdfium_loader = pdfium_loader or _load_pdfium

    def extract_notice_content(self, notice_content: str) -> ExtractedDocumentText:
        if type(notice_content) is not str:
            raise DocumentTextIncompleteError("notice_content must be text")
        source_bytes = notice_content.encode("utf-8")
        page = _normalized_page(notice_content, page_number=1)
        return _build_artifact(
            source_format="notice_content",
            source_document_sha256=hashlib.sha256(source_bytes).hexdigest(),
            pages=(page,),
            extractor_name="eastmoney_notice_content",
        )

    def extract_pdf(
        self,
        pdf_bytes: bytes,
        *,
        expected_page_count: int | None = None,
    ) -> ExtractedDocumentText:
        if type(pdf_bytes) is not bytes or not pdf_bytes.startswith(b"%PDF-"):
            raise DocumentTextIncompleteError("source document is not a PDF")
        if expected_page_count is not None and (
            type(expected_page_count) is not int or expected_page_count <= 0
        ):
            raise DocumentTextIncompleteError("expected_page_count must be positive")
        source_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        try:
            pdfium = self._pdfium_loader()
        except ModuleNotFoundError as exc:
            raise DocumentTextExtractionError(
                "pypdfium2 is required for opt-in PDF text extraction"
            ) from exc

        document: _PdfDocument | None = None
        try:
            document = pdfium.PdfDocument(pdf_bytes)
            page_count = len(document)
            if page_count <= 0:
                raise DocumentTextIncompleteError("PDF contains no pages")
            if expected_page_count is not None and page_count != expected_page_count:
                raise DocumentTextIncompleteError(
                    "PDF page count does not match provider metadata: "
                    f"expected {expected_page_count}, extracted {page_count}"
                )
            pages = tuple(
                _extract_pdf_page(document, page_index=index) for index in range(page_count)
            )
        except DocumentTextExtractionError:
            raise
        except Exception as exc:
            raise DocumentTextExtractionError("PDF text-layer extraction failed") from exc
        finally:
            if document is not None:
                document.close()

        return _build_artifact(
            source_format="pdf",
            source_document_sha256=source_sha256,
            pages=pages,
            extractor_name=_EXTRACTOR_NAME,
            extractor_library_version=_pdfium_library_version(),
        )


def _load_pdfium() -> _PdfiumModule:
    module: ModuleType = importlib.import_module("pypdfium2")
    return cast(_PdfiumModule, module)


def _pdfium_library_version() -> str | None:
    try:
        return importlib.metadata.version("pypdfium2")
    except importlib.metadata.PackageNotFoundError:
        return None


def _extract_pdf_page(document: _PdfDocument, *, page_index: int) -> ExtractedTextPage:
    page: _PdfPage | None = None
    text_page: _PdfTextPage | None = None
    try:
        page = document[page_index]
        text_page = page.get_textpage()
        raw_text = text_page.get_text_range()
        if type(raw_text) is not str:
            raise DocumentTextExtractionError(
                f"PDF page {page_index + 1} returned non-text content"
            )
        return _normalized_page(raw_text, page_number=page_index + 1)
    finally:
        if text_page is not None:
            text_page.close()
        if page is not None:
            page.close()


def _normalized_page(raw_text: str, *, page_number: int) -> ExtractedTextPage:
    line_normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = unicodedata.normalize("NFKC", line_normalized).strip()
    if not normalized or not any(not character.isspace() for character in normalized):
        raise DocumentTextIncompleteError(
            f"document page {page_number} has no extractable text; OCR is not enabled"
        )
    return ExtractedTextPage(
        page_number=page_number,
        text=normalized,
        text_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        character_count=len(normalized),
        non_whitespace_character_count=sum(not character.isspace() for character in normalized),
    )


def _build_artifact(
    *,
    source_format: Literal["notice_content", "pdf"],
    source_document_sha256: str,
    pages: tuple[ExtractedTextPage, ...],
    extractor_name: str,
    extractor_library_version: str | None = None,
) -> ExtractedDocumentText:
    text = _PAGE_SEPARATOR.join(page.text for page in pages)
    return ExtractedDocumentText(
        source_format=source_format,
        source_document_sha256=source_document_sha256,
        pages=pages,
        text=text,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        extractor_name=extractor_name,
        extractor_version=_EXTRACTOR_VERSION,
        extractor_library_version=extractor_library_version,
    )
