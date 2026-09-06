"""Stateless portfolio-import, review, and on-demand narrative endpoints."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time
from typing import cast

from fastapi import APIRouter, Request, status

from ashare_lab.adapters.language.deepseek_portfolio_highlight import (
    DeepSeekPortfolioHighlightNarrator,
    PortfolioNarrativeUnavailable,
    validate_model_narrative_safety,
)
from ashare_lab.application.portfolio_review import (
    PortfolioReviewInputError,
    analyze_portfolio_review,
    build_verified_highlight,
)
from ashare_lab.ports.market_data import MarketDataRepository
from ashare_lab.ports.portfolio_highlight_narrative import (
    PortfolioHighlightNarrative,
    PortfolioHighlightNarrator,
    VerifiedPortfolioHighlight,
)

from ..errors import ApiProblem, ErrorDetail
from ..portfolio_review_schemas import (
    BrokerLedgerParseRequest,
    BrokerLedgerParseResponse,
    NarrativeLikelyDriverPayload,
    NarrativeSourcePayload,
    PortfolioHighlightNarrationRequest,
    PortfolioHighlightNarrationResponse,
    PortfolioImportContractResponse,
    PortfolioReviewRequest,
    PortfolioReviewResponse,
    portfolio_import_contract,
)
from ..schemas import error_response_docs

_LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/portfolio-reviews", tags=["portfolio-reviews"])


@router.post(
    "/imports/parse",
    response_model=BrokerLedgerParseResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_200_OK,
    operation_id="parsePortfolioBrokerLedger",
    responses=error_response_docs(413, 422, 500),
)
def parse_broker_ledger(body: BrokerLedgerParseRequest) -> BrokerLedgerParseResponse:
    """Infer a common editable draft from browser-decoded CSV/XLSX rows."""

    try:
        inferred = body.infer_draft()
        return BrokerLedgerParseResponse.model_validate(inferred)
    except PortfolioReviewInputError as exc:
        raise _inconsistent_records(exc) from exc


@router.post(
    "/analyze",
    response_model=PortfolioReviewResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_200_OK,
    operation_id="analyzePortfolioReview",
    responses=error_response_docs(413, 422, 500),
)
def analyze_review(body: PortfolioReviewRequest, request: Request) -> PortfolioReviewResponse:
    """Analyze exactly the confirmed evidence supplied; unsupported outputs stay absent."""

    try:
        analysis = analyze_portfolio_review(body.to_application_input())
    except PortfolioReviewInputError as exc:
        raise _inconsistent_records(exc) from exc
    narrative_available = _configured_narrator(request) is not None
    analysis.payload["narrative_status"] = {
        "available": narrative_available,
        "mode": "on_demand",
        "reason": (
            "configured; generated only when a verified highlight is opened"
            if narrative_available
            else "server-side research API key is not configured"
        ),
    }
    return PortfolioReviewResponse.model_validate(analysis.payload)


@router.post(
    "/narrate-highlight",
    response_model=PortfolioHighlightNarrationResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_200_OK,
    operation_id="narratePortfolioHighlight",
    responses=error_response_docs(413, 422, 500, 503),
)
async def narrate_highlight(
    body: PortfolioHighlightNarrationRequest,
    request: Request,
) -> PortfolioHighlightNarrationResponse:
    """Research one ledger-verified highlight only when the user opens it."""

    narrator = _configured_narrator(request)
    if narrator is None:
        raise ApiProblem(
            status_code=503,
            code="portfolio_narrative_not_configured",
            message="Portfolio highlight narrative requires a server-side research API key",
        )

    source = body.review.to_application_input()
    try:
        verified = build_verified_highlight(source, body.highlight_index)
    except PortfolioReviewInputError as exc:
        raise _inconsistent_records(exc) from exc

    if verified.market == "CN_A":
        compiler = request.app.state.container.compiler
        resolved = await compiler.resolve_instrument_context(verified.symbol)
        if resolved != verified.symbol:
            raise ApiProblem(
                status_code=422,
                code="portfolio_review_security_normalization_failed",
                message="Portfolio highlight security identity could not be normalized",
            )

    market_data = _configured_market_data(request)
    if market_data is not None and verified.market == "CN_A":
        try:
            verified = await asyncio.to_thread(
                build_verified_highlight,
                source,
                body.highlight_index,
                market_data=market_data,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            # Market evidence is optional; the ledger fact remains usable.
            _LOGGER.info(
                "portfolio daily market evidence unavailable type=%s",
                type(exc).__name__,
            )

    try:
        narrative = await narrator.narrate(verified)
        validate_model_narrative_safety(narrative)
        _validate_narrative_point_in_time(narrative, known_as_of=verified.occurred_at)
    except PortfolioNarrativeUnavailable as exc:
        raise ApiProblem(
            status_code=503,
            code="portfolio_narrative_unavailable",
            message="Portfolio highlight narrative is temporarily unavailable",
        ) from exc

    headline, empathetic_summary = _verified_highlight_copy(verified)
    return PortfolioHighlightNarrationResponse(
        highlight_index=body.highlight_index,
        headline=headline,
        empathetic_summary=empathetic_summary,
        likely_drivers=tuple(
            NarrativeLikelyDriverPayload(
                reason=item.reason,
                confidence=item.confidence.value,
                source_ids=item.source_ids,
            )
            for item in narrative.likely_drivers
        ),
        sources=tuple(
            NarrativeSourcePayload(
                source_id=item.source_id,
                title=item.title,
                url=item.url,
                publisher=item.publisher,
                published_at=item.published_at,
            )
            for item in narrative.sources
        ),
        unresolved=narrative.unresolved,
        historical_market_evidence_count=max(0, len(verified.performance_evidence) - 1),
    )


@router.get(
    "/import-contract",
    response_model=PortfolioImportContractResponse,
    status_code=status.HTTP_200_OK,
    operation_id="getPortfolioReviewImportContract",
    responses=error_response_docs(500),
)
def get_import_contract() -> PortfolioImportContractResponse:
    return portfolio_import_contract()


def _configured_narrator(request: Request) -> PortfolioHighlightNarrator | None:
    override = getattr(request.app.state, "portfolio_highlight_narrator", None)
    if override is not None:
        return cast(PortfolioHighlightNarrator, override)
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        return None
    settings = runtime.execution.settings
    endpoint = settings.research_provider_endpoint
    model = settings.research_provider_model
    api_key = settings.research_provider_api_key
    if endpoint is None or model is None or api_key is None:
        return None
    try:
        return DeepSeekPortfolioHighlightNarrator(
            api_key=api_key,
            endpoint=str(endpoint),
            model=model,
            timeout_seconds=settings.research_provider_timeout_seconds,
        )
    except ValueError:
        return None


def _configured_market_data(request: Request) -> MarketDataRepository | None:
    override = getattr(request.app.state, "portfolio_market_data", None)
    if override is not None:
        return cast(MarketDataRepository, override)
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        return None
    return cast(MarketDataRepository, runtime.execution.market_data)


def _verified_highlight_copy(verified: VerifiedPortfolioHighlight) -> tuple[str, str]:
    """Render account copy without allowing the narrator to author ledger facts."""

    # ``verified`` is produced locally by build_verified_highlight.  Keeping
    # this formatting in the route makes the trust boundary explicit even for
    # a custom/test narrator implementation.
    headline = f"{verified.name} · {verified.action}"
    fact_sentences = " ".join(
        _sentence(item.statement) for item in verified.performance_evidence
    )
    summary = (
        f"{verified.occurred_at.date().isoformat()}，账户记录为“{verified.action}”。"
        f"{fact_sentences} "
        "这段操作值得回看；公开背景只用于理解当时可能的市场环境，"
        "不改写账户事实，也不构成投资建议。"
    )
    return headline, summary


def _sentence(value: str) -> str:
    normalized = value.strip()
    if normalized.endswith(("。", "！", "？", ".", "!", "?")):
        return normalized
    return f"{normalized}。"


def _validate_narrative_point_in_time(
    narrative: PortfolioHighlightNarrative,
    *,
    known_as_of: datetime,
) -> None:
    """Reject hindsight sources programmatically; prompt instructions are not a gate."""

    for source in narrative.sources:
        published_at = _source_published_at(source.published_at, known_as_of=known_as_of)
        if published_at is None or published_at > known_as_of:
            raise PortfolioNarrativeUnavailable("narrative source violates point-in-time cutoff")


def _source_published_at(value: str | None, *, known_as_of: datetime) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    if len(normalized) == 10:
        try:
            published_date = date.fromisoformat(normalized)
        except ValueError:
            return None
        # A date-only source on the event date could have been published after
        # the trade.  Earlier dates are known by the start of the event date.
        if published_date >= known_as_of.date():
            return None
        return datetime.combine(published_date, time(0), tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _inconsistent_records(exc: PortfolioReviewInputError) -> ApiProblem:
    return ApiProblem(
        status_code=422,
        code="portfolio_review_records_inconsistent",
        message="Portfolio review records are inconsistent",
        details=(ErrorDetail(location="body", message=str(exc), type="value_error"),),
    )
