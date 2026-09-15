from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

import ashare_lab.bootstrap as bootstrap_module
from ashare_lab.adapters.language.openai_compatible import (
    OpenAICompatibleCandidateTransport,
)
from ashare_lab.adapters.language.rule_based import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_backtest_review import VibeBacktestReviewAdvisor
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateCapabilityMatrix,
    CandidateProviderIdentityView,
    CandidateTransportRequest,
)
from ashare_lab.adapters.language.vibe_strategy_advice import (
    VibeVerifiedFactStrategyAdvisor,
)
from ashare_lab.api.app import create_app
from ashare_lab.application.compile_strategy import StrategyCompiler
from ashare_lab.bootstrap import create_configured_app
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import CandidateAst, CompileInput
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchRequest,
    CurrentFactResearchResult,
    ResearchFact,
    ResearchPurpose,
    ResearchSource,
)
from ashare_lab.ports.idea_routing import IdeaAssetMapping, IdeaProposal, IdeaRoute
from ashare_lab.settings import AppSettings

ROOT = Path(__file__).parents[2]
MODEL_ENTRY_TEXT = "MACD 12 26 9 快线向上穿越时买入"
MODEL_EXIT_TEXT = "MACD 12 26 9 快线向下穿越时卖出"
MODEL_UTTERANCE = f"{MODEL_ENTRY_TEXT}，{MODEL_EXIT_TEXT}"


def _settings(tmp_path: Path, **overrides: object) -> AppSettings:
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "daily_ohlcv.parquet").touch()
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        "data_root": data_root,
        "catalog_root": ROOT / "catalogs",
        "queue_backend": "thread",
        "market_data_profile": "generic_parquet",
        "session_reference_mode": "research_300059",
        "event_data_required": False,
        "mx_saas_api_key": "",  # Do not read the workstation's installed Skill credential.
    }
    values.update(overrides)
    # Composition fixtures must not inherit the developer's real search keys.
    return AppSettings(_env_file=None, **values)


def _draft(utterance: str) -> dict[str, str]:
    return {
        "utterance": utterance,
        "instrument_context": "300059.SZ",
        "as_of_date": "2026-08-30",
    }


def _macd_batch() -> dict[str, object]:
    entry_text = MODEL_ENTRY_TEXT
    exit_text = MODEL_EXIT_TEXT
    return {
        "candidates": [
            {
                "instrument_symbol": "300059.SZ",
                "entry": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "golden_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "exit": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "death_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "entry_spans": [
                    {"start": 0, "end": len(entry_text), "text": entry_text},
                ],
                "exit_spans": [
                    {
                        "start": len(entry_text) + 1,
                        "end": len(entry_text) + 1 + len(exit_text),
                        "text": exit_text,
                    },
                ],
                "confidence": 0.91,
            }
        ]
    }


def _semantic_review_payload(request: CandidateTransportRequest) -> dict[str, object]:
    """Return typed fixture evidence only for the candidate's two known clauses."""
    assert request.user_payload is not None
    candidate = cast(dict[str, object], request.user_payload["candidate"])
    assert (candidate["instrument_symbol"] or request.user_payload["instrumentContext"]) == (
        "300059.SZ"
    )
    clauses = request.utterance.split("，")
    assert len(clauses) in {2, 3}
    assert "买入" in clauses[0] and "卖出" in clauses[1]
    return {
        "instrument": "equivalent",
        "requested_bar_interval": "unspecified",
        "differences": [],
        "requirements": [
            {
                "candidate_path": f"/{side}/0",
                "source_quote": clause,
                "requested_meaning": clause,
                "candidate_meaning": clause,
                "status": "represented",
            }
            for side, clause in zip(("entry", "exit"), clauses[:2], strict=True)
        ],
    }


def _reply_review_payload(request: CandidateTransportRequest) -> dict[str, object]:
    assert request.user_payload is not None
    assert request.user_payload["candidateExecutionState"] == (
        "proposed_not_selected_or_executed"
    )
    return {
        "facts": "supported",
        "state_and_authority": "supported",
        "user_intent_and_tone": "supported",
    }


def _idea_payload() -> dict[str, object]:
    return {
        "understanding": "用户表达了方向，但还没有参数化买卖条件。",
        "hypothesis": "可以分别检验趋势、反转和动量规则。",
        "proposals": [
            {
                "title": "趋势确认",
                "hypothesis": "检验均线突破后的趋势延续。",
                "entry_summary": "股价上穿 20 日均线",
                "exit_summary": "股价跌破 20 日均线",
                "suggested_utterance": "股价上穿20日均线买入，股价跌破20日均线卖出，回测近1年",
            },
            {
                "title": "超跌反转",
                "hypothesis": "检验 RSI 区间内的均值回归。",
                "entry_summary": "RSI 低于 30",
                "exit_summary": "RSI 高于 70",
                "suggested_utterance": "RSI低于30买入，RSI高于70卖出，回测近1年",
            },
            {
                "title": "动量转强",
                "hypothesis": "检验 MACD 交叉后的动量变化。",
                "entry_summary": "MACD 金叉",
                "exit_summary": "MACD 死叉",
                "suggested_utterance": "MACD金叉买入，MACD死叉卖出，回测近1年",
            },
        ],
    }


def _idea_candidate_batch(utterance: str) -> dict[str, object]:
    entry_text, exit_text, backtest_text = utterance.split("，", 2)
    if utterance.startswith("股价上穿"):
        entry = {
            "kind": "indicator",
            "indicator_id": "technical.ma",
            "definition_version": "1.0.0",
            "trigger": "price_crosses_above",
            "params": {"period": 20, "price_field": "close"},
        }
        exit_rule = {**entry, "trigger": "price_crosses_below"}
        defaulted_fields = [
            "/entry/0/params/price_field",
            "/exit/0/params/price_field",
        ]
    elif utterance.startswith("RSI"):
        entry = {
            "kind": "indicator",
            "indicator_id": "technical.rsi",
            "definition_version": "1.0.0",
            "trigger": "below",
            "params": {"period": 14},
            "value": 30,
        }
        exit_rule = {**entry, "trigger": "above", "value": 70}
        defaulted_fields = [
            "/entry/0/params/period",
            "/exit/0/params/period",
        ]
    else:
        entry = {
            "kind": "indicator",
            "indicator_id": "technical.macd",
            "definition_version": "1.0.0",
            "trigger": "golden_cross",
            "params": {"fast": 12, "slow": 26, "signal": 9},
        }
        exit_rule = {**entry, "trigger": "death_cross"}
        defaulted_fields = [
            f"/{side}/0/params/{name}"
            for side in ("entry", "exit")
            for name in ("fast", "signal", "slow")
        ]

    def span(text: str) -> dict[str, object]:
        start = utterance.index(text)
        return {"start": start, "end": start + len(text), "text": text}

    return {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [entry],
                "exit": [exit_rule],
                "entry_spans": [span(entry_text)],
                "exit_spans": [span(exit_text)],
                "backtest_lookback_years": 1,
                "backtest_span": span(backtest_text),
                "confidence": 0.91,
                "defaulted_fields": defaulted_fields,
            }
        ]
    }


class _ResolvedProposalIdeaRouter:
    async def route(self, _request: CompileInput) -> IdeaRoute:
        proposals = tuple(
            IdeaProposal(
                id=f"idea_{index:012x}",
                title=cast(str, proposal["title"]),
                hypothesis=cast(str, proposal["hypothesis"]),
                entry_summary=cast(str, proposal["entry_summary"]),
                exit_summary=cast(str, proposal["exit_summary"]),
                suggested_utterance=cast(str, proposal["suggested_utterance"]),
                capability_ids=("provider.claim",),
                assumptions=("仅用于测试服务端标的绑定。",),
                confidence=0.75,
                instrument_symbol="300033.SZ",
            )
            for index, proposal in enumerate(
                cast(list[dict[str, object]], _idea_payload()["proposals"]),
                start=1,
            )
        )
        return IdeaRoute(
            understanding="联网研究只解析到同花顺。",
            hypothesis="用三种不同价格行为做历史检验。",
            asset_mapping=IdeaAssetMapping(
                instrument_symbol=None,
                relation="unbound",
                rationale="候选卡片携带服务端已核验标的。",
                evidence_status="instrument_required",
            ),
            proposals=proposals,
        )


def _profile_diagnostic(
    *,
    profile: str,
    configured: bool,
    provider: str,
    model: str,
    thinking: str = "disabled",
    reasoning_effort: str | None = None,
    inherited_from: str | None = None,
) -> dict[str, object]:
    return {
        "configured": configured,
        "profile": profile,
        "provider": provider,
        "model": model,
        "thinking": {
            "type": thinking,
            "reasoning_effort": reasoning_effort,
        },
        "inherited_from": inherited_from,
    }


def test_direct_http_factory_keeps_deterministic_default() -> None:
    with TestClient(create_app()) as client:
        known = cast(
            Response,
            client.post(
                "/api/v1/strategy-drafts",
                json=_draft("MACD 金叉买入，死叉卖出"),
            ),
        )

    assert known.status_code == 201
    assert known.json()["status"] == "ready"
    assert known.json()["candidate_provenance"] is None


def test_http_factory_can_exclude_the_separate_portfolio_review_product() -> None:
    app = create_app(include_portfolio_review=False)

    paths = app.openapi()["paths"]

    assert "/api/v1/strategy-drafts" in paths
    assert not any(path.startswith("/api/v1/portfolio-reviews") for path in paths)


def test_unconfigured_provider_keeps_fast_path_and_marks_long_tail_unavailable(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = create_configured_app(settings)
    assert app.state.container.compiler.has_clarification_dialogue is False

    with TestClient(app) as client:
        known = cast(
            Response,
            client.post(
                "/api/v1/strategy-drafts",
                json=_draft("MACD 金叉买入，死叉卖出"),
            ),
        )
        long_tail = cast(
            Response,
            client.post(
                "/api/v1/strategy-drafts",
                json=_draft("把我脑海里的那套条件自动拿来交易"),
            ),
        )

    assert known.status_code == 201
    assert known.json()["status"] == "ready"
    assert known.json()["candidate_provenance"] is None
    assert long_tail.status_code == 201
    assert long_tail.json()["status"] == "needs_clarification"
    # An unknown theme needs research before model proposals; with both
    # channels disabled its first unavailable dependency is the research path.
    assert long_tail.json()["diagnostic_code"] == "idea_research_unavailable"
    assert long_tail.json()["idea_route"] is None
    assert app.state.candidate_provider_identity == {
        "provider": "disabled",
        "model": "unconfigured",
        "prompt_version": "ashare-lab.bounded-candidate.prompt.v1",
        "schema_version": "ashare-lab.bounded-candidate.schema.v1",
    }
    assert app.state.candidate_provider_response_mode == "disabled"
    extract_fast = _profile_diagnostic(
        profile="extract_fast",
        configured=False,
        provider="disabled",
        model="unconfigured",
    )
    assert app.state.language_provider_diagnostics == {
        "candidate_translation": extract_fast,
        "clarification_reply": extract_fast,
        "idea_generation": _profile_diagnostic(
            profile="plan_deep",
            configured=False,
            provider="disabled",
            model="unconfigured",
            inherited_from="extract_fast",
        ),
        "strategy_advice": _profile_diagnostic(
            profile="plan_deep",
            configured=False,
            provider="disabled",
            model="unconfigured",
            inherited_from="extract_fast",
        ),
        "backtest_review": _profile_diagnostic(
            profile="plan_deep",
            configured=False,
            provider="disabled",
            model="unconfigured",
            inherited_from="extract_fast",
        ),
        "viewpoint_web_research": {
            "configured": False,
            "provider": "disabled",
            "model": None,
        },
    }
    assert app.state.candidate_capability_projection["version"] == ("candidate-capabilities.v1")
    assert app.state.candidate_capability_projection["hash"].startswith("sha256:")


def test_configured_provider_is_model_first_for_a_complete_known_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[CandidateTransportRequest] = []

    async def generate_json(
        _self: OpenAICompatibleCandidateTransport,
        request: CandidateTransportRequest,
    ) -> dict[str, object]:
        calls.append(request)
        if request.response_schema_name == "initial_dialogue_intent":
            return {"intent": "new_strategy"}
        if request.response_schema_name == "strategy_semantic_review":
            return _semantic_review_payload(request)
        if request.response_schema_name == "ashare_clarification_dialogue":
            return {
                "reply_kind": "question", "acknowledgement_id": "answer_question",
                "natural_reply": "MACD 买卖规则已准备好，可以核对后再开始回测。",
            }
        if request.response_schema_name == "dialogue_reply_semantic_review":
            assert request.user_payload is not None
            assert "没有产生回测结果" in str(request.user_payload["contextSummary"])
            return {
                "facts": "supported", "state_and_authority": "supported",
                "user_intent_and_tone": "supported",
            }
        assert request.response_schema_name == "ashare_bounded_strategy_candidates"
        return _macd_batch()

    async def deterministic_generate(
        _self: RuleBasedCandidateGenerator,
        _request: CompileInput,
    ) -> tuple[CandidateAst, ...]:
        raise AssertionError("configured Live compiler must use extract_fast first")

    monkeypatch.setattr(OpenAICompatibleCandidateTransport, "generate_json", generate_json)
    monkeypatch.setattr(RuleBasedCandidateGenerator, "generate", deterministic_generate)
    secret = "provider-secret-must-not-leak"
    settings = _settings(
        tmp_path,
        candidate_provider_mode="openai_compatible",
        candidate_provider_endpoint="https://gateway.example.test/v1/chat/completions",
        candidate_provider_name="fixture-gateway",
        candidate_provider_model="fixture-model",
        candidate_provider_api_key=secret,
        candidate_provider_timeout_seconds=1.0,
    )
    app = create_configured_app(settings)

    with TestClient(app) as client:
        known = cast(
            Response,
            client.post(
                "/api/v1/strategy-drafts",
                json=_draft(MODEL_UTTERANCE),
            ),
        )

    assert known.status_code == 201
    assert known.json()["status"] == "ready"
    candidate_call = next(
        request for request in calls
        if request.response_schema_name == "ashare_bounded_strategy_candidates"
    )
    assert known.json()["candidate_provenance"] == {
        "source": "bounded_provider",
        "provider": "fixture-gateway",
        "model": "fixture-model",
        "prompt_version": "ashare-lab.bounded-candidate.prompt.v1",
        "schema_version": "ashare-lab.bounded-candidate.schema.v1",
        "capability_projection_version": "candidate-capabilities.v1",
        "capability_projection_hash": candidate_call.capability_projection_hash,
        "upstream_pattern_commit": "e90b6c6cd9fea23067a85667e7fbf74f9d73ea48",
        "candidate_rank": 1,
    }
    assert [item["path"] for item in known.json()["candidate_grounding"]["spans"]] == [
        "/entry/0",
        "/exit/0",
    ]
    assert [request.response_schema_name for request in calls] == [
        "initial_dialogue_intent",
        "ashare_bounded_strategy_candidates",
        "strategy_semantic_review",
        "ashare_clarification_dialogue",
        "dialogue_reply_semantic_review",
    ]
    assert known.json()["assistant_message"] == (
        "MACD 买卖规则已准备好，可以核对后再开始回测。"
    )
    assert candidate_call.max_candidates == 1
    assert candidate_call.capability_projection_version == "candidate-capabilities.v1"
    indicator_ids = {
        item["indicator_id"]  # type: ignore[index]
        for item in candidate_call.capability_matrix["indicators"]  # type: ignore[union-attr]
    }
    event_codes = {
        item["event_code"]  # type: ignore[index]
        for item in candidate_call.capability_matrix["events"]  # type: ignore[union-attr]
    }
    assert "technical.macd" in indicator_ids
    assert "event.financial_results.annual_report" in event_codes
    assert app.state.candidate_provider_identity == {
        "provider": "fixture-gateway",
        "model": "fixture-model",
        "prompt_version": "ashare-lab.bounded-candidate.prompt.v1",
        "schema_version": "ashare-lab.bounded-candidate.schema.v1",
    }
    assert app.state.candidate_provider_response_mode == "json_schema"
    assert secret not in str(app.state.candidate_provider_identity)
    assert secret not in known.text


def test_configured_app_exposes_nonsecret_language_channel_wiring(tmp_path: Path) -> None:
    candidate_secret = "candidate-secret-must-not-leak"
    research_secret = "research-secret-must-not-leak"
    app = create_configured_app(
        _settings(
            tmp_path,
            candidate_provider_mode="openai_compatible",
            candidate_provider_endpoint="https://gateway.example.test/v1/chat/completions",
            candidate_provider_name="deepseek",
            candidate_provider_model="deepseek-v4-flash",
            candidate_provider_api_key=candidate_secret,
            candidate_provider_response_mode="json_object",
            research_provider_endpoint="https://api.deepseek.com/responses",
            research_provider_model="deepseek-v4-flash",
            research_provider_api_key=research_secret,
        )
    )

    extract_fast = _profile_diagnostic(
        profile="extract_fast",
        configured=True,
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    assert app.state.language_provider_diagnostics == {
        "candidate_translation": extract_fast,
        "clarification_reply": extract_fast,
        "idea_generation": _profile_diagnostic(
            profile="plan_deep",
            configured=True,
            provider="deepseek",
            model="deepseek-v4-flash",
            inherited_from="extract_fast",
        ),
        "strategy_advice": _profile_diagnostic(
            profile="plan_deep",
            configured=True,
            provider="deepseek",
            model="deepseek-v4-flash",
            inherited_from="extract_fast",
        ),
        "backtest_review": _profile_diagnostic(
            profile="plan_deep",
            configured=True,
            provider="deepseek",
            model="deepseek-v4-flash",
            inherited_from="extract_fast",
        ),
        "viewpoint_web_research": {
            "configured": True,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
        },
    }
    diagnostic_text = str(app.state.language_provider_diagnostics)
    assert candidate_secret not in diagnostic_text
    assert research_secret not in diagnostic_text


def test_configured_app_uses_dedicated_plan_deep_transport_for_strategy_advice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[OpenAICompatibleCandidateTransport] = []
    review_captured: list[OpenAICompatibleCandidateTransport] = []
    original_advisor = VibeVerifiedFactStrategyAdvisor
    original_review_advisor = VibeBacktestReviewAdvisor

    def capture_advisor(
        transport: OpenAICompatibleCandidateTransport,
        *,
        capability_matrix: CandidateCapabilityMatrix,
        provider_identity: CandidateProviderIdentityView,
        model_semantic_review: bool,
    ) -> VibeVerifiedFactStrategyAdvisor:
        assert model_semantic_review is True
        captured.append(transport)
        return original_advisor(
            transport,
            capability_matrix=capability_matrix,
            provider_identity=provider_identity,
            model_semantic_review=model_semantic_review,
        )

    monkeypatch.setattr(
        bootstrap_module,
        "VibeVerifiedFactStrategyAdvisor",
        capture_advisor,
    )

    def capture_review_advisor(
        transport: OpenAICompatibleCandidateTransport,
        *,
        review_transport: OpenAICompatibleCandidateTransport,
        capability_matrix: CandidateCapabilityMatrix,
        provider_identity: CandidateProviderIdentityView,
        model_semantic_review: bool,
    ) -> VibeBacktestReviewAdvisor:
        assert model_semantic_review is True
        review_captured.append(transport)
        return original_review_advisor(
            transport,
            review_transport=review_transport,
            capability_matrix=capability_matrix,
            provider_identity=provider_identity,
            model_semantic_review=model_semantic_review,
        )

    monkeypatch.setattr(
        bootstrap_module,
        "VibeBacktestReviewAdvisor",
        capture_review_advisor,
    )
    extract_secret = "extract-secret-must-not-leak"
    plan_secret = "plan-secret-must-not-leak"
    app = create_configured_app(
        _settings(
            tmp_path,
            candidate_provider_mode="openai_compatible",
            candidate_provider_endpoint="https://api.deepseek.com/chat/completions",
            candidate_provider_name="deepseek",
            candidate_provider_model="deepseek-v4-flash",
            candidate_provider_api_key=extract_secret,
            candidate_provider_response_mode="json_object",
            plan_deep_provider_mode="openai_compatible",
            plan_deep_provider_endpoint="https://api.deepseek.com/chat/completions",
            plan_deep_provider_name="deepseek",
            plan_deep_provider_model="deepseek-v4-pro",
            plan_deep_provider_api_key=plan_secret,
            plan_deep_provider_response_mode="json_object",
            plan_deep_provider_thinking="enabled",
            plan_deep_provider_reasoning_effort="high",
        )
    )

    assert len(captured) == 1
    assert len(review_captured) == 1
    assert captured[0].identity.model == "deepseek-v4-pro"
    assert review_captured[0] is captured[0]
    assert app.state.language_provider_diagnostics["candidate_translation"] == (
        _profile_diagnostic(
            profile="extract_fast",
            configured=True,
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        | {"timeout_fallback_model": "deepseek-v4-pro"}
    )
    assert app.state.language_provider_diagnostics["strategy_advice"] == (
        _profile_diagnostic(
            profile="plan_deep",
            configured=True,
            provider="deepseek",
            model="deepseek-v4-pro",
            thinking="enabled",
            reasoning_effort="high",
        )
    )
    assert app.state.language_provider_diagnostics["idea_generation"] == (
        app.state.language_provider_diagnostics["strategy_advice"]
    )
    assert app.state.language_provider_diagnostics["backtest_review"] == (
        app.state.language_provider_diagnostics["strategy_advice"]
    )
    rendered = str(app.state.language_provider_diagnostics)
    assert extract_secret not in rendered
    assert plan_secret not in rendered


def test_vague_strategy_uses_dedicated_plan_deep_transport_and_compiler_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, CandidateTransportRequest]] = []

    async def generate_json(
        transport: OpenAICompatibleCandidateTransport,
        request: CandidateTransportRequest,
    ) -> dict[str, object]:
        calls.append((transport.identity.model, request))
        if request.response_schema_name == "initial_dialogue_intent":
            return {"intent": "vague_strategy"}
        if request.response_schema_name == "strategy_semantic_review":
            return _semantic_review_payload(request)
        if request.response_schema_name == "dialogue_reply_semantic_review":
            return _reply_review_payload(request)
        properties = cast(dict[str, object], request.response_schema["properties"])
        if "proposals" in properties:
            return _idea_payload()
        assert request.response_schema_name == "ashare_bounded_strategy_candidates"
        return _idea_candidate_batch(request.utterance)

    monkeypatch.setattr(OpenAICompatibleCandidateTransport, "generate_json", generate_json)
    app = create_configured_app(
        _settings(
            tmp_path,
            candidate_provider_mode="openai_compatible",
            candidate_provider_endpoint="https://api.deepseek.com/chat/completions",
            candidate_provider_name="deepseek",
            candidate_provider_model="deepseek-v4-flash",
            candidate_provider_api_key="extract-secret",
            candidate_provider_response_mode="json_object",
            plan_deep_provider_mode="openai_compatible",
            plan_deep_provider_endpoint="https://api.deepseek.com/chat/completions",
            plan_deep_provider_name="deepseek",
            plan_deep_provider_model="deepseek-v4-pro",
            plan_deep_provider_api_key="plan-secret",
            plan_deep_provider_response_mode="json_object",
            plan_deep_provider_thinking="enabled",
            plan_deep_provider_reasoning_effort="high",
            research_provider_mode="disabled",
        )
    )

    with TestClient(app) as client:
        response = cast(
            Response,
            client.post(
                "/api/v1/strategy-drafts",
                json=_draft("低买高卖"),
            ),
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["diagnostic_code"] == "idea_guidance_required"
    assert payload["idea_route"]["provenance"]["provider"] == "deepseek"
    assert payload["idea_route"]["provenance"]["model"] == "deepseek-v4-pro"
    assert payload["idea_route"]["provenance"]["prompt_version"] == "idea-route.prompt.v26"
    assert payload["idea_route"]["provenance"]["schema_version"] == (
        "idea-route-provider.v6"
    )
    plan_calls = [request for model, request in calls if model == "deepseek-v4-pro"]
    assert len(plan_calls) == 1
    assert plan_calls[0].response_schema_name == "strategy_ideas"
    assert all(
        model == "deepseek-v4-flash"
        for model, request in calls if request.response_schema_name != "strategy_ideas"
    )
    assert [request.response_schema_name for _model, request in calls] == [
        "initial_dialogue_intent", "strategy_ideas", "dialogue_reply_semantic_review",
        "ashare_bounded_strategy_candidates", "strategy_semantic_review",
        "ashare_bounded_strategy_candidates", "strategy_semantic_review",
        "ashare_bounded_strategy_candidates", "strategy_semantic_review",
    ]
    properties = cast(dict[str, object], plan_calls[0].response_schema["properties"])
    assert "proposals" in properties
    assert "template_ids" not in properties
    assert plan_calls[0].user_payload is not None
    assert "capabilityMatrix" in plan_calls[0].user_payload


def test_configured_app_can_select_volcengine_web_search(tmp_path: Path) -> None:
    secret = "volcengine-search-secret-must-not-leak"
    app = create_configured_app(
        _settings(
            tmp_path,
            research_provider_mode="volcengine_web_search",
            research_provider_api_key=secret,
        )
    )

    assert app.state.language_provider_diagnostics["viewpoint_web_research"] == {
        "configured": True,
        "provider": "volcengine",
        "model": "web-search",
    }
    assert secret not in str(app.state.language_provider_diagnostics)


def test_configured_app_routes_a_pure_viewpoint_to_idea_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wire source-backed research, proposals, extraction and independent reviews."""

    calls: list[CandidateTransportRequest] = []
    research_calls: list[CurrentFactResearchRequest] = []

    class FixtureResearcher:
        async def research(
            self, request: CurrentFactResearchRequest,
        ) -> CurrentFactResearchResult:
            research_calls.append(request)
            return CurrentFactResearchResult(
                provider="fixture-research", model="fixture-search",
                provider_response_id="research-viewpoint-1",
                query=request.query, purpose=request.purpose, as_of=request.as_of,
                summary="公开资料可用于理解观点，但不能证明其与当前股票的因果关系。",
                facts=(ResearchFact(
                    statement="资料讨论了特朗普这一公众人物。",
                    fact_kind="reported_fact", source_ids=("source-1",), time_scope=None,
                ),),
                sources=(ResearchSource(
                    source_id="source-1", title="观点研究测试资料",
                    url="https://research.example.test/public-context",
                    publisher="fixture-publisher", published_at=None,
                ),),
                unresolved_questions=("没有东方财富的直接资产暴露证据。",),
                retrieved_at=request.as_of, response_sha256="sha256:" + "a" * 64,
                search_call_count=1,
            )

    async def generate_json(
        _self: OpenAICompatibleCandidateTransport,
        request: CandidateTransportRequest,
    ) -> dict[str, object]:
        calls.append(request)
        if request.response_schema_name == "initial_dialogue_intent":
            return {"intent": "viewpoint"}
        if request.response_schema_name == "strategy_semantic_review":
            return _semantic_review_payload(request)
        if request.response_schema_name == "dialogue_reply_semantic_review":
            assert request.user_payload is not None
            assert request.user_payload["research"] is not None
            return _reply_review_payload(request)
        properties = cast(dict[str, object], request.response_schema.get("properties", {}))
        assert "template_ids" not in properties
        if "proposals" in properties:
            assert len(research_calls) == 1  # Research must precede the proposals.
            result = _idea_payload()
            result["understanding"] = (
                "听起来你对特朗普很反感。可以先把这种感受放在讨论中，"
                "另外用当前股票检验几种价格规则，不假定二者存在因果关系。"
            )
            return result
        assert request.response_schema_name == "ashare_bounded_strategy_candidates"
        return _idea_candidate_batch(request.utterance)

    monkeypatch.setattr(OpenAICompatibleCandidateTransport, "generate_json", generate_json)
    monkeypatch.setattr(
        bootstrap_module, "_build_web_researcher", lambda _settings: FixtureResearcher(),
    )
    settings = _settings(
        tmp_path,
        candidate_provider_mode="openai_compatible",
        candidate_provider_endpoint="https://gateway.example.test/v1/chat/completions",
        candidate_provider_name="fixture-gateway",
        candidate_provider_model="fixture-model",
        candidate_provider_api_key="fixture-secret",
        candidate_provider_timeout_seconds=1.0,
    )
    app = create_configured_app(settings)

    with TestClient(app) as client:
        response = cast(
            Response,
            client.post(
                "/api/v1/strategy-drafts",
                json=_draft("我讨厌特朗普"),
            ),
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "needs_clarification"
    assert payload["diagnostic_code"] == "idea_guidance_required"
    assert payload["strategy"] is None
    assert payload["strategy_hash"] is None
    assert payload["idea_route"]["schema_version"] == "idea-route.v1"
    assert payload["idea_route"]["asset_mapping"] == {
        "instrument_symbol": "300059.SZ",
        "relation": "current_page_proxy",
        "rationale": (
            "只使用当前股票页的 300059.SZ 检验价格行为；"
            "当前没有资产暴露证据，不声称该观点导致该股涨跌。"
        ),
        "evidence_status": "host_context_only",
    }
    assert len(payload["idea_route"]["proposals"]) == 3
    assert {item["capability_ids"][0] for item in payload["idea_route"]["proposals"]} == {
        "technical.ma",
        "technical.rsi",
        "technical.macd",
    }
    assert [request.response_schema_name for request in calls] == [
        "initial_dialogue_intent", "strategy_ideas", "dialogue_reply_semantic_review",
        "ashare_bounded_strategy_candidates", "strategy_semantic_review",
        "ashare_bounded_strategy_candidates", "strategy_semantic_review",
        "ashare_bounded_strategy_candidates", "strategy_semantic_review",
    ]
    assert len(research_calls) == 1
    assert research_calls[0].query == "我讨厌特朗普"
    assert research_calls[0].purpose is ResearchPurpose.VIEWPOINT
    assert research_calls[0].instrument_context == "300059.SZ"
    proposal_call = calls[1]
    assert "proposals" in cast(dict[str, object], proposal_call.response_schema["properties"])
    assert proposal_call.user_payload is not None
    assert "capabilityMatrix" in proposal_call.user_payload
    research_context = cast(dict[str, object], proposal_call.user_payload["research"])
    assert research_context["facts"] == [{
        "statement": "资料讨论了特朗普这一公众人物。", "factKind": "reported_fact",
        "sourceIds": ["source-1"], "timeScope": None,
    }]
    assert "观点研究测试资料" in str(research_context["sources"])
    assert payload["idea_route"]["research"]["sources"][0]["url"] == (
        "https://research.example.test/public-context"
    )
    assert "对特朗普很反感" in payload["idea_route"]["understanding"]
    assert "不假定二者存在因果关系" in payload["idea_route"]["understanding"]
    assert all(
        "candidates" in cast(dict[str, object], request.response_schema["properties"])
        for request in calls[3::2]
    )


def test_api_proposal_selection_preserves_the_server_verified_proposal_symbol() -> None:
    catalog = load_catalog_directory(ROOT / "catalogs")
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=_ResolvedProposalIdeaRouter(),
        catalog=catalog,
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )
    app = create_app(compiler=compiler, catalog=catalog)

    with TestClient(app) as client:
        created_response = cast(
            Response,
            client.post(
                "/api/v1/strategy-drafts",
                json={
                    "utterance": "我讨厌特朗普",
                    "as_of_date": "2026-08-30",
                },
            ),
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        assert created["idea_route"]["asset_mapping"]["instrument_symbol"] is None
        assert all(
            item["instrument_symbol"] == "300033.SZ"
            for item in created["idea_route"]["proposals"]
        )

        selected_response = cast(
            Response,
            client.post(
                (
                    f"/api/v1/strategy-drafts/{created['draft_id']}"
                    f"/revisions/{created['revision']}/clarification-answers"
                ),
                json={"answer": "1"},
            ),
        )

    assert selected_response.status_code == 200, selected_response.text
    selected = selected_response.json()
    assert selected["reply_kind"] == "accepted"
    assert selected["draft"]["status"] == "ready"
    assert selected["draft"]["strategy"]["instrument"]["symbol"] == "300033.SZ"
