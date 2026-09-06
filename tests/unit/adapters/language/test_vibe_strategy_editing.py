"""The model cannot ask a non-editing reply to trigger execution."""

from datetime import date
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from ashare_lab.adapters.language.vibe_candidates import (
    CandidateProviderIdentityView,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.language.vibe_strategy_editing import (
    VibeStrategyEditor,
    _ProviderEdit,
    _report_references,
)
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.strategy import FirstOfExit, IndicatorCondition, StrategySpec
from ashare_lab.ports.strategy_editing import StrategyEditRequest


@pytest.mark.asyncio
@pytest.mark.parametrize(("disposition", "answer"), [
    ("discuss", "核对一下当前规则"),
    ("request_optimization", "先给我几个能继续尝试的调整方向，不要直接帮我跑。"),
])
async def test_editor_supplies_catalog_in_custom_provider_payload(
    disposition: str, answer: str,
) -> None:
    # A custom user_payload replaces the transport's default payload entirely.
    # The editor must send the matrix it tells the model to use, not just metadata.
    root = Path(__file__).parents[4]
    matrix = build_candidate_capability_matrix(
        load_catalog_directory(root / "catalogs"),
        load_coverage_catalog_directory(root / "catalogs" / "coverage"),
    )
    requests: list[CandidateTransportRequest] = []

    class Transport:
        async def generate_json(
            self, request: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            requests.append(request)
            return cast(CandidateTransportResponse, {
                "disposition": disposition, "message": "先核对当前规则。", "strategy": None,
            })

    editor = VibeStrategyEditor(
        Transport(), capability_matrix=matrix,
        provider_identity=CandidateProviderIdentityView(
            provider="test", model="test", prompt_version="test", schema_version="test",
        ),
    )
    strategy = StrategySpec.model_validate_json(
        (root / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text()
    )
    reports = ({
        "runId": "run:stored-facts",
        "executionCosts": {
            "slippageBps": "5", "commissionRate": "0.0003", "minimumCommissionCny": "5",
        },
        "dataSource": {"provider": "eastmoney_mx_finance_data", "cacheStatus": "unknown"},
    },)
    result = await editor.edit(StrategyEditRequest(
        answer=answer, prior_utterance="", strategy=strategy,
        as_of_date=date(2026, 9, 5), backtest_results=reports,
    ))
    assert result is not None
    assert result.disposition == disposition
    assert result.strategy is None
    assert not result.run_requested
    assert not result.refresh_data
    assert len(requests) == 1
    assert requests[0].user_payload is not None
    assert requests[0].user_payload["capabilityMatrix"] == matrix.model_dump(mode="json")
    assert requests[0].user_payload["currentStrategy"] == strategy.model_dump(mode="json")
    assert requests[0].user_payload["backtestResults"] == list(reports)
    assert requests[0].user_payload["reportReferences"] == {
        "current": None, "previousDifferentStrategy": None, "earliest": None,
    }
    assert "不得用当前默认参数补造历史费用" in requests[0].system_contract
    assert "不能断言缓存或非缓存" in requests[0].system_contract


@pytest.mark.asyncio
@pytest.mark.parametrize(("disposition", "answer"), [
    ("request_optimization", "改下，我要一个盈利的策略"),
    ("discuss", "不是你推荐的吗，怎么还是亏损？"),
])
async def test_loss_feedback_keeps_user_goal_and_does_not_defend_or_execute_recommendation(
    disposition: str, answer: str,
) -> None:
    root = Path(__file__).parents[4]
    matrix = build_candidate_capability_matrix(
        load_catalog_directory(root / "catalogs"),
        load_coverage_catalog_directory(root / "catalogs" / "coverage"),
    )
    strategy = StrategySpec.model_validate_json(
        (root / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text(),
    )
    report = {
        "runId": "run:loss", "strategy": strategy.model_dump(mode="json"),
        "summary": {"totalReturn": -0.130675990386398,
                    "benchmarkReturn": -0.2693720547218151, "tradeCount": 9},
    }
    model_message = "这段回测亏损13.07%，没有达到你的盈利目标；下一步可检验退出条件的调整。"
    requests: list[CandidateTransportRequest] = []

    class Transport:
        async def generate_json(
            self, request: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            requests.append(request)
            return {"disposition": disposition, "message": model_message, "strategy": None}

    editor = VibeStrategyEditor(
        Transport(), capability_matrix=matrix,
        provider_identity=CandidateProviderIdentityView(
            provider="test", model="test", prompt_version="test", schema_version="test",
        ),
    )
    result = await editor.edit(StrategyEditRequest(
        answer=answer, prior_utterance="旧规则", strategy=strategy,
        as_of_date=date(2026, 9, 5), backtest_results=(report,),
    ))
    assert result is not None
    assert result.disposition == disposition and result.message == model_message
    assert result.strategy is None and not result.run_requested and not result.refresh_data
    assert result.provenance.prompt_version == "strategy-edit.prompt.v19"
    assert len(requests) == 1
    payload = requests[0].user_payload
    assert payload is not None and payload["answer"] == answer
    assert payload["backtestResults"] == [report]
    assert payload["reportReferences"] == {
        "current": report, "previousDifferentStrategy": None, "earliest": report,
    }
    contract = requests[0].system_contract
    assert "先承认本区间亏损、未达到用户的盈利目标" in contract
    assert "不要用跑赢基准或样本不足为推荐辩护" in contract
    assert "低胜率和小样本本身不是亏损原因" in contract
    assert "没有逐笔交易等证据时，不编造亏损因果" in contract
    assert "不保证盈利，不擅自运行新回测" in contract
    assert "第二句必须给出基于当前规则的具体下一步" in contract


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    "这次和上次的差别主要是什么？",
    "这版跟最早那版差在哪儿？两句话就行。",
])
async def test_version_comparison_keeps_ordered_reports_and_distinguishes_previous_from_earliest(
    answer: str,
) -> None:
    root = Path(__file__).parents[4]
    matrix = build_candidate_capability_matrix(
        load_catalog_directory(root / "catalogs"),
        load_coverage_catalog_directory(root / "catalogs" / "coverage"),
    )
    earliest = StrategySpec.model_validate_json(
        (root / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text(),
    )
    previous = earliest.model_copy(update={
        "entry": IndicatorCondition(
            indicator_id="technical.rsi", definition_version="1.0.0",
            params={"period": 14}, trigger="below", value=25,
        ),
        "exit": FirstOfExit(children=(IndicatorCondition(
            indicator_id="technical.rsi", definition_version="1.0.0",
            params={"period": 14}, trigger="above", value=65,
        ),)),
    })
    current = earliest.model_copy(update={
        "entry": IndicatorCondition(
            indicator_id="technical.ma_cross", definition_version="1.0.0",
            params={"fast_period": 5, "slow_period": 20, "price_field": "close"},
            trigger="golden_cross",
        ),
        "exit": FirstOfExit(children=(IndicatorCondition(
            indicator_id="technical.ma_cross", definition_version="1.0.0",
            params={"fast_period": 5, "slow_period": 20, "price_field": "close"},
            trigger="death_cross",
        ),)),
    })
    reports = tuple({
        "runId": run_id, "strategy": strategy.model_dump(mode="json"),
        "summary": {"totalReturn": total_return, "maxDrawdown": drawdown,
                    "tradeCount": trades},
    } for run_id, strategy, total_return, drawdown, trades in (
        ("run:earliest", earliest, -0.0463, -0.1014, 3),
        ("run:previous-version", previous, 0.1647, -0.1842, 2),
        ("run:current-first-run", current, -0.2200, -0.2200, 7),
        ("run:current-repeat", current, -0.2200, -0.2200, 7),
    ))
    requests: list[CandidateTransportRequest] = []
    model_message = "模型返回的版本比较，保留原规则且尚未执行新回测。"

    class Transport:
        async def generate_json(
            self, request: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            requests.append(request)
            return {"disposition": "discuss", "message": model_message, "strategy": None}

    editor = VibeStrategyEditor(
        Transport(), capability_matrix=matrix,
        provider_identity=CandidateProviderIdentityView("test", "test", "test", "test"),
    )
    result = await editor.edit(StrategyEditRequest(
        answer=answer, prior_utterance="已改成5/20双均线。", strategy=current,
        as_of_date=date(2026, 9, 5), backtest_results=reports,
    ))

    assert result is not None and result.disposition == "discuss"
    assert result.message == model_message and result.strategy is None
    assert not result.run_requested and not result.refresh_data
    assert result.provenance.prompt_version == "strategy-edit.prompt.v19"
    assert len(requests) == 1
    payload = requests[0].user_payload
    assert payload is not None and payload["answer"] == answer
    assert payload["currentStrategy"] == current.model_dump(mode="json")
    assert payload["backtestResults"] == list(reports)
    assert payload["reportReferences"] == {
        "current": reports[3], "previousDifferentStrategy": reports[1], "earliest": reports[0],
    }
    contract = requests[0].system_contract
    assert "版本比较必须使用这些角色" in contract
    assert "最近一份 strategy 不同的已完成报告" in contract
    assert "同一 strategy 的重复回测不算不同执行版本" in contract
    assert "问‘最早那版’时比较 reportReferences.earliest 和 current" in contract
    assert "角色为 null 表示所提供历史没有相应报告" in contract
    assert "版本比较必须同时概述两版买入和卖出规则" in contract
    assert "两份报告各自的收益、最大回撤及完整交易次数" in contract
    assert "缺失指标不能补造" in contract
    assert "收益差用百分点" in contract


@pytest.mark.parametrize(("versions", "current_index", "previous_index"), [
    (("current", "different", "current", "current"), 3, 1),
    (("current", "different", "current", "different"), 2, 1),
    (("current", "current"), 1, None),
    (("different",), None, None),
    ((), None, None),
])
def test_report_references_use_full_strategy_and_only_look_before_current(
    versions: tuple[str, ...], current_index: int | None, previous_index: int | None,
) -> None:
    current = StrategySpec.model_validate_json((Path(__file__).parents[4]
        / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text())
    # Rules and instrument are identical: a different backtest period is still
    # a different executed strategy. Mapping key order alone is not a difference.
    different = current.model_copy(update={
        "backtest": current.backtest.model_copy(update={"start": date(2022, 1, 1)}),
    })
    reports = tuple({
        "runId": f"run:{index}",
        "strategy": dict(reversed((current if version == "current" else different)
                                  .model_dump(mode="json").items())),
        "summary": {"totalReturn": 0, "maxDrawdown": 0, "tradeCount": 0},
    } for index, version in enumerate(versions))

    references = _report_references(StrategyEditRequest(
        answer="这次和上次的差别主要是什么？", prior_utterance="",
        strategy=current, as_of_date=date(2026, 9, 5), backtest_results=reports,
    ))

    assert references == {
        "current": reports[current_index] if current_index is not None else None,
        "previousDifferentStrategy": (
            reports[previous_index] if previous_index is not None else None
        ),
        "earliest": reports[0] if reports else None,
    }


def test_report_references_skip_incomplete_reports_without_inventing_metrics() -> None:
    strategy = StrategySpec.model_validate_json((Path(__file__).parents[4]
        / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text())
    complete = strategy.model_dump(mode="json")
    partial = {key: value for key, value in complete.items() if key != "execution"}
    summary = {
        "totalReturn": 0, "maxDrawdown": None, "tradeCount": 0,
        "benchmarkReturn": None, "benchmarkComparisonStatus": "benchmark_unavailable",
        "dataRange": {"start": "2025-09-05", "end": "2026-09-05"},
        "interpretation": "保留在完整报告，紧凑角色不重复说明文字。",
    }
    reports = (
        {"runId": "run:no-strategy", "summary": summary},
        {"runId": "run:partial-strategy", "strategy": partial, "summary": summary},
        {"runId": "run:invalid-strategy", "strategy": {}, "summary": summary},
        {"runId": "run:complete", "strategy": complete, "summary": summary},
        {"runId": "run:no-summary", "strategy": complete},
    )
    request = StrategyEditRequest(
        answer="比较结果", prior_utterance="", strategy=strategy,
        as_of_date=date(2026, 9, 5), backtest_results=reports,
    )
    references = _report_references(request)

    expected = {
        "runId": "run:complete", "strategy": complete,
        "summary": {key: value for key, value in summary.items() if key != "interpretation"},
    }
    assert references == {
        "current": expected, "previousDifferentStrategy": None, "earliest": expected,
    }
    assert request.backtest_results == reports
    assert "execution" not in partial


@pytest.mark.parametrize("disposition", ["request_optimization", "discuss", "clarify", "not_edit"])
def test_non_editing_model_reply_cannot_request_execution(disposition: str) -> None:
    payload = {"disposition": disposition, "message": "先核对这次结果。", "strategy": None}
    assert not _ProviderEdit.model_validate(payload).run_requested
    with pytest.raises(ValidationError, match="only an applied edit can request a run"):
        _ProviderEdit.model_validate({**payload, "run_requested": True})
    with pytest.raises(ValidationError, match="data refresh requires an explicit run request"):
        _ProviderEdit.model_validate({**payload, "refresh_data": True})
    with pytest.raises(ValidationError, match="only an applied edit can request a run"):
        _ProviderEdit.model_validate({**payload, "run_requested": True, "refresh_data": True})


@pytest.mark.parametrize("disposition", ["apply", "change_instrument", "select_optimization"])
def test_data_refresh_requires_an_executable_edit_with_an_explicit_run(disposition: str) -> None:
    baseline = StrategySpec.model_validate_json((Path(__file__).parents[4]
        / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text())
    payload = {
        "disposition": disposition, "message": "按这次选择重新取数并回测。",
        "strategy": baseline if disposition == "apply" else None,
        "instrument_refs": ["生益科技"] if disposition == "change_instrument" else [],
        "optimization_candidate_id": (
            "model-opt-1" if disposition == "select_optimization" else None
        ),
        "run_requested": True, "refresh_data": True,
    }
    parsed = _ProviderEdit.model_validate(payload)
    assert parsed.run_requested and parsed.refresh_data
    with pytest.raises(ValidationError, match="data refresh requires an explicit run request"):
        _ProviderEdit.model_validate({**payload, "run_requested": False})


@pytest.mark.parametrize("value", ["true", "false", "1", 1, 0, None])
def test_data_refresh_requires_a_real_boolean(value: object) -> None:
    baseline = StrategySpec.model_validate_json((Path(__file__).parents[4]
        / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text())
    with pytest.raises(ValidationError, match="valid boolean"):
        _ProviderEdit.model_validate({
            "disposition": "apply", "message": "重新取数并回测。", "strategy": baseline,
            "run_requested": True, "refresh_data": value,
        })


def test_optimization_request_cannot_supply_a_strategy_or_select_a_candidate() -> None:
    payload = {
        "disposition": "request_optimization", "message": "提供待验证的调整方案。",
        "strategy": None,
    }
    baseline = StrategySpec.model_validate_json((Path(__file__).parents[4]
        / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text())
    with pytest.raises(ValidationError, match="only an apply result contains a strategy"):
        _ProviderEdit.model_validate({**payload, "strategy": baseline})
    with pytest.raises(ValidationError, match="only optimization selection contains a candidate"):
        _ProviderEdit.model_validate({**payload, "optimization_candidate_id": "model-opt-1"})


def test_instrument_selection_cannot_supply_a_model_rewritten_strategy() -> None:
    payload = {
        "disposition": "change_instrument", "message": "按同一规则换股再测。",
        "strategy": None, "instrument_refs": ["贵州茅台", "600519"], "run_requested": True,
    }
    assert _ProviderEdit.model_validate(payload).instrument_refs == ("贵州茅台", "600519")
    with pytest.raises(ValidationError, match="target references"):
        _ProviderEdit.model_validate({**payload, "instrument_refs": []})
    with pytest.raises(ValidationError, match="target references"):
        _ProviderEdit.model_validate({**payload, "disposition": "discuss", "run_requested": False})


@pytest.mark.asyncio
@pytest.mark.parametrize(("repair_patch", "original_run", "expected_disposition"), [
    pytest.param(None, False, "change_instrument", id="literal-conflicting-pair-one-call"),
    pytest.param({}, False, "change_instrument", id="repair-to-exact-conflicting-pair"),
    pytest.param({}, True, "change_instrument", id="repair-preserves-run-permission"),
    pytest.param(
        {"disposition": "clarify", "instrument_refs": [], "run_requested": False},
        True, "clarify", id="repair-can-safely-clarify",
    ),
    pytest.param({"instrument_refs": ["贵州茅台", "300059.SZ"]}, False, None,
                 id="second-nonliteral-code-rejected"),
    pytest.param({"instrument_refs": ["贵州茅台"]}, False, None,
                 id="repair-cannot-drop-a-reference"),
    pytest.param({"instrument_refs": ["贵州茅台", "贵州茅台"]}, False, None,
                 id="repair-cannot-duplicate-a-reference"),
    pytest.param({"instrument_refs": [""]}, False, None, id="empty-reference-rejected"),
    pytest.param({"disposition": "discuss", "instrument_refs": []}, False, None,
                 id="repair-cannot-change-disposition"),
    pytest.param({"run_requested": True}, False, None, id="repair-cannot-add-run-permission"),
    pytest.param({"refresh_data": True}, True, None, id="repair-cannot-add-refresh-permission"),
    pytest.param("not-json", False, None, id="repair-invalid-json-stops"),
    pytest.param(CandidateTransportError("unavailable"), False, None,
                 id="repair-transport-failure-stops"),
])
async def test_instrument_reference_repair_is_exact_once_and_cannot_expand_authority(
    repair_patch: dict[str, object] | str | CandidateTransportError | None,
    original_run: bool, expected_disposition: str | None,
) -> None:
    root = Path(__file__).parents[4]
    matrix = build_candidate_capability_matrix(
        load_catalog_directory(root / "catalogs"),
        load_coverage_catalog_directory(root / "catalogs" / "coverage"),
    )
    strategy = StrategySpec.model_validate_json(
        (root / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text(),
    )
    request = StrategyEditRequest(
        answer="换成贵州茅台300059试试", prior_utterance="", strategy=strategy,
        as_of_date=date(2026, 9, 5),
    )
    # Synthetic malformed output, not a claim about the live model's raw refs.
    original_refs = ["贵州茅台", "300059" if repair_patch is None else "600519.SH"]
    original: dict[str, object] = {
        "disposition": "change_instrument", "message": "核实这次指定的股票。",
        "strategy": None, "instrument_refs": original_refs,
        "run_requested": original_run, "refresh_data": False,
    }
    repaired = ({
        **original, "message": "保留原文名称与代码，交由服务端核实。",
        "instrument_refs": ["贵州茅台", "300059"], **repair_patch,
    } if isinstance(repair_patch, dict) else repair_patch)
    requests: list[CandidateTransportRequest] = []

    class Transport:
        async def generate_json(
            self, transport_request: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            requests.append(transport_request)
            if len(requests) == 1:
                return original
            if isinstance(repaired, CandidateTransportError):
                raise repaired
            assert repaired is not None
            return repaired

    result = await VibeStrategyEditor(
        Transport(), capability_matrix=matrix,
        provider_identity=CandidateProviderIdentityView("test", "test", "test", "test"),
    ).edit(request)

    assert len(requests) == (1 if repair_patch is None else 2)
    assert "不纠正名称或代码，不添加交易所后缀" in requests[0].system_contract
    assert "即使名称和代码看似冲突，也原样保留" in requests[0].system_contract
    if repair_patch is not None:
        assert requests[1].user_payload == {
            **(requests[0].user_payload or {}),
            "instrumentReferenceRepair": {
                "reason": "instrument_refs_must_be_exact_answer_substrings",
                "previousInstrumentRefs": original_refs,
                "originalRunRequested": original_run, "originalRefreshData": False,
            },
        }
        assert requests[1].response_schema == requests[0].response_schema
        assert "名称和代码同时出现就分别保留两项" in (requests[1].system_footer or "")
    if expected_disposition is None:
        assert result is None
        return
    assert result is not None and result.disposition == expected_disposition
    assert result.message == (original if repair_patch is None else repaired)["message"]
    assert result.strategy is None and not result.refresh_data
    assert result.provenance.prompt_version == "strategy-edit.prompt.v19"
    if expected_disposition == "change_instrument":
        assert result.instrument_refs == ("贵州茅台", "300059")
        assert result.run_requested == original_run
    else:
        assert not result.instrument_refs and not result.run_requested


def test_optimization_selection_uses_exact_server_candidate_and_rejects_stale_base() -> None:
    baseline = StrategySpec.model_validate_json((Path(__file__).parents[4]
        / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text())
    changed = baseline.model_dump(mode="json")
    changed["entry"]["children"][0]["params"]["fast"] = 8
    optimized = StrategySpec.model_validate(changed)
    request = StrategyEditRequest(
        answer="用第一个优化方案再跑", prior_utterance="", strategy=baseline,
        as_of_date=date(2026, 9, 5), backtest_results=({
            "strategy": baseline.model_dump(mode="json"),
            "review": {"optimizationCandidates": [{"id": "model-opt-1", "strategy": changed}]},
        },),
    )
    parsed = _ProviderEdit.model_validate({
        "disposition": "select_optimization", "optimization_candidate_id": "model-opt-1",
        "strategy": None, "run_requested": True, "message": "按第一个方案重新回测。",
    })
    assert VibeStrategyEditor._selected_optimization(request, parsed.optimization_candidate_id) \
        == optimized
    with pytest.raises(ValueError, match="one stored candidate"):
        VibeStrategyEditor._selected_optimization(request, "model-opt-2")
    stale = StrategyEditRequest(
        answer=request.answer, prior_utterance="", strategy=optimized,
        as_of_date=request.as_of_date, backtest_results=request.backtest_results,
    )
    with pytest.raises(ValueError, match="different strategy revision"):
        VibeStrategyEditor._selected_optimization(stale, "model-opt-1")
