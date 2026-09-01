# Starlette's TestClient currently exposes partially unknown httpx method types to Pyright.
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import HybridCandidateGenerator
from ashare_lab.api import create_app
from ashare_lab.api.schemas import StrategyDraftResponse
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import StrategySpec, canonical_hash
from ashare_lab.ports.candidate_generation import CandidateAst, CompileInput
from ashare_lab.ports.idea_routing import IdeaAssetMapping, IdeaProposal, IdeaRoute

ROOT = Path(__file__).resolve().parents[3]


class _UnexpectedFallback:
    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        raise AssertionError(f"bounded fallback must not run: {request!r}")


class _ExplodingCompiler:
    async def compile(self, _request: Any) -> Any:
        raise RuntimeError("internal secret must not leak")


class _IdeaCompiler:
    async def compile(self, _request: Any) -> CompileOutcome:
        proposals = tuple(
            IdeaProposal(
                id=f"idea_{index:012x}",
                title=f"候选 {index}",
                hypothesis="仅用价格行为代理检验该观点。",
                entry_summary="MACD 金叉",
                exit_summary="MACD 死叉",
                suggested_utterance="MACD金叉买入，死叉卖出，回测近5年",
                capability_ids=("technical.macd",),
                assumptions=("不证明因果关系。",),
                confidence=0.75,
            )
            for index in range(1, 3)
        )
        return CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            clarification="请选择一个可回测的价格代理。",
            diagnostic_code="idea_guidance_required",
            idea_route=IdeaRoute(
                understanding="用户表达了一个政治态度。",
                hypothesis="相关不确定性可能与当前股票价格行为同期出现。",
                asset_mapping=IdeaAssetMapping(
                    instrument_symbol="300059.SZ",
                    rationale="只使用当前页面股票作为价格代理。",
                ),
                proposals=proposals,
            ),
        )


def test_ready_draft_returns_canonical_strategy_hash_and_provenance(
    client: TestClient,
    ready_request: dict[str, str],
) -> None:
    response = client.post("/api/v1/strategy-drafts", json=ready_request)
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert response.headers["Idempotency-Replayed"] == "false"
    assert payload["revision"] == 1
    assert payload["status"] == "ready"
    assert payload["strategy"]["schema_version"] == "strategy.v1"
    assert payload["strategy"]["instrument"]["symbol"] == "300059.SZ"
    assert payload["strategy_hash"].startswith("sha256:")
    assert {item["path"] for item in payload["provenance"]} >= {
        "/instrument/symbol",
        "/backtest/start",
        "/backtest/end",
    }


def test_idea_guidance_is_typed_and_remains_non_executable() -> None:
    app = create_app()
    app.state.container = replace(app.state.container, compiler=_IdeaCompiler())
    with TestClient(app) as idea_client:
        response = idea_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "我讨厌特朗普",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-30",
            },
        )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "needs_clarification"
    assert payload["diagnostic_code"] == "idea_guidance_required"
    assert payload["strategy"] is None
    assert payload["strategy_hash"] is None
    assert payload["idea_route"]["schema_version"] == "idea-route.v1"
    assert payload["idea_route"]["asset_mapping"] == {
        "instrument_symbol": "300059.SZ",
        "relation": "current_page_proxy",
        "rationale": "只使用当前页面股票作为价格代理。",
        "evidence_status": "host_context_only",
    }
    assert len(payload["idea_route"]["proposals"]) == 2
    assert all(
        item["suggested_utterance"] == "MACD金叉买入，死叉卖出，回测近5年"
        for item in payload["idea_route"]["proposals"]
    )
    with pytest.raises(ValidationError):
        StrategyDraftResponse.model_validate({**payload, "idea_route": None})
    with pytest.raises(ValidationError):
        StrategyDraftResponse.model_validate(
            {**payload, "diagnostic_code": "non_daily_timeframe_not_supported"}
        )
    StrategyDraftResponse.model_validate({**payload, "diagnostic_code": "strategy_rule_incomplete"})
    StrategyDraftResponse.model_validate(
        {**payload, "diagnostic_code": "ambiguous_cross_indicator"}
    )


def test_bare_cross_api_preserves_instrument_and_clarification_grounding() -> None:
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnexpectedFallback(),
        instrument_name_resolver=lambda name: {"汤姆猫": "300459.SZ"}[name],
    )
    compiler = StrategyCompiler(
        generator=generator,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )
    app = create_app()
    app.state.container = replace(app.state.container, compiler=compiler)

    with TestClient(app) as grounded_client:
        response = grounded_client.post(
            "/api/v1/strategy-drafts",
            json={"utterance": "汤姆猫金叉买死叉卖", "as_of_date": "2026-08-20"},
        )

    assert response.status_code == 201
    payload: dict[str, Any] = response.json()
    assert payload["status"] == "needs_clarification"
    assert payload["diagnostic_code"] == "ambiguous_cross_indicator"
    assert payload["idea_route"]["asset_mapping"]["instrument_symbol"] == "300459.SZ"
    assert payload["candidate_grounding"]["matched_spans"] == ["汤姆猫", "汤姆猫金叉"]
    assert [(item["path"], item["text"]) for item in payload["candidate_grounding"]["spans"]] == [
        ("/instrument/symbol", "汤姆猫"),
        ("/clarification", "汤姆猫金叉"),
    ]


def test_instrument_context_rejects_a_different_symbol_named_in_the_utterance(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "贵州茅台600519.SS的MACD金叉买入，死叉卖出",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-30",
        },
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "unsupported"
    assert payload["diagnostic_code"] == "instrument_context_mismatch"
    assert payload["strategy"] is None


def test_report_term_count_and_fill_anchored_exit_survive_the_http_contract(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "同花顺发年报提到ai次数超过5次的话就买入，3天后卖出",
            "instrument_context": "300033.SZ",
            "as_of_date": "2026-08-30",
        },
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "ready"
    assert payload["strategy"]["instrument"]["symbol"] == "300033.SZ"
    assert payload["strategy"]["entry"]["event_code"] == ("event.financial_results.annual_report")
    assert payload["strategy"]["entry"]["document_text"] == {
        "metric_id": "document.literal_mention_count",
        "metric_version": "1.0.0",
        "term": "ai",
        "normalization": "nfkc",
        "match_mode": "ascii_token",
        "case_sensitive": False,
        "comparator": "gt",
        "value": 5,
    }
    assert payload["strategy"]["exit"]["children"] == [
        {
            "type": "holding_period_exit",
            "sessions": 3,
            "anchor": "first_entry_fill",
            "count_mode": "subsequent_trading_sessions",
            "execution": "target_session_open_proxy",
        }
    ]


def test_boolean_connectives_change_the_strategy_shape_and_hash(client: TestClient) -> None:
    common = {
        "instrument_context": "300059.SZ",
        "as_of_date": "2026-08-27",
    }
    all_response = client.post(
        "/api/v1/strategy-drafts",
        json={
            **common,
            "utterance": "MACD金叉并且RSI低于30买入，MACD死叉卖出",
        },
    )
    any_response = client.post(
        "/api/v1/strategy-drafts",
        json={
            **common,
            "utterance": "MACD金叉或者RSI低于30买入，MACD死叉卖出",
        },
    )

    assert all_response.status_code == any_response.status_code == 201
    assert all_response.json()["status"] == any_response.json()["status"] == "ready"
    assert all_response.json()["strategy"]["entry"]["type"] == "all"
    assert any_response.json()["strategy"]["entry"]["type"] == "any"
    assert all_response.json()["strategy_hash"] != any_response.json()["strategy_hash"]


def test_unsupported_position_aware_exit_and_is_explicit_in_http_contract(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "MACD金叉买入，止盈20%且止损5%卖出",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "unsupported"
    assert payload["strategy"] is None
    assert payload["diagnostic_code"] == "position_aware_exit_and_not_supported"
    assert "同时满足才卖出" in payload["clarification"]


@pytest.mark.parametrize(
    ("utterance", "diagnostic_code"),
    [
        ("5分钟MACD金叉买入，5分钟死叉卖出", "non_daily_timeframe_not_supported"),
        ("30min MACD金叉买入，30min MACD死叉卖出", "non_daily_timeframe_not_supported"),
        (
            "MACD金叉当天收盘买入，死叉当天收盘卖出",
            "same_session_execution_not_supported",
        ),
        (
            "MACD金叉后下一交易日收盘买入，死叉后下一交易日收盘卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉后第二天收盘买入，MACD死叉卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉后第二个交易日收盘买入，MACD死叉卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉三天后买入，MACD死叉卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉后3个交易日买入，MACD死叉卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉买入，MACD死叉三天后卖出",
            "execution_price_time_not_supported",
        ),
        (
            "业绩预告亏损后买入，MACD死叉卖出",
            "event_attribute_filter_not_supported",
        ),
        (
            "业绩预告利润大于1000万元后买入，MACD死叉卖出",
            "event_attribute_filter_not_supported",
        ),
        (
            "定期报告净利润增长超过30%后买入，MACD死叉卖出",
            "event_attribute_filter_not_supported",
        ),
        (
            "一季报发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "2024年报发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "2024年度报告发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "今年年报发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "去年的年度报告发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "24年报发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "MACD在零轴上方金叉买入，MACD死叉卖出",
            "technical_qualifier_not_supported",
        ),
    ],
)
def test_http_draft_never_erases_explicit_unsupported_source_semantics(
    client: TestClient,
    utterance: str,
    diagnostic_code: str,
) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": utterance,
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-30",
        },
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "unsupported"
    assert payload["diagnostic_code"] == diagnostic_code
    assert payload["strategy"] is None


def test_http_draft_preserves_supported_fill_anchored_holding_exit(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "年报发布后买入，持有3个交易日卖出",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-30",
        },
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "ready"
    assert payload["strategy"]["exit"]["children"] == [
        {
            "type": "holding_period_exit",
            "sessions": 3,
            "anchor": "first_entry_fill",
            "count_mode": "subsequent_trading_sessions",
            "execution": "target_session_open_proxy",
        }
    ]


@pytest.mark.parametrize(
    "utterance",
    [
        "MACD买入，MACD卖出",
        "RSI买入，RSI卖出",
        "KDJ买入，KDJ卖出",
        "成交量买入，MACD死叉卖出",
    ],
)
def test_http_draft_clarifies_named_indicators_without_triggers(
    client: TestClient,
    utterance: str,
) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": utterance,
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-30",
        },
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "needs_clarification"
    assert payload["diagnostic_code"] == "indicator_trigger_requires_clarification"
    assert payload["strategy"] is None
    assert payload["clarification"] is not None
    assert "不会替你补默认触发规则" in payload["clarification"]


def test_compiler_statuses_are_preserved_as_domain_results(client: TestClient) -> None:
    clarification = client.post(
        "/api/v1/strategy-drafts",
        json={"utterance": "MACD金叉买入，死叉卖出", "as_of_date": "2026-08-27"},
    )
    unsupported = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "东方财富大跌反弹时买入",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )
    invalid = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "   ",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )
    missing_exit = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "年度报告发布后买入",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )
    incomplete_rule = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "MACD",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )
    missing_entry = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "MACD死叉卖出",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )
    unrecognized_entry = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "随便买入，MACD死叉卖出",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )

    assert clarification.json()["status"] == "needs_clarification"
    assert clarification.json()["diagnostic_code"] == "instrument_required"
    assert unsupported.json()["status"] == "unsupported"
    assert unsupported.json()["diagnostic_code"] == "template_not_published/big_drop_rebound"
    assert invalid.json()["status"] == "invalid"
    assert invalid.json()["diagnostic_code"] == "empty_utterance"
    assert missing_exit.json()["status"] == "needs_clarification"
    assert missing_exit.json()["diagnostic_code"] == "exit_rule_not_recognized"
    assert "什么条件下卖出" in missing_exit.json()["clarification"]
    assert missing_exit.json()["idea_route"] is None
    assert missing_exit.json()["strategy"] is None
    assert incomplete_rule.json()["status"] == "needs_clarification"
    assert incomplete_rule.json()["diagnostic_code"] == "strategy_rule_incomplete"
    assert incomplete_rule.json()["clarification"] == "想怎么把它变成买卖规则？"
    assert len(incomplete_rule.json()["idea_route"]["proposals"]) >= 2
    assert incomplete_rule.json()["strategy"] is None
    assert missing_entry.json()["status"] == "needs_clarification"
    assert missing_entry.json()["diagnostic_code"] == "entry_rule_not_recognized"
    assert missing_entry.json()["clarification"] == "什么时候买？"
    assert len(missing_entry.json()["idea_route"]["proposals"]) == 3
    assert missing_entry.json()["strategy"] is None
    assert unrecognized_entry.json()["status"] == "needs_clarification"
    assert unrecognized_entry.json()["diagnostic_code"] == "entry_rule_not_recognized"
    assert unrecognized_entry.json()["clarification"] == "什么时候买？"
    assert len(unrecognized_entry.json()["idea_route"]["proposals"]) == 3
    assert unrecognized_entry.json()["strategy"] is None


def test_idempotency_key_replays_same_create_without_new_draft(
    client: TestClient,
    ready_request: dict[str, str],
) -> None:
    headers = {"Idempotency-Key": "draft-mobile-001"}
    first = client.post("/api/v1/strategy-drafts", json=ready_request, headers=headers)
    second = client.post("/api/v1/strategy-drafts", json=ready_request, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert first.headers["Idempotency-Replayed"] == "false"
    assert second.headers["Idempotency-Replayed"] == "true"


def test_idempotency_key_rejects_different_payload(
    client: TestClient,
    ready_request: dict[str, str],
) -> None:
    headers = {"Idempotency-Key": "draft-mobile-002"}
    first = client.post("/api/v1/strategy-drafts", json=ready_request, headers=headers)
    changed = dict(ready_request, instrument_context="000001.SZ")
    second = client.post("/api/v1/strategy-drafts", json=changed, headers=headers)

    assert first.status_code == 201
    _assert_error(second, status_code=409, code="idempotency_key_conflict")


def test_revision_increments_and_is_itself_idempotent(
    client: TestClient,
    ready_request: dict[str, str],
) -> None:
    created = client.post("/api/v1/strategy-drafts", json=ready_request).json()
    url = f"/api/v1/strategy-drafts/{created['draft_id']}/revisions"
    strategy = deepcopy(created["strategy"])
    strategy["entry"]["params"] = {"fast": 10, "signal": 7, "slow": 30}
    strategy["exit"]["children"][0]["params"] = {"fast": 10, "signal": 7, "slow": 30}
    strategy["backtest"] = {
        "start": "2022-01-04",
        "end": "2026-08-06",
        "initial_cash_cny": 250_000,
    }
    changed = {"utterance": ready_request["utterance"], "strategy": strategy}
    headers = {"Idempotency-Key": "revision-001"}

    first = client.post(url, json=changed, headers=headers)
    replay = client.post(url, json=changed, headers=headers)

    assert first.status_code == replay.status_code == 201
    assert first.json()["revision"] == 2
    assert first.json()["draft_id"] == created["draft_id"]
    assert first.json()["strategy"] == strategy
    assert first.json()["strategy_hash"] == canonical_hash(StrategySpec.model_validate(strategy))
    assert first.json()["provenance"] == [{"path": "/", "source": "revision/request.strategy"}]
    assert first.json() == replay.json()
    assert replay.headers["Idempotency-Replayed"] == "true"


def test_revision_preserves_event_leaf_and_edited_exit_exactly(client: TestClient) -> None:
    request = {
        "utterance": "东方财富年报发布后买入，MACD死叉卖出",
        "instrument_context": "300059.SZ",
        "as_of_date": "2026-08-06",
    }
    created = client.post("/api/v1/strategy-drafts", json=request).json()
    strategy = deepcopy(created["strategy"])
    event_leaf = deepcopy(strategy["entry"])
    strategy["exit"]["children"][0]["params"] = {"fast": 8, "signal": 5, "slow": 21}
    strategy["backtest"] = {
        "start": "2023-01-03",
        "end": "2026-08-06",
        "initial_cash_cny": 180_000,
    }

    response = client.post(
        f"/api/v1/strategy-drafts/{created['draft_id']}/revisions",
        json={"utterance": request["utterance"], "strategy": strategy},
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["revision"] == 2
    assert payload["strategy"] == strategy
    assert payload["strategy"]["entry"] == event_leaf
    assert payload["strategy_hash"] == canonical_hash(StrategySpec.model_validate(strategy))


def test_revision_rejects_strategy_outside_active_catalog(
    client: TestClient,
    ready_request: dict[str, str],
) -> None:
    created = client.post("/api/v1/strategy-drafts", json=ready_request).json()
    strategy = deepcopy(created["strategy"])
    strategy["entry"]["definition_version"] = "999.0.0"

    response = client.post(
        f"/api/v1/strategy-drafts/{created['draft_id']}/revisions",
        json={"utterance": ready_request["utterance"], "strategy": strategy},
    )

    _assert_error(response, status_code=422, code="strategy_revision_invalid")


def test_revision_rejects_a_share_code_suffix_mismatch(
    client: TestClient,
    ready_request: dict[str, str],
) -> None:
    created = client.post("/api/v1/strategy-drafts", json=ready_request).json()
    strategy = deepcopy(created["strategy"])
    strategy["instrument"]["symbol"] = "300059.SH"

    response = client.post(
        f"/api/v1/strategy-drafts/{created['draft_id']}/revisions",
        json={"utterance": ready_request["utterance"], "strategy": strategy},
    )

    _assert_error(response, status_code=422, code="strategy_revision_invalid")


def test_missing_draft_and_invalid_request_use_uniform_error_envelope(
    client: TestClient,
    ready_request: dict[str, str],
) -> None:
    strategy = client.post("/api/v1/strategy-drafts", json=ready_request).json()["strategy"]
    missing = client.post(
        f"/api/v1/strategy-drafts/{uuid4()}/revisions",
        json={"utterance": ready_request["utterance"], "strategy": strategy},
        headers={"X-Request-ID": "contract-404"},
    )
    invalid = client.post(
        "/api/v1/strategy-drafts",
        json={"utterance": "x", "as_of_date": "not-a-date", "unexpected": True},
    )

    _assert_error(missing, status_code=404, code="strategy_draft_not_found")
    assert missing.json()["request_id"] == "contract-404"
    assert missing.headers["X-Request-ID"] == "contract-404"
    _assert_error(invalid, status_code=422, code="request_validation_failed")
    assert invalid.json()["error"]["details"]


def test_body_and_field_limits_are_enforced_before_compilation() -> None:
    with TestClient(create_app(max_body_bytes=256)) as limited_client:
        response = limited_client.post(
            "/api/v1/strategy-drafts",
            content=b"{" + b"x" * 300 + b"}",
            headers={"Content-Type": "application/json", "X-Request-ID": "too-big"},
        )
    _assert_error(response, status_code=413, code="request_body_too_large")
    assert response.json()["request_id"] == "too-big"

    with TestClient(create_app()) as normal_client:
        too_long = normal_client.post(
            "/api/v1/strategy-drafts",
            json={"utterance": "x" * 2_001, "as_of_date": "2026-08-27"},
        )
    _assert_error(too_long, status_code=422, code="request_validation_failed")


def test_unexpected_errors_are_sanitized_and_correlated() -> None:
    app = create_app()
    app.state.container = replace(app.state.container, compiler=_ExplodingCompiler())
    with TestClient(app, raise_server_exceptions=False) as failing_client:
        response = failing_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "MACD金叉买入，死叉卖出",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-27",
            },
            headers={"X-Request-ID": "contract-500"},
        )

    _assert_error(response, status_code=500, code="internal_error")
    assert response.json()["request_id"] == "contract-500"
    assert "secret" not in response.text


def _assert_error(response: Any, *, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    payload: dict[str, Any] = response.json()
    assert set(payload) == {"error", "request_id"}
    assert payload["error"]["code"] == code
    assert payload["request_id"]
    assert response.headers["X-Request-ID"] == payload["request_id"]
