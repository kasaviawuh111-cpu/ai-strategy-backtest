from __future__ import annotations

import hashlib
import json

import pytest

from ashare_lab.adapters.event_sources.document_text import (
    AnnouncementDocumentTextExtractor,
    DocumentTextExtractionError,
    DocumentTextIncompleteError,
)


class _FakeTextPage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.closed = False

    def get_text_range(self) -> str:
        return self.text

    def close(self) -> None:
        self.closed = True


class _FakePage:
    def __init__(self, text: str) -> None:
        self.text_page = _FakeTextPage(text)
        self.closed = False

    def get_textpage(self) -> _FakeTextPage:
        return self.text_page

    def close(self) -> None:
        self.closed = True


class _FakeDocument:
    def __init__(self, page_texts: list[str]) -> None:
        self.pages = [_FakePage(text) for text in page_texts]
        self.closed = False

    def __len__(self) -> int:
        return len(self.pages)

    def __getitem__(self, index: int) -> _FakePage:
        return self.pages[index]

    def close(self) -> None:
        self.closed = True


class _FakePdfium:
    def __init__(self, document: _FakeDocument) -> None:
        self.document = document
        self.received_bytes: bytes | None = None

    def PdfDocument(self, value: bytes) -> _FakeDocument:
        self.received_bytes = value
        return self.document


def test_notice_content_is_nfkc_normalized_and_tied_to_raw_hash() -> None:
    raw = "\r\n  年报提到ＡＩ ６次。\r\n"

    result = AnnouncementDocumentTextExtractor().extract_notice_content(raw)

    assert result.source_format == "notice_content"
    assert result.source_document_sha256 == hashlib.sha256(raw.encode()).hexdigest()
    assert result.text == "年报提到AI 6次。"
    assert result.page_count == 1
    assert result.quality_status == "complete_text_layer"
    assert result.text_sha256 == hashlib.sha256(result.text.encode()).hexdigest()
    page_payload = json.loads(result.pages_json())
    assert page_payload == [
        {
            "characterCount": 10,
            "nonWhitespaceCharacterCount": 9,
            "pageNumber": 1,
            "text": "年报提到AI 6次。",
            "textSha256": hashlib.sha256("年报提到AI 6次。".encode()).hexdigest(),
        }
    ]


def test_pdf_extracts_every_page_and_preserves_page_boundaries() -> None:
    pdf_bytes = b"%PDF-1.7\nfixture"
    document = _FakeDocument(["第一页ＡＩ", "第二页\r\n正文"])
    pdfium = _FakePdfium(document)
    extractor = AnnouncementDocumentTextExtractor(pdfium_loader=lambda: pdfium)

    result = extractor.extract_pdf(pdf_bytes, expected_page_count=2)

    assert pdfium.received_bytes == pdf_bytes
    assert result.source_format == "pdf"
    assert result.source_document_sha256 == hashlib.sha256(pdf_bytes).hexdigest()
    assert result.text == "第一页AI\n\f\n第二页\n正文"
    assert [page.text for page in result.pages] == ["第一页AI", "第二页\n正文"]
    assert result.page_count == 2
    assert document.closed is True
    assert all(page.closed and page.text_page.closed for page in document.pages)


def test_pdf_page_count_mismatch_fails_closed_before_text_is_published() -> None:
    document = _FakeDocument(["第一页", "第二页"])
    extractor = AnnouncementDocumentTextExtractor(pdfium_loader=lambda: _FakePdfium(document))

    with pytest.raises(DocumentTextIncompleteError, match=r"expected 3, extracted 2"):
        extractor.extract_pdf(
            b"%PDF-1.7\nfixture",
            expected_page_count=3,
        )

    assert document.closed is True


def test_empty_or_scanned_pdf_page_fails_closed_and_closes_resources() -> None:
    document = _FakeDocument(["可提取正文", " \r\n\t"])
    pdfium = _FakePdfium(document)
    extractor = AnnouncementDocumentTextExtractor(pdfium_loader=lambda: pdfium)

    with pytest.raises(DocumentTextIncompleteError, match=r"page 2.*OCR is not enabled"):
        extractor.extract_pdf(b"%PDF-1.7\nfixture")

    assert document.closed is True
    assert all(page.closed and page.text_page.closed for page in document.pages)


def test_missing_pdf_dependency_fails_closed() -> None:
    def missing_loader():
        raise ModuleNotFoundError("pypdfium2")

    extractor = AnnouncementDocumentTextExtractor(pdfium_loader=missing_loader)

    with pytest.raises(
        DocumentTextExtractionError,
        match="pypdfium2 is required",
    ) as error_info:
        extractor.extract_pdf(b"%PDF-1.7\nfixture")

    assert not isinstance(error_info.value, DocumentTextIncompleteError)


def test_unexpected_pdf_sdk_failure_is_not_document_quality_incomplete() -> None:
    class CrashingPdfium:
        @staticmethod
        def PdfDocument(value: bytes) -> _FakeDocument:
            del value
            raise RuntimeError("parser crashed")

    extractor = AnnouncementDocumentTextExtractor(pdfium_loader=CrashingPdfium)

    with pytest.raises(
        DocumentTextExtractionError,
        match="text-layer extraction failed",
    ) as error_info:
        extractor.extract_pdf(b"%PDF-1.7\nfixture")

    assert not isinstance(error_info.value, DocumentTextIncompleteError)


def test_non_pdf_bytes_are_rejected_before_loading_parser() -> None:
    loaded = False

    def loader():
        nonlocal loaded
        loaded = True
        return _FakePdfium(_FakeDocument(["正文"]))

    extractor = AnnouncementDocumentTextExtractor(pdfium_loader=loader)

    with pytest.raises(DocumentTextIncompleteError, match="not a PDF"):
        extractor.extract_pdf(b"not-pdf")

    assert loaded is False
