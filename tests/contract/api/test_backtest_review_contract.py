from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from functools import partial
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.api import backtest_review_schemas, create_app
from ashare_lab.api.container import ApiContainer
from ashare_lab.api.routes.backtest_runs import load_backtest_dialogue_results
from ashare_lab.api.schemas import (
    BacktestOptimizationCandidateView,
    BacktestReviewModelProvenance,
    BacktestReviewReference,
    BacktestReviewResponse,
    StrategyDraftResponse,
)
from ashare_lab.application.compile_strategy import StrategyCompiler
from ashare_lab.application.result_views import calculate_result_bundle_hash
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import IndicatorCondition, StrategySpec, canonical_json
from ashare_lab.ports.backtest_review import (
    BacktestModelReview,
    BacktestOptimizationCandidate,
    BacktestReviewRequest,
)
from ashare_lab.ports.backtest_runs import BacktestJobState
from ashare_lab.ports.candidate_generation import CandidateProvenance
from ashare_lab.ports.strategy_editing import StrategyEditRequest, StrategyEditResult

from .backtest_fakes import FakeRunStore, FakeSubmitter, make_record, result_bundle_json

ROOT = Path(__file__).parents[3]


class _Advisor:
    def __init__(self, strategies: tuple[StrategySpec, StrategySpec] | None = None) -> None:
        self.requests: list[BacktestReviewRequest] = []
        self.strategies = strategies
        self.response_hash = "sha256:" + "b" * 64

    async def review(self, request: BacktestReviewRequest) -> BacktestModelReview:
        self.requests.append(request)
        strategies = self.strategies or (
            _strategy_variant(fast=8, slow=21, signal=5),
            _strategy_variant(fast=10, slow=30, signal=8),
        )
        return BacktestModelReview(
            analysis="这次样本只有一笔完整交易，不能据此判断策略有效。",
            conclusion="先分别检验更快的趋势参数和反转逻辑，再比较成本与回撤。",
            proposals=(
                BacktestOptimizationCandidate(
                    title="检验更快的趋势反应",
                    diagnosis="原规则可能对趋势切换反应偏慢。",
                    change_dimension="entry",
                    expected_effect="检验更短参数是否能更早确认趋势。",
                    tradeoff="触发增加也会放大震荡和交易成本。",
                    suggested_utterance="候选一的完整文字说明不参与二次编译。",
                    strategy=strategies[0],
                ),
                BacktestOptimizationCandidate(
                    title="检验更慢的趋势确认",
                    diagnosis="原规则可能在震荡路径中触发过多。",
                    change_dimension="entry",
                    expected_effect="检验更慢参数是否减少假突破。",
                    tradeoff="确认更慢可能错过趋势早期。",
                    suggested_utterance="候选二的完整文字说明不参与二次编译。",
                    strategy=strategies[1],
                ),
            ),
            provider="deepseek",
            model="deepseek-v4-pro",
            prompt_version="backtest-review.prompt.v3",
            schema_version="backtest-review.v2",
            response_hash=self.response_hash,
        )


def _one_year_strategy() -> StrategySpec:
    payload = StrategySpec.model_validate_json(
        (ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text(encoding="utf-8")
    ).model_dump(mode="json")
    payload["backtest"]["start"] = "2025-08-27"
    return StrategySpec.model_validate(payload)


def _strategy_variant(*, fast: int, slow: int, signal: int) -> StrategySpec:
    payload = _one_year_strategy().model_dump(mode="json")
    payload["entry"]["children"][0]["params"] = {
        "fast": fast,
        "slow": slow,
        "signal": signal,
    }
    return StrategySpec.model_validate(payload)


class _CompilerMustNotRun:
    async def compile(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("review candidates must not be recompiled")


@pytest.fixture
def review_api() -> Iterator[tuple[TestClient, FakeRunStore, _Advisor]]:
    store = FakeRunStore()
    advisor = _Advisor()
    app = create_app(
        compiler=cast(StrategyCompiler, _CompilerMustNotRun()),
        backtest_submission=FakeSubmitter(store),
        run_store=store,
        backtest_review_advisor=advisor,
    )
    with TestClient(app) as client:
        yield client, store, advisor


class _OptimizationEditor:
    def __init__(self) -> None:
        self.requests: list[StrategyEditRequest] = []

    async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
        self.requests.append(request)
        disposition = ("request_optimization" if request.answer in _OPTIMIZATION_ANSWERS
                       else "discuss")
        return StrategyEditResult(
            disposition, "保留当前规则，先讨论待验证的调整方向。", None,
            CandidateProvenance(
                source="bounded_provider", provider="test", model="contract-fixture",
                prompt_version="strategy-edit.prompt.v12", schema_version="strategy-edit.v5",
                capability_projection_version="test",
                capability_projection_hash="sha256:" + "0" * 64,
                upstream_pattern_commit="0" * 40, candidate_rank=1,
            ),
        )


_OPTIMIZATION_ANSWERS = (
    "先给我几个能继续尝试的调整方向，不要直接帮我跑。",
    "改下，我要一个盈利的策略",
)


@pytest.mark.parametrize("entrypoint", ["create", "clarification"])
@pytest.mark.parametrize("matching_report", [True, False])
@pytest.mark.parametrize("optimization_answer", _OPTIMIZATION_ANSWERS)
def test_optimization_request_preserves_rules_and_runs_through_both_draft_endpoints(
    entrypoint: str, matching_report: bool, optimization_answer: str,
) -> None:
    editor, advisor, store = _OptimizationEditor(), _Advisor(), FakeRunStore()
    submitter = FakeSubmitter(store)
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=editor,
        backtest_anchor_date=date(2026, 9, 5),
    )
    app = create_app(compiler=compiler, backtest_submission=submitter, run_store=store,
                     backtest_review_advisor=advisor)
    with TestClient(app) as client:
        initial_utterance = "创30日新高买入，下穿10日均线卖出，回测近1年"
        initial_response = client.post("/api/v1/strategy-drafts", json={
            "utterance": initial_utterance,
            "instrument_context": "300059.SZ", "as_of_date": "2026-09-05",
        })
        assert initial_response.status_code == 201, initial_response.text
        initial = initial_response.json()
        assert initial["status"] == "ready"
        baseline = StrategySpec.model_validate(initial["strategy"])
        assert isinstance(baseline.entry, IndicatorCondition)
        variants = tuple(baseline.model_copy(update={
            "entry": baseline.entry.model_copy(update={
                "params": {**baseline.entry.params, "period": period},
            }),
        }) for period in (20, 40))
        advisor.strategies = (variants[0], variants[1])
        reports = [("run:optimization:other", variants[0])]
        if matching_report:
            reports = [("run:optimization:old", baseline),
                       ("run:optimization:current", baseline), *reports]
        for run_id, strategy in reports:
            store.seed(make_record(
                run_id, state=BacktestJobState.SUCCEEDED, strategy_json=canonical_json(strategy),
                result_json=result_bundle_json(run_id),
            ))
        original_records = dict(store.records)
        related_ids = [run_id for run_id, _strategy in reports]
        target = initial
        if entrypoint == "clarification":
            discussion = client.post("/api/v1/strategy-drafts", headers={
                "X-Conversation-Parent-Draft-ID": target["draft_id"],
            }, json={"utterance": "先解释一下当前规则。", "as_of_date": "2026-09-05"})
            assert discussion.status_code == 201, discussion.text
            target = discussion.json()
            response = client.post(
                f"/api/v1/strategy-drafts/{target['draft_id']}/revisions/"
                f"{target['revision']}/clarification-answers",
                json={"answer": optimization_answer, "related_run_ids": related_ids},
            )
            assert response.status_code == 200, response.text
            optimized = response.json()["draft"]
        else:
            response = client.post("/api/v1/strategy-drafts", headers={
                "X-Conversation-Parent-Draft-ID": target["draft_id"],
            }, json={"utterance": optimization_answer, "as_of_date": "2026-09-05",
                     "related_run_ids": related_ids})
            assert response.status_code == 201, response.text
            optimized = response.json()
        assert optimized["status"] == "needs_clarification"
        assert not optimized.get("run_requested", False)
        container = cast(ApiContainer, app.state.container)
        assert client.portal is not None
        saved = client.portal.call(partial(
            container.drafts.load_latest_dialogue_state, draft_id=UUID(optimized["draft_id"]),
        ))
        assert saved.outcome.revision_base_strategy == baseline
        review_reference = None
        if matching_report:
            assert optimized["diagnostic_code"] == "strategy_optimization_requested"
            review = optimized["backtest_review"]
            assert review["runId"] == "run:optimization:current"
            assert len(review["optimizationCandidates"]) == 2
            assert [candidate["strategy"] for candidate in review["optimizationCandidates"]] == [
                variant.model_dump(mode="json") for variant in variants
            ]
            assert len(advisor.requests) == 1
            assert advisor.requests[0].strategy_payload == baseline.model_dump(mode="json")
            assert advisor.requests[0].user_request == optimization_answer
            assert advisor.requests[0].user_request != initial_utterance
            stored_review = client.portal.call(
                container.drafts.get_review, review["runId"],
                review["modelProvenance"]["responseHash"],
            )
            assert stored_review is not None
            assert stored_review.model_dump(mode="json") == review
            review_reference = {
                "run_id": review["runId"],
                "response_hash": review["modelProvenance"]["responseHash"],
            }
        else:
            assert optimized["diagnostic_code"] == "strategy_optimization_unavailable"
            assert "backtest_review" not in optimized
            assert advisor.requests == []
        for answer in (
            "这些方案会改变哪些条件？", "刚才那些优化先不采用，我们继续用原来的那只票。",
        ):
            response = client.post(
                f"/api/v1/strategy-drafts/{optimized['draft_id']}/revisions/"
                f"{optimized['revision']}/clarification-answers",
                json={"answer": answer, "related_run_ids": related_ids,
                      "related_review": review_reference},
            )
            assert response.status_code == 200, response.text
            optimized = response.json()["draft"]
            assert optimized["diagnostic_code"] == "strategy_discussion"
            assert "backtest_review" not in optimized
            assert not optimized.get("run_requested", False)
            saved = client.portal.call(partial(
                container.drafts.load_latest_dialogue_state, draft_id=UUID(optimized["draft_id"]),
            ))
            assert saved.outcome.revision_base_strategy == baseline
        assert all(request.strategy == baseline for request in editor.requests)
        assert len(advisor.requests) == int(matching_report)
        assert submitter.configs == []
        assert store.records == original_records


def test_review_and_verified_instrument_embed_without_changing_the_http_review_contract(
    review_api: tuple[TestClient, FakeRunStore, _Advisor],
) -> None:
    assert backtest_review_schemas.BacktestReviewResponse is BacktestReviewResponse
    assert (backtest_review_schemas.BacktestOptimizationCandidateView
            is BacktestOptimizationCandidateView)
    assert backtest_review_schemas.BacktestReviewModelProvenance is BacktestReviewModelProvenance
    client, store, advisor = review_api
    run_id = "run:review:embedded"
    store.seed(make_record(
        run_id, state=BacktestJobState.SUCCEEDED,
        strategy_json=canonical_json(_one_year_strategy()),
        result_json=result_bundle_json(run_id),
    ))
    response = client.post(f"/api/v1/backtest-runs/{run_id}/review")
    assert response.status_code == 200
    assert len(advisor.requests) == 1
    assert advisor.requests[0].user_request is None
    operation = cast(FastAPI, client.app).openapi()["paths"][
        "/api/v1/backtest-runs/{run_id}/review"
    ]["post"]
    assert "requestBody" not in operation
    assert {(parameter["in"], parameter["name"]) for parameter in operation["parameters"]} == {
        ("path", "run_id"),
    }
    review = response.json()
    baseline = {
        "draft_id": str(uuid4()), "revision": 1, "status": "needs_clarification",
        "created_at": "2026-09-05T12:00:00Z",
    }
    absent = StrategyDraftResponse.model_validate(baseline).model_dump(mode="json")
    assert "backtest_review" not in absent
    assert "verified_instrument" not in absent
    draft = StrategyDraftResponse.model_validate({
        **baseline, "backtest_review": review,
        "verified_instrument": {
            "symbol": "300059.SZ", "name": "东方财富", "source": "confirmed_stock_strategy_pair",
            "retrieved_at": baseline["created_at"],
        },
    })
    app = FastAPI()

    @app.get("/draft", response_model=StrategyDraftResponse)
    def get_draft() -> StrategyDraftResponse:
        return draft

    with TestClient(app) as nested_client:
        nested = nested_client.get("/draft")
    assert nested.status_code == 200
    payload = nested.json()
    assert payload["backtest_review"] == review
    assert payload["verified_instrument"]["name"] == "东方财富"
    assert not payload.get("run_requested", False)


def test_completed_verified_run_uses_model_dsl_without_recompiling(
    review_api: tuple[TestClient, FakeRunStore, _Advisor],
) -> None:
    client, store, advisor = review_api
    run_id = "run:review:verified"
    strategy = _one_year_strategy()
    store.seed(
        make_record(
            run_id,
            state=BacktestJobState.SUCCEEDED,
            strategy_json=canonical_json(strategy),
            result_json=result_bundle_json(run_id),
        )
    )

    response = client.post(f"/api/v1/backtest-runs/{run_id}/review")

    assert response.status_code == 200
    payload = response.json()
    assert payload["runId"] == run_id
    assert payload["sourceResultHash"].startswith("sha256:")
    assert payload["evidenceGrade"] == "insufficient"
    assert len(payload["evidenceReasons"]) >= 2
    assert payload["modelProvenance"] == {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "promptVersion": "backtest-review.prompt.v3",
        "schemaVersion": "backtest-review.v2",
        "responseHash": "sha256:" + "b" * 64,
    }
    assert len(payload["optimizationCandidates"]) == 2
    assert all(item["modelSuggested"] is True for item in payload["optimizationCandidates"])
    assert all(
        item["strategy"]["instrument"]["symbol"] == "300059.SZ"
        for item in payload["optimizationCandidates"]
    )
    assert all(
        item["strategy"]["backtest"] == strategy.model_dump(mode="json")["backtest"]
        for item in payload["optimizationCandidates"]
    )
    assert {
        item["strategy"]["entry"]["children"][0]["params"]["fast"]
        for item in payload["optimizationCandidates"]
    } == {8, 10}
    assert advisor.requests[0].evidence_grade == "insufficient"
    assert "series" not in advisor.requests[0].result_facts
    assert advisor.requests[0].result_facts["summary"]["tradeCount"] == 1

    # A later model response must not replace the exact version already displayed.
    advisor.response_hash = "sha256:" + "c" * 64
    advisor.strategies = (_strategy_variant(fast=6, slow=21, signal=5),
                          _strategy_variant(fast=7, slow=30, signal=8))
    newer = client.post(f"/api/v1/backtest-runs/{run_id}/review").json()
    assert newer["optimizationCandidates"] != payload["optimizationCandidates"]
    container = cast(ApiContainer, cast(FastAPI, client.app).state.container)
    assert client.portal is not None
    for version in (payload, newer):
        facts = client.portal.call(load_backtest_dialogue_results, container, (run_id,),
                                  BacktestReviewReference(
                                      run_id=run_id,
                                      response_hash=version["modelProvenance"]["responseHash"],
                                  ))
        assert facts[0]["review"]["optimizationCandidates"] == version["optimizationCandidates"]


@pytest.mark.parametrize(("config_json", "expected_costs", "with_source"), [
    (
        '{"slippage_bps":"12.5","commission_rate":"0.0002",'
        '"minimum_commission_cny":"3","unrelated":"must-not-forward"}',
        {"slippageBps": "12.5", "commissionRate": "0.0002", "minimumCommissionCny": "3"},
        True,
    ),
    (
        '{"slippage_bps":0,"commission_rate":0,"minimum_commission_cny":0}',
        {"slippageBps": "0", "commissionRate": "0", "minimumCommissionCny": "0"},
        False,
    ),
    (
        '{}',
        {"slippageBps": None, "commissionRate": None, "minimumCommissionCny": None},
        False,
    ),
    (
        '{invalid',
        {"slippageBps": None, "commissionRate": None, "minimumCommissionCny": None},
        False,
    ),
])
def test_report_context_projects_recorded_costs_and_source_without_guessing_cache(
    review_api: tuple[TestClient, FakeRunStore, _Advisor],
    config_json: str, expected_costs: dict[str, str | None], with_source: bool,
) -> None:
    client, store, advisor = review_api
    run_id = "run:review:costs-source"
    bundle = json.loads(result_bundle_json(run_id))
    if with_source:
        bundle["summary"]["dataProvenance"] = {
            "provider": "eastmoney_mx_finance_data", "instrumentId": "300059.SZ",
            "priceBasis": "provider_back_adjusted", "retrievedAt": "2026-09-05T10:57:02Z",
            "historyStart": "2025-01-02", "historyEnd": "2025-01-03", "historyRows": 2,
            "indicatorSeries": 0, "indicatorPoints": 0,
            "queries": ["raw query is not part of the compact source"],
        }
        bundle["audit"]["resultHash"] = calculate_result_bundle_hash(bundle)
    record = replace(make_record(
        run_id, state=BacktestJobState.SUCCEEDED,
        strategy_json=canonical_json(_one_year_strategy()), result_json=canonical_json(bundle),
    ), config_json=config_json)
    store.seed(record)
    container = cast(ApiContainer, cast(FastAPI, client.app).state.container)
    assert client.portal is not None
    facts = client.portal.call(load_backtest_dialogue_results, container, (run_id,))
    expected_source = {
        "provider": "eastmoney_mx_finance_data" if with_source else None,
        "instrumentId": "300059.SZ" if with_source else None,
        "priceBasis": "provider_back_adjusted" if with_source else None,
        "retrievedAt": "2026-09-05T10:57:02+00:00" if with_source else None,
        "historyRange": ({"start": "2025-01-02", "end": "2025-01-03", "rows": 2}
                         if with_source else None),
        "cacheStatus": "unknown",
        "indicatorCacheStatuses": [],
        "refreshRequested": False,
    }
    assert facts[0]["executionCosts"] == expected_costs
    assert facts[0]["dataSource"] == expected_source
    assert "dataProvenance" not in facts[0]["summary"]
    assert client.post(f"/api/v1/backtest-runs/{run_id}/review").status_code == 200
    assert advisor.requests[0].result_facts["executionCosts"] == expected_costs
    assert advisor.requests[0].result_facts["dataSource"] == expected_source
    assert store.records[record.run_id] == record


@pytest.mark.parametrize(("history_status", "indicator_statuses", "refresh_requested"), [
    ("memory", ["disk", "live"], False),
    ("forced", ["forced", "forced"], True),
])
def test_report_context_projects_recorded_cache_evidence_and_keeps_integrity_checks(
    review_api: tuple[TestClient, FakeRunStore, _Advisor],
    history_status: str, indicator_statuses: list[str], refresh_requested: bool,
) -> None:
    client, store, advisor = review_api
    run_id = "run:review:cache-evidence"
    bundle = json.loads(result_bundle_json(run_id))
    bundle["summary"]["dataProvenance"] = {
        "provider": "eastmoney_mx_finance_data", "instrumentId": "300059.SZ",
        "priceBasis": "provider_back_adjusted", "retrievedAt": "2026-09-05T10:57:02Z",
        "historyStart": "2025-01-02", "historyEnd": "2025-01-03", "historyRows": 2,
        "indicatorSeries": 2, "indicatorPoints": 4,
        "historyCacheStatus": history_status, "indicatorCacheStatuses": indicator_statuses,
        "refreshRequested": refresh_requested,
        "queries": ["raw query is not part of the compact source"],
    }
    bundle["audit"]["resultHash"] = calculate_result_bundle_hash(bundle)
    record = make_record(
        run_id, state=BacktestJobState.SUCCEEDED,
        strategy_json=canonical_json(_one_year_strategy()), result_json=canonical_json(bundle),
    )
    store.seed(record)
    container = cast(ApiContainer, cast(FastAPI, client.app).state.container)
    assert client.portal is not None
    facts = client.portal.call(load_backtest_dialogue_results, container, (run_id,))
    expected_source = {
        "provider": "eastmoney_mx_finance_data", "instrumentId": "300059.SZ",
        "priceBasis": "provider_back_adjusted", "retrievedAt": "2026-09-05T10:57:02+00:00",
        "historyRange": {"start": "2025-01-02", "end": "2025-01-03", "rows": 2},
        "cacheStatus": history_status, "indicatorCacheStatuses": indicator_statuses,
        "refreshRequested": refresh_requested,
    }
    assert facts[0]["dataSource"] == expected_source
    assert "dataProvenance" not in facts[0]["summary"]
    assert client.post(f"/api/v1/backtest-runs/{run_id}/review").status_code == 200
    assert advisor.requests[0].result_facts["dataSource"] == expected_source
    assert store.records[record.run_id] == record

    # Evidence is still part of the immutable result, not unverified display metadata.
    bundle["summary"]["dataProvenance"]["historyCacheStatus"] = "live"
    store.seed(replace(record, result_json=canonical_json(bundle)))
    rejected = client.post(f"/api/v1/backtest-runs/{run_id}/review")
    assert rejected.status_code == 500
    assert rejected.json()["error"]["code"] == "backtest_result_integrity_mismatch"
    assert len(advisor.requests) == 1


def test_review_requires_configured_model_and_completed_verified_result() -> None:
    store = FakeRunStore()
    app = create_app(backtest_submission=FakeSubmitter(store), run_store=store)
    strategy = _one_year_strategy()
    running_id = "run:review:running"
    complete_id = "run:review:no-model"
    store.seed(
        make_record(
            running_id,
            state=BacktestJobState.RUNNING_EXECUTION,
            strategy_json=canonical_json(strategy),
        )
    )
    store.seed(
        make_record(
            complete_id,
            state=BacktestJobState.SUCCEEDED,
            strategy_json=canonical_json(strategy),
            result_json=result_bundle_json(complete_id),
        )
    )

    with TestClient(app) as client:
        running = client.post(f"/api/v1/backtest-runs/{running_id}/review")
        no_model = client.post(f"/api/v1/backtest-runs/{complete_id}/review")

    assert running.status_code == 409
    assert running.json()["error"]["code"] == "backtest_result_not_ready"
    assert no_model.status_code == 503
    assert no_model.json()["error"]["code"] == "backtest_review_model_unavailable"


@pytest.mark.parametrize("violation", ["cash", "date", "catalog", "duplicate", "other_dimension"])
def test_review_rejects_unlocked_invalid_or_duplicate_model_dsl(violation: str) -> None:
    store = FakeRunStore()
    strategy = _one_year_strategy()
    accepted = _strategy_variant(fast=8, slow=21, signal=5)
    rejected_payload = _strategy_variant(fast=10, slow=30, signal=8).model_dump(mode="json")
    if violation == "cash":
        rejected_payload["backtest"]["initial_cash_cny"] = 2_000_000
    elif violation == "date":
        rejected_payload["backtest"]["start"] = "2025-08-28"
    elif violation == "catalog":
        rejected_payload["entry"]["children"][0]["indicator_id"] = "technical.not_in_catalog"
    elif violation == "other_dimension":
        rejected_payload["exit"]["children"][0]["params"]["fast"] = 8
    rejected = (
        accepted if violation == "duplicate" else StrategySpec.model_validate(rejected_payload)
    )
    advisor = _Advisor((accepted, rejected))
    app = create_app(
        compiler=cast(StrategyCompiler, _CompilerMustNotRun()),
        backtest_submission=FakeSubmitter(store),
        run_store=store,
        backtest_review_advisor=advisor,
    )
    run_id = f"run:review:rejected:{violation}"
    store.seed(
        make_record(
            run_id,
            state=BacktestJobState.SUCCEEDED,
            strategy_json=canonical_json(strategy),
            result_json=result_bundle_json(run_id),
        )
    )

    with TestClient(app) as client:
        response = client.post(f"/api/v1/backtest-runs/{run_id}/review")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "backtest_review_candidates_unavailable"
