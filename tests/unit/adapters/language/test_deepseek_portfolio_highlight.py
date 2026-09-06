from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from ashare_lab.adapters.language.deepseek_portfolio_highlight import (
    DeepSeekPortfolioHighlightNarrator,
    PortfolioNarrativeUnavailable,
)
from ashare_lab.ports.portfolio_highlight_narrative import (
    DriverConfidence,
    VerifiedPerformanceEvidence,
    VerifiedPortfolioHighlight,
)


def _highlight() -> VerifiedPortfolioHighlight:
    return VerifiedPortfolioHighlight(
        symbol="600519.SH",
        name="贵州茅台",
        market="CN_A",
        action="买入后持有",
        occurred_at=datetime(2026, 8, 3, 10, 30, tzinfo=UTC),
        performance_evidence=(
            VerifiedPerformanceEvidence(
                evidence_id="ledger_42",
                statement="2026-08-03 至 2026-08-10 持有区间收益率 +6.2%",
            ),
        ),
    )


def _payload() -> dict[str, object]:
    return {
        "likely_drivers": [
            {
                "reason": "最可能：事件窗口内公司披露的经营信息改善了市场情绪。",
                "confidence": "medium",
                "source_ids": ["src_1"],
            }
        ],
        "sources": [
            {
                "source_id": "src_1",
                "title": "公司经营数据公告",
                "url": "https://example.com/announcement",
                "publisher": "交易所",
                "published_at": "2026-08-06T09:00:00+08:00",
            }
        ],
        "unresolved": ["无法仅凭公开信息确认单一因果。"],
    }


def _response(payload: dict[str, object], *, with_search: bool = True) -> bytes:
    output: list[dict[str, object]] = []
    if with_search:
        output.append({"type": "web_search_call", "status": "completed", "id": "ws_1"})
    output.append(
        {
            "type": "message",
            "status": "completed",
            "id": "msg_1",
            "content": [
                {
                    "type": "output_text",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ],
        }
    )
    return json.dumps(
        {
            "id": "resp_highlight_1",
            "status": "completed",
            "model": "deepseek-v4-flash",
            "output": output,
        },
        ensure_ascii=False,
    ).encode()


@pytest.mark.asyncio
async def test_forces_web_search_and_returns_only_sourced_likely_drivers() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.deepseek.com/responses")
        assert request.headers["Authorization"] == "Bearer server-secret"
        body = json.loads(request.content)
        assert body["tools"] == [{"type": "web_search"}]
        assert body["tool_choice"] == {"type": "web_search"}
        assert body["text"]["format"]["type"] == "json_schema"
        assert set(body["text"]["format"]["schema"]["properties"]) == {
            "likely_drivers",
            "sources",
            "unresolved",
        }
        assert "server-secret" not in request.content.decode()
        user_payload = json.loads(body["input"][1]["content"])
        assert user_payload == {
            "symbol": "600519.SH",
            "name": "贵州茅台",
            "market": "CN_A",
            "action": "买入后持有",
            "time": "2026-08-03T10:30:00+00:00",
            "performanceEvidence": [
                {
                    "evidenceId": "ledger_42",
                    "statement": "2026-08-03 至 2026-08-10 持有区间收益率 +6.2%",
                }
            ],
        }
        return httpx.Response(200, content=_response(_payload()))

    narrator = DeepSeekPortfolioHighlightNarrator(
        api_key=SecretStr("server-secret"),
        transport=httpx.MockTransport(handler),
    )

    result = await narrator.narrate(_highlight())

    assert result.likely_drivers[0].reason.startswith("最可能：")
    assert result.likely_drivers[0].confidence is DriverConfidence.MEDIUM
    assert result.likely_drivers[0].source_ids == ("src_1",)
    assert result.sources[0].url == "https://example.com/announcement"
    assert result.investment_advice is None
    assert result.signal_records == ()


@pytest.mark.asyncio
async def test_rejects_missing_search_invalid_source_and_unlabelled_reason() -> None:
    bad_source = _payload()
    bad_source["likely_drivers"] = [
        {
            "reason": "推断：事件窗口内的公开信息可能影响了情绪。",
            "confidence": "low",
            "source_ids": ["src_missing"],
        }
    ]
    unlabelled = _payload()
    unlabelled["likely_drivers"] = [
        {
            "reason": "事件窗口内的公开信息可能影响了情绪。",
            "confidence": "low",
            "source_ids": ["src_1"],
        }
    ]
    responses = iter(
        (
            _response(_payload(), with_search=False),
            _response(bad_source),
            _response(unlabelled),
        )
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=next(responses))

    narrator = DeepSeekPortfolioHighlightNarrator(
        api_key=SecretStr("server-secret"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PortfolioNarrativeUnavailable, match="search evidence"):
        await narrator.narrate(_highlight())
    with pytest.raises(PortfolioNarrativeUnavailable, match="source references"):
        await narrator.narrate(_highlight())
    with pytest.raises(PortfolioNarrativeUnavailable, match="schema validation"):
        await narrator.narrate(_highlight())


@pytest.mark.asyncio
async def test_source_free_claim_must_be_unresolved_and_advice_is_rejected() -> None:
    unresolved_only = _payload()
    unresolved_only["likely_drivers"] = []
    unresolved_only["sources"] = []
    advice = _payload()
    advice["unresolved"] = ["建议立即加仓。"]
    responses = iter((_response(unresolved_only), _response(advice)))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=next(responses))

    narrator = DeepSeekPortfolioHighlightNarrator(
        api_key=SecretStr("server-secret"),
        transport=httpx.MockTransport(handler),
    )

    result = await narrator.narrate(_highlight())
    assert result.likely_drivers == ()
    assert result.sources == ()
    assert result.unresolved
    with pytest.raises(PortfolioNarrativeUnavailable, match="investment advice"):
        await narrator.narrate(_highlight())


@pytest.mark.asyncio
async def test_rejects_account_scoped_performance_and_actions() -> None:
    invented_number = _payload()
    invented_number["unresolved"] = ["账户赚了 999999 元。"]
    conflicting_action = _payload()
    conflicting_action["unresolved"] = ["用户当时买入了这只股票。"]
    responses = iter((_response(invented_number), _response(conflicting_action)))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=next(responses))

    narrator = DeepSeekPortfolioHighlightNarrator(
        api_key=SecretStr("server-secret"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PortfolioNarrativeUnavailable, match="account performance"):
        await narrator.narrate(_highlight())
    with pytest.raises(PortfolioNarrativeUnavailable, match="account actions"):
        await narrator.narrate(_highlight())


@pytest.mark.asyncio
async def test_allows_sourced_market_numbers_and_third_party_market_actions() -> None:
    market_context = _payload()
    market_context["likely_drivers"] = [
        {
            "reason": (
                "最可能：公告显示营收同比增长 12%，北向资金当日买入规模扩大。"
            ),
            "confidence": "medium",
            "source_ids": ["src_1"],
        }
    ]
    market_context["unresolved"] = ["无法确认 2026 年 8 月的公开信息是否构成单一因果。"]

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_response(market_context))

    narrator = DeepSeekPortfolioHighlightNarrator(
        api_key=SecretStr("server-secret"),
        transport=httpx.MockTransport(handler),
    )

    result = await narrator.narrate(_highlight())

    assert "12%" in result.likely_drivers[0].reason
    assert "北向资金当日买入" in result.likely_drivers[0].reason
    assert "2026" in result.unresolved[0]


@pytest.mark.asyncio
async def test_provider_failure_never_logs_secret_or_response_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"provider-private-body")

    narrator = DeepSeekPortfolioHighlightNarrator(
        api_key=SecretStr("server-private-key"),
        transport=httpx.MockTransport(handler),
    )

    with (
        caplog.at_level(logging.INFO),
        pytest.raises(PortfolioNarrativeUnavailable, match="provider request failed"),
    ):
        await narrator.narrate(_highlight())

    assert "provider-private-body" not in caplog.text
    assert "server-private-key" not in caplog.text


def test_verified_highlight_requires_timezone_and_performance_evidence() -> None:
    with pytest.raises(ValueError, match="explicit timezone"):
        VerifiedPortfolioHighlight(
            symbol="600519.SH",
            name="贵州茅台",
            market="CN_A",
            action="买入",
            occurred_at=datetime(2026, 8, 3, 10, 30),
            performance_evidence=(
                VerifiedPerformanceEvidence("ledger_1", "收益率 +1%"),
            ),
        )
    with pytest.raises(ValueError, match="cannot be empty"):
        VerifiedPortfolioHighlight(
            symbol="600519.SH",
            name="贵州茅台",
            market="CN_A",
            action="买入",
            occurred_at=datetime(2026, 8, 3, 10, 30, tzinfo=UTC),
            performance_evidence=(),
        )
