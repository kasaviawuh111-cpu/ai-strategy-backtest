"""Deterministic document metrics for frozen A-share event artifacts.

This module deliberately evaluates only already-extracted, content-addressed
text stored in an immutable event snapshot.  It never downloads a document,
runs OCR, or asks a language model to count terms during a backtest.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from ashare_lab.domain.strategy.models import EventDocumentTextPredicate

_COMPLETE_QUALITY = "complete_text_layer"
_EXTRACTOR_NORMALIZATION = "unicode_nfkc_lf_strip.v1"
_PAGE_SEPARATOR = "\n\f\n"
_HEX = frozenset("0123456789abcdef")
_EXTRACTOR_BY_SOURCE_FORMAT = {
    "notice_content": ("eastmoney_notice_content", "1"),
    "pdf": ("pypdfium2_text_layer", "1"),
}


class DocumentMetricError(ValueError):
    """A frozen event does not contain a deterministically replayable text artifact."""


@dataclass(frozen=True, slots=True)
class DocumentMetricResult:
    count: int
    matched: bool


@dataclass(frozen=True, slots=True)
class ValidatedDocumentTextArtifact:
    """Frozen document text proven replayable by the canonical artifact contract."""

    text: str
    text_sha256: str
    source_sha256: str


def validate_document_text_artifact(
    attributes: Mapping[str, str | int | Decimal | bool | None],
    *,
    expected_source_sha256: str | None = None,
) -> ValidatedDocumentTextArtifact:
    """Validate the complete immutable document artifact, or fail closed.

    This is deliberately the one validator used both by runtime metric
    evaluation and by snapshot preflight.  Keeping the extractor identity,
    page evidence and text-rebuild rules here prevents a snapshot from passing
    preparation under a weaker contract than the worker later enforces.
    """

    text = _required_text(attributes, "document_text")
    text_sha256 = _required_sha256(attributes, "document_text_sha256")
    source_sha256 = _required_sha256(attributes, "document_text_source_sha256")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256:
        raise DocumentMetricError("document text hash does not match frozen text")
    if expected_source_sha256 is not None:
        expected_digest = _normalized_sha256(expected_source_sha256, "expected_source_sha256")
        if source_sha256 != expected_digest:
            raise DocumentMetricError(
                "document text source hash does not match frozen source document"
            )
    if _required_text(attributes, "document_text_quality") != _COMPLETE_QUALITY:
        raise DocumentMetricError("document text quality is not complete_text_layer")
    if _required_text(attributes, "document_text_normalization") != _EXTRACTOR_NORMALIZATION:
        raise DocumentMetricError("document text normalization is unsupported")
    if _required_text(attributes, "document_version_role") != "initial_complete":
        raise DocumentMetricError("document is not the initial complete report version")
    if _required_text(attributes, "document_text_scope") != "complete_primary_document":
        raise DocumentMetricError("document text is not the complete primary document")

    source_format = _required_text(attributes, "document_text_source_format")
    extractor_identity = _EXTRACTOR_BY_SOURCE_FORMAT.get(source_format)
    if extractor_identity is None:
        raise DocumentMetricError("document text source format is unsupported")
    extractor_name, extractor_version = extractor_identity
    if _required_text(attributes, "document_text_extractor") != extractor_name:
        raise DocumentMetricError("document text extractor identity is unsupported")
    if _required_text(attributes, "document_text_extractor_version") != extractor_version:
        raise DocumentMetricError("document text extractor version is unsupported")
    if source_format == "pdf":
        _required_text(attributes, "document_text_extractor_library_version")

    provider_pages = _required_positive_int(attributes, "document_text_provider_page_count")
    extracted_pages = _required_positive_int(attributes, "document_text_extracted_page_count")
    page_count = _required_positive_int(attributes, "document_text_page_count")
    empty_pages = _required_non_negative_int(attributes, "document_text_empty_page_count")
    if provider_pages != extracted_pages or extracted_pages != page_count or empty_pages != 0:
        raise DocumentMetricError("document text does not cover every provider page")
    if source_format == "notice_content" and page_count != 1:
        raise DocumentMetricError("notice-content text must contain exactly one provider page")

    pages = _validated_pages(attributes, expected_page_count=page_count)
    if _PAGE_SEPARATOR.join(pages) != text:
        raise DocumentMetricError("document page text does not rebuild the frozen document text")
    if _required_positive_int(attributes, "document_text_character_count") != len(text):
        raise DocumentMetricError("document text character count does not match frozen text")
    if _required_positive_int(
        attributes,
        "document_text_non_whitespace_character_count",
    ) != sum(not character.isspace() for character in text):
        raise DocumentMetricError("document text non-whitespace count does not match frozen text")

    return ValidatedDocumentTextArtifact(
        text=text,
        text_sha256=text_sha256,
        source_sha256=source_sha256,
    )


def evaluate_document_text_predicate(
    predicate: EventDocumentTextPredicate,
    attributes: Mapping[str, str | int | Decimal | bool | None],
) -> DocumentMetricResult:
    """Count a literal term and apply the frozen comparison, or fail closed."""

    text = validate_document_text_artifact(attributes).text

    normalized_text = unicodedata.normalize("NFKC", text)
    normalized_term = unicodedata.normalize("NFKC", predicate.term)
    if not predicate.case_sensitive:
        normalized_text = normalized_text.casefold()
        normalized_term = normalized_term.casefold()

    if predicate.match_mode == "ascii_token":
        # ASCII word boundaries are explicit rather than Python ``\b`` so
        # Chinese adjacency does not turn ``AI`` into a partial ``AIGC`` hit.
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(normalized_term)}(?![A-Za-z0-9_])")
        count = sum(1 for _ in pattern.finditer(normalized_text))
    else:
        count = normalized_text.count(normalized_term)

    matched = {
        "gt": count > predicate.value,
        "gte": count >= predicate.value,
        "eq": count == predicate.value,
        "lte": count <= predicate.value,
        "lt": count < predicate.value,
    }[predicate.comparator]
    return DocumentMetricResult(count=count, matched=matched)


def _required_text(
    values: Mapping[str, str | int | Decimal | bool | None],
    name: str,
) -> str:
    value = values.get(name)
    if type(value) is not str or not value:
        raise DocumentMetricError(f"event document attribute {name!r} is missing")
    return value


def _required_sha256(
    values: Mapping[str, str | int | Decimal | bool | None],
    name: str,
) -> str:
    return _normalized_sha256(values.get(name), name)


def _normalized_sha256(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise DocumentMetricError(f"event document attribute {name!r} is missing")
    digest = value.lower().removeprefix("sha256:")
    if len(digest) != 64 or any(character not in _HEX for character in digest):
        raise DocumentMetricError(f"event document attribute {name!r} is not SHA-256")
    return digest


def _required_positive_int(
    values: Mapping[str, str | int | Decimal | bool | None],
    name: str,
) -> int:
    value = values.get(name)
    if type(value) is not int or value <= 0:
        raise DocumentMetricError(f"event document attribute {name!r} must be positive")
    return value


def _required_non_negative_int(
    values: Mapping[str, str | int | Decimal | bool | None],
    name: str,
) -> int:
    value = values.get(name)
    if type(value) is not int or value < 0:
        raise DocumentMetricError(f"event document attribute {name!r} must be non-negative")
    return value


def _validated_pages(
    attributes: Mapping[str, str | int | Decimal | bool | None],
    *,
    expected_page_count: int,
) -> tuple[str, ...]:
    raw_pages = _required_text(attributes, "document_text_pages_json")
    try:
        decoded = cast(object, json.loads(raw_pages))
    except json.JSONDecodeError as exc:
        raise DocumentMetricError("document page evidence is not valid JSON") from exc
    if not isinstance(decoded, list):
        raise DocumentMetricError("document page evidence count does not match provider pages")
    raw_page_items = cast(list[object], decoded)
    if len(raw_page_items) != expected_page_count:
        raise DocumentMetricError("document page evidence count does not match provider pages")

    texts: list[str] = []
    required_keys = {
        "pageNumber",
        "text",
        "textSha256",
        "characterCount",
        "nonWhitespaceCharacterCount",
    }
    for expected_number, raw_page in enumerate(raw_page_items, start=1):
        if not isinstance(raw_page, dict):
            raise DocumentMetricError("document page evidence has an unsupported shape")
        untyped_page = cast(dict[object, object], raw_page)
        if any(not isinstance(key, str) for key in untyped_page):
            raise DocumentMetricError("document page evidence has an unsupported shape")
        page = cast(dict[str, object], untyped_page)
        if set(page) != required_keys:
            raise DocumentMetricError("document page evidence has an unsupported shape")
        if type(page["pageNumber"]) is not int or page["pageNumber"] != expected_number:
            raise DocumentMetricError("document page numbers are not contiguous")
        text = page["text"]
        if type(text) is not str or not text.strip():
            raise DocumentMetricError("document page evidence contains no extractable text")
        digest = page["textSha256"]
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in _HEX for character in digest)
        ):
            raise DocumentMetricError("document page text hash is not SHA-256")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
            raise DocumentMetricError("document page text hash does not match page text")
        character_count = page["characterCount"]
        if type(character_count) is not int or character_count != len(text):
            raise DocumentMetricError("document page character count does not match page text")
        non_whitespace_count = page["nonWhitespaceCharacterCount"]
        if type(non_whitespace_count) is not int or non_whitespace_count != sum(
            not character.isspace() for character in text
        ):
            raise DocumentMetricError("document page non-whitespace count does not match page text")
        texts.append(text)
    return tuple(texts)
