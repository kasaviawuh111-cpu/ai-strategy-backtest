from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.domain.market_data import EventEnvelope, MarketEvent, TimeQuality
from ashare_lab.domain.shared import InstrumentId, StrongId
from ashare_lab.domain.signals import SignalRuntime, SignalRuntimeError
from ashare_lab.domain.strategy import EventCondition, EventDocumentTextPredicate

from .conftest import make_bars

SHANGHAI = ZoneInfo("Asia/Shanghai")
INSTRUMENT = InstrumentId("300033.SZ")
EVENT_CODE = "event.financial_results.annual_report"


def _document_attributes(text: str) -> dict[str, str | int]:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    pages = [
        {
            "pageNumber": 1,
            "text": text,
            "textSha256": digest,
            "characterCount": len(text),
            "nonWhitespaceCharacterCount": sum(not character.isspace() for character in text),
        }
    ]
    return {
        "document_text": text,
        "document_text_sha256": digest,
        "document_text_source_sha256": "b" * 64,
        "document_text_quality": "complete_text_layer",
        "document_text_normalization": "unicode_nfkc_lf_strip.v1",
        "document_text_page_count": 1,
        "document_text_provider_page_count": 1,
        "document_text_extracted_page_count": 1,
        "document_text_empty_page_count": 0,
        "document_version_role": "initial_complete",
        "document_text_scope": "complete_primary_document",
        "document_text_source_format": "notice_content",
        "document_text_extractor": "eastmoney_notice_content",
        "document_text_extractor_version": "1",
        "document_text_character_count": len(text),
        "document_text_non_whitespace_character_count": sum(
            not character.isspace() for character in text
        ),
        "document_text_pages_json": json.dumps(
            pages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _event(attributes: dict[str, str | int]) -> EventEnvelope:
    available_at = datetime(2024, 1, 3, 8, 0, tzinfo=SHANGHAI)
    return EventEnvelope(
        event=MarketEvent(
            event_id=StrongId("annual-report-1"),
            event_code=EVENT_CODE,
            instrument_id=INSTRUMENT,
            attributes=attributes,
        ),
        occurred_at=None,
        source_released_at=available_at,
        vendor_first_available_at=available_at,
        ingested_at=available_at,
        revision_no=0,
        time_quality=TimeQuality.EXACT,
        source_event_id="AN-test-1",
        provider="eastmoney",
        source_url="https://example.test/annual-report-1.pdf",
        raw_response_sha256="a" * 64,
        validation_status="validated",
    )


def _condition(*, comparator: str = "gt", value: int = 5, term: str = "AI") -> EventCondition:
    return EventCondition(
        event_code=EVENT_CODE,
        definition_version="1.0.0",
        document_text=EventDocumentTextPredicate(
            term=term,
            match_mode="ascii_token" if term.isascii() else "literal",
            comparator=comparator,  # type: ignore[arg-type]
            value=value,
        ),
    )


def test_ascii_term_count_uses_token_boundaries_and_casefold() -> None:
    bars = make_bars([10, 11, 12], symbol=INSTRUMENT.value)
    text = "AI ai Ai AIGC XAI AI_模型 AI-driven"

    aligned = SignalRuntime().evaluate_aligned(
        _condition(comparator="gte", value=4),
        bars,
        (_event(_document_attributes(text)),),
    )

    fact = aligned[1]
    assert fact is not None and fact.triggered
    assert "literal_mention_count('AI')=[4] gte 4" in fact.reason


@pytest.mark.parametrize(
    ("comparator", "value", "triggered"),
    [("gt", 5, True), ("gt", 6, False), ("gte", 6, True), ("gte", 7, False)],
)
def test_document_comparison_boundary(
    comparator: str,
    value: int,
    triggered: bool,
) -> None:
    bars = make_bars([10, 11, 12], symbol=INSTRUMENT.value)
    text = " ".join(["AI"] * 6)

    aligned = SignalRuntime().evaluate_aligned(
        _condition(comparator=comparator, value=value),
        bars,
        (_event(_document_attributes(text)),),
    )

    assert any(fact is not None and fact.triggered for fact in aligned) is triggered


@pytest.mark.parametrize(
    "mutation",
    [
        {"document_text_quality": "scanned_without_ocr"},
        {"document_text_empty_page_count": 1},
        {"document_text_extracted_page_count": 1, "document_text_provider_page_count": 2},
        {"document_text_sha256": "c" * 64},
        {"document_version_role": "revision"},
        {"document_text_scope": "summary"},
    ],
)
def test_incomplete_or_mutated_document_fails_closed(
    mutation: dict[str, str | int],
) -> None:
    bars = make_bars([10, 11, 12], symbol=INSTRUMENT.value)
    attributes = _document_attributes("AI AI AI AI AI AI")
    attributes.update(mutation)

    with pytest.raises(SignalRuntimeError, match="document metric cannot be replayed"):
        SignalRuntime().evaluate_aligned(
            _condition(),
            bars,
            (_event(attributes),),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"document_text_source_format": "html"},
        {"document_text_extractor": "unknown_parser"},
        {"document_text_extractor_version": "2"},
        {"document_text_pages_json": "not-json"},
        {"document_text_character_count": 1},
        {"document_text_non_whitespace_character_count": 1},
    ],
)
def test_extractor_identity_and_page_provenance_fail_closed(
    mutation: dict[str, str | int],
) -> None:
    bars = make_bars([10, 11, 12], symbol=INSTRUMENT.value)
    attributes = _document_attributes("AI AI AI AI AI AI")
    attributes.update(mutation)

    with pytest.raises(SignalRuntimeError, match="document metric cannot be replayed"):
        SignalRuntime().evaluate_aligned(
            _condition(),
            bars,
            (_event(attributes),),
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("text", "AI AI AI AI AI"),
        ("textSha256", "c" * 64),
        ("pageNumber", 2),
        ("characterCount", 1),
        ("nonWhitespaceCharacterCount", 1),
    ],
)
def test_mutated_per_page_evidence_fails_closed(field: str, replacement: str | int) -> None:
    bars = make_bars([10, 11, 12], symbol=INSTRUMENT.value)
    attributes = _document_attributes("AI AI AI AI AI AI")
    raw_pages = attributes["document_text_pages_json"]
    assert isinstance(raw_pages, str)
    decoded = cast(object, json.loads(raw_pages))
    assert isinstance(decoded, list)
    pages = cast(list[dict[str, object]], decoded)
    assert isinstance(pages[0], dict)
    pages[0][field] = replacement
    attributes["document_text_pages_json"] = json.dumps(
        pages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(SignalRuntimeError, match="document metric cannot be replayed"):
        SignalRuntime().evaluate_aligned(
            _condition(),
            bars,
            (_event(attributes),),
        )


def test_pdf_provenance_requires_the_frozen_library_version() -> None:
    bars = make_bars([10, 11, 12], symbol=INSTRUMENT.value)
    attributes = _document_attributes("AI AI AI AI AI AI")
    attributes.update(
        {
            "document_text_source_format": "pdf",
            "document_text_extractor": "pypdfium2_text_layer",
        }
    )

    with pytest.raises(SignalRuntimeError, match="document metric cannot be replayed"):
        SignalRuntime().evaluate_aligned(
            _condition(),
            bars,
            (_event(attributes),),
        )

    attributes["document_text_extractor_library_version"] = "5.13.0"
    aligned = SignalRuntime().evaluate_aligned(
        _condition(),
        bars,
        (_event(attributes),),
    )

    assert any(fact is not None and fact.triggered for fact in aligned)


def test_chinese_literal_is_counted_without_ai_alias_expansion() -> None:
    bars = make_bars([10, 11, 12], symbol=INSTRUMENT.value)
    text = "人工智能 AI 人工智能 大模型"

    aligned = SignalRuntime().evaluate_aligned(
        _condition(comparator="gte", value=2, term="人工智能"),
        bars,
        (_event(_document_attributes(text)),),
    )

    assert any(fact is not None and fact.triggered for fact in aligned)
