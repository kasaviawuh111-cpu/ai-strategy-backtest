import hashlib
import json
from datetime import date

import pytest
from pydantic import ValidationError

from ashare_lab.adapters.language.candidate_semantic_review import (
    CandidateSemanticReviewProtocolError,
    SemanticReview,
    review_candidate_semantics,
)
from ashare_lab.adapters.language.executable_semantic_projection import (
    project_executable_semantics,
)
from ashare_lab.adapters.language.vibe_candidates import CandidateTransportRequest
from ashare_lab.domain.signals import SignalRuntime
from ashare_lab.domain.signals.provider_catalog import provider_binding_for_condition
from ashare_lab.domain.strategy import IndicatorCondition
from tests.unit.signals.conftest import make_bars


def verdict():
    return {
        "instrument": "equivalent",
        "requested_bar_interval": "unspecified",
        "differences": [],
    }


def test_represented_requirement_cannot_also_be_a_semantic_difference():
    from ashare_lab.adapters.language.candidate_semantic_review import _validate_semantic_review
    row = dict(candidate_path='/trading_plan/parameters/observation', source_quote='q0',
               requested_meaning='每天收盘确认', candidate_meaning='按日收盘观察，符合要求')
    data = {**verdict(), 'differences': [row], 'requirements': [{**row, 'status': 'represented'}]}
    with pytest.raises(ValueError, match='semantic review contradicts represented requirement'):
        _validate_semantic_review(data, utterance='每天收盘确认', paths=[row['candidate_path']],
                                  source_quotes={'q0': '每天收盘确认'})
    data['requirements'][0]['status'] = 'different'
    assert not _validate_semantic_review(data, utterance='每天收盘确认', paths=[row['candidate_path']],
                                        source_quotes={'q0': '每天收盘确认'}).equivalent


@pytest.mark.parametrize("kind", ["holding_period", "stop_loss", "relative_price"])
@pytest.mark.parametrize("mode", ["shares", "amount", "all_position"])
def test_all_position_review_omits_only_inactive_sizing_fields(kind, mode):
    rule = {"kind": kind, "side": "sell", "sizing_mode": mode,
            "quantity": 100, "amount_cny": None}
    projected = project_executable_semantics(rule)
    if mode == "all_position":
        assert projected == {"kind": kind, "side": "sell", "sizing_mode": mode}
    else:
        assert projected == rule
    assert rule["quantity"] == 100


@pytest.mark.parametrize("kind", ["position_return", "trailing_drawdown"])
@pytest.mark.parametrize("observation", ["minute_bar", "daily_close"])
def test_hybrid_context_keeps_daily_entry_and_minute_exit_distinct(kind, observation):
    from ashare_lab.adapters.language.candidate_semantic_review import _candidate_execution_context
    context = _candidate_execution_context({
        "entry": [{"kind": "indicator"}],
        "exit": [{"kind": kind, "observation": observation}],
    })
    if observation == "minute_bar":
        assert context["bar_interval"] == "1d_entry_and_1m_protection"
        assert context["entry_execution"] == "next_tradable_session_open"
        assert context["protection_execution"] == "next_bar_order_activation_then_ohlc_matching"
        assert context["protection_position"] == "actual_held_shares_subject_to_t_plus_one"
    else:
        assert context["bar_interval"] == "1d"
        assert context["execution"] == "next_tradable_session_open"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,observation,interval", [
    ("grid", "minute_bar", "1m"), ("conditional", "minute_bar", "1m"),
    ("grid", "daily_close", "1d"), ("grid", None, "server_selected"),
])
async def test_review_execution_context_matches_price_plan(kind, observation, interval):
    captured = []
    class Transport:
        async def generate_json(self, request):
            captured.append(request)
            return provider_verdict(request)
    request = CandidateTransportRequest(
        utterance="按一分钟网格买卖", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 11), max_candidates=1, response_schema={},
        capability_matrix={}, capability_projection_version="test",
        capability_projection_hash="test", system_contract="test",
    )
    candidate = {"trading_plan": {"kind": kind, "parameters": {"observation": observation}}}
    await review_candidate_semantics(Transport(), request, candidate)
    context = captured[0].user_payload["executionContext"]
    assert context["bar_interval"] == interval
    if interval == "1m":
        assert context["execution"] == "next_bar_order_activation_then_ohlc_matching"
        assert context["availability"] == "checked_at_backtest_submission"


@pytest.mark.parametrize("at", ["open", "close"])
def test_scheduled_execution_context_is_not_next_day_signal(at):
    from ashare_lab.adapters.language.candidate_semantic_review import _candidate_execution_context
    context = _candidate_execution_context({"trading_plan": {
        "kind": "scheduled", "parameters": {"at": at},
    }})
    assert context["signal_evaluation"] == "schedule_fixed_before_session_open"
    assert context["execution"] == f"scheduled_session_{at}"
    assert context["limit_matching"] == ("close_only" if at == "close" else "open_then_hl")


def test_display_groups_same_requirement_without_losing_repair_paths():
    review = SemanticReview.model_validate({**verdict(), "differences": [
        {"candidate_path": path, "source_quote": "突破后停止策略",
         "requested_meaning": "突破后持续停止策略", "candidate_meaning": meaning}
        for path, meaning in [
            ("/entry", "没有持续停用机制"),
            ("/exit", "没有持续停用机制"),
            ("/exit/0", "仅触发一次卖出"),
        ]
    ]})
    assert review.issues == ["原意为突破后持续停止策略；当前为没有持续停用机制；仅触发一次卖出"]
    assert len(review.repair_issues) == 3
    assert [item.split("：", 1)[0] for item in review.repair_issues] == [
        "/entry", "/exit", "/exit/0",
    ]
    assert not review.equivalent


def test_display_grouping_keeps_distinct_requirements_and_long_behaviours():
    meanings = ["甲" * 150, "乙" * 150, "丙" * 150]
    review = SemanticReview.model_validate({**verdict(), "differences": [
        {"candidate_path": f"/exit/{index}", "source_quote": "原要求",
         "requested_meaning": "停用策略" if index < 2 else "退出持仓",
         "candidate_meaning": meaning}
        for index, meaning in enumerate(meanings)
    ]})
    assert len(review.issues) == 3
    assert all(len(issue) <= 240 for issue in review.issues)
    assert all(meaning in "".join(review.issues) for meaning in meanings)
    assert review.issues[-1].startswith("原意为退出持仓")
    assert len(review.repair_issues) == 3


def provider_verdict(request):
    return {**verdict(), "requirements": [{
        "status": "represented",
        "candidate_path": "/" + next(iter(request.user_payload["candidate"])),
        "source_quote": request.utterance,
        "requested_meaning": "测试要求", "candidate_meaning": "测试要求",
    }]}


@pytest.mark.parametrize("result", ["mismatch", "uncertain"])
def test_disputed_identity_is_not_approved(result):
    assert not SemanticReview.model_validate({**verdict(), "instrument": result}).equivalent


@pytest.mark.asyncio
@pytest.mark.parametrize("final_instrument", ["equivalent", "mismatch", "uncertain", "invalid"])
async def test_review_enum_repair_keeps_candidate_and_never_defaults_to_approval(final_instrument):
    requests = []

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            return {**provider_verdict(request),
                    "instrument": "invalid" if len(requests) == 1 else final_instrument}

    original = CandidateTransportRequest(
        utterance="东方财富公告后买入", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 8), max_candidates=1, response_schema={},
        capability_matrix={}, capability_projection_version="test",
        capability_projection_hash="test", system_contract="test",
    )
    candidate = {"instrument_symbol": "300059.SZ"}
    if final_instrument == "invalid":
        with pytest.raises(CandidateSemanticReviewProtocolError):
            await review_candidate_semantics(Transport(), original, candidate)
    else:
        result = await review_candidate_semantics(Transport(), original, candidate)
        assert result.review.equivalent == (final_instrument == "equivalent")
        serialized = json.dumps(candidate, ensure_ascii=False, sort_keys=True, allow_nan=False)
        expected_hash = "sha256:" + hashlib.sha256(serialized.encode()).hexdigest()
        assert result.candidate_sha256 == expected_hash
    assert len(requests) == 2
    assert all(r.response_schema_name == "strategy_semantic_review" for r in requests)
    assert all(r.user_payload["candidate"] == candidate for r in requests)
    assert "literal_error" in requests[1].user_payload["validationFeedback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("instrument", ["equivalent", "mismatch"])
async def test_review_receives_verified_name_mapping_without_forcing_approval(instrument):
    requests = []

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            return {**provider_verdict(request), "instrument": instrument}

    verified = {"symbol": "300059.SZ", "matchedUserText": "东方财富",
                "sourceSpan": {"start": 0, "end": 4}}
    original = CandidateTransportRequest(
        utterance="东方财富公告后买入", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 8), max_candidates=1, response_schema={},
        capability_matrix={}, capability_projection_version="test",
        capability_projection_hash="test", system_contract="test",
        user_payload={"verifiedInstrument": verified},
    )
    result = await review_candidate_semantics(
        Transport(), original, {"instrument_symbol": "300059.SZ"},
    )
    assert result.review.equivalent == (instrument == "equivalent")
    assert len(requests) == 1
    assert requests[0].user_payload["verifiedInstrument"] == verified
    assert requests[0].user_payload["originalUtterance"] == original.utterance


def test_only_concrete_differences_block_without_seven_way_voting():
    with pytest.raises(ValidationError):
        SemanticReview.model_validate({"entry": "equivalent"})
    assert SemanticReview.model_validate(verdict()).equivalent
    assert not SemanticReview.model_validate(
        {**verdict(), "differences": [{"candidate_path": "/entry/0/value",
            "source_quote": "RSI高于55", "requested_meaning": "RSI大于55",
            "candidate_meaning": "RSI大于50"}]}
    ).equivalent


@pytest.mark.parametrize("interval", ["other_intraday", "weekly", "monthly", "tick", "uncertain"])
def test_explicit_incompatible_interval_overrules_model_equivalent_verdict(interval):
    assert not SemanticReview.model_validate(
        {**verdict(), "requested_bar_interval": interval}
    ).equivalent


@pytest.mark.asyncio
async def test_review_is_bound_to_exact_candidate_and_original_input():
    requests = []

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            return provider_verdict(request)

    original = CandidateTransportRequest(
        utterance="茅台，收盘站上过去二十天最高价就买，掉到二十日均线下卖",
        instrument_context="600519.SH",
        as_of_date=date(2026, 9, 7),
        max_candidates=1,
        response_schema={},
        capability_matrix={"indicators": [
            {"indicator_id": "technical.donchian", "formula_summary": "rolling channel"},
            {"indicator_id": "technical.rsi", "formula_summary": "relative strength"},
        ]},
        capability_projection_version="test",
        capability_projection_hash="test",
        system_contract="original generation",
    )
    candidate = {"entry": [{"indicator_id": "technical.donchian", "params": {"period": 20}}]}
    first = await review_candidate_semantics(Transport(), original, candidate)
    second = await review_candidate_semantics(Transport(), original, {"period": 60})
    assert first.review.equivalent
    assert first.candidate_sha256 != second.candidate_sha256
    assert requests[0].user_payload["originalUtterance"] == original.utterance
    assert requests[0].user_payload["asOfDate"] == "2026-09-07"
    assert requests[0].response_schema_name == "strategy_semantic_review"
    assert requests[0].user_payload["candidate"] == candidate
    assert "capabilityMatrix" not in requests[0].user_payload
    assert requests[0].user_payload["selectedIndicatorDefinitions"] == [
        {"indicator_id": "technical.donchian", "formula_summary": "rolling channel"},
    ]
    assert requests[0].user_payload["executionContext"]["bar_interval"] == "1d"
    for name in ("SemanticDifference", "RequirementCoverage"):
        path_schema = requests[0].response_schema["$defs"][name]["properties"]["candidate_path"]
        assert path_schema["enum"] == [
            "/entry", "/entry/0", "/entry/0/indicator_id", "/entry/0/params",
            "/entry/0/params/period",
        ]


@pytest.mark.asyncio
async def test_source_metadata_is_not_reviewed_as_extra_conditions_but_stays_hash_bound():
    requests = []

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            return provider_verdict(request)

    original = CandidateTransportRequest(
        utterance="当日放量买，跌破均线卖",
        instrument_context="600519.SH",
        as_of_date=date(2026, 9, 7),
        max_candidates=1,
        response_schema={},
        capability_matrix={},
        capability_projection_version="test",
        capability_projection_hash="test",
        system_contract="test",
    )
    execution = {"entry": [{"params": {"baseline_period": 20}, "value": 2}]}
    metadata = {"entry_spans": [{"text": "原文"}], "defaulted_fields": ["/entry/0/params/x"]}
    first = await review_candidate_semantics(Transport(), original, {**execution, **metadata})
    second = await review_candidate_semantics(Transport(), original, execution)
    assert first.candidate_sha256 != second.candidate_sha256
    assert (
        requests[0].user_payload["candidate"] == requests[1].user_payload["candidate"] == execution
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_evidence", ["quote", "path"])
async def test_review_cannot_invent_source_or_refer_to_auxiliary_metadata(bad_evidence):
    requests = []

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            return {**provider_verdict(request), "differences": [{
                "candidate_path": "/entry_spans" if bad_evidence == "path" else "/entry",
                "source_quote": "虚构原话" if bad_evidence == "quote" else "买入条件",
                "requested_meaning": "明确条件", "candidate_meaning": "不同条件",
            }]}

    request = CandidateTransportRequest(
        utterance="买入条件", instrument_context="600519.SH", as_of_date=date(2026, 9, 7),
        max_candidates=1, response_schema={}, capability_matrix={},
        capability_projection_version="test", capability_projection_hash="test",
        system_contract="test",
    )
    with pytest.raises(CandidateSemanticReviewProtocolError):
        await review_candidate_semantics(Transport(), request, {"entry": [], "entry_spans": []})
    assert len(requests) == 2  # One local evidence repair, never unbounded retries.
    if bad_evidence == "path":
        assert requests[1].user_payload["invalidCandidatePathLocations"] == ["/differences/0/candidate_path"]
        assert "/entry" in requests[1].user_payload["allowedCandidatePaths"]
        assert "/entry_spans" not in requests[1].user_payload["allowedCandidatePaths"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["represented", "missing", "different"])
async def test_evidence_typo_repairs_only_review_and_preserves_substantive_verdict(status):
    requests = []
    utterance = "东方财富发布大股东增持公告后次日买入，持有30个交易日卖出"

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            return {**verdict(), "requirements": [{
                "status": status, "candidate_path": "/entry",
                "source_quote": "大股东发布增持公告" if len(requests) == 1 else utterance,
                "requested_meaning": "公告后次日入场",
                "candidate_meaning": (
                    "公告后次日入场" if status == "represented" else "普通价格信号入场"
                ),
            }]}

    request = CandidateTransportRequest(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 7),
        max_candidates=1, response_schema={}, capability_matrix={},
        capability_projection_version="test", capability_projection_hash="test",
        system_contract="test",
    )
    candidate = {"entry": [], "exit": [{"kind": "holding_period", "sessions": 30}]}
    result = await review_candidate_semantics(Transport(), request, candidate)
    assert len(requests) == 2
    assert all(item.response_schema_name == "strategy_semantic_review" for item in requests)
    assert (requests[0].user_payload["candidate"]
            == requests[1].user_payload["candidate"] == candidate)
    assert requests[1].user_payload["previousReview"]["requirements"][0]["status"] == status
    assert "source quote" in requests[1].user_payload["validationFeedback"]
    for name in ("SemanticDifference", "RequirementCoverage"):
        quotes = requests[0].response_schema["$defs"][name]["properties"]["source_quote"]["enum"]
        sources = requests[0].user_payload["sourceQuotes"]
        assert quotes == list(sources) and sources["q0"] == utterance
        assert all(quote in utterance for quote in sources.values())
    assert result.review.equivalent is (status == "represented")
    if status != "represented":
        assert result.review.differences[0].requested_meaning == "公告后次日入场"
        assert result.review.differences[0].candidate_meaning == "普通价格信号入场"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["represented", "missing", "different"])
@pytest.mark.parametrize("reference", ["full_id", "fragment_id", "legacy_quote"])
async def test_quote_ids_resolve_exact_multiturn_evidence_without_changing_verdict(
    status, reference,
):
    requests = []
    utterance = ("原请求：东方财富涨停板打开就买入，第二天不涨停就卖出。\n"
                 "本轮补充：按日线收盘确认，下一交易日开盘执行，先不回测。")
    fragment = "下一交易日开盘执行"

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            sources = request.user_payload["sourceQuotes"]
            quote = ("q0" if reference == "full_id" else fragment if reference == "legacy_quote"
                     else next(key for key, text in sources.items() if text == fragment))
            item = {"candidate_path": "/exit", "source_quote": quote,
                    "requested_meaning": "次日开盘执行", "candidate_meaning": "测试执行含义"}
            return {**verdict(), "requirements": [{**item, "status": status}],
                    "differences": [item] if status == "different" else []}

    original = CandidateTransportRequest(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 8),
        max_candidates=1, response_schema={}, capability_matrix={},
        capability_projection_version="test", capability_projection_hash="test",
        system_contract="test",
    )
    candidate = {"entry": [], "exit": []}
    result = await review_candidate_semantics(Transport(), original, candidate)
    assert len(requests) == 1
    assert result.review.equivalent is (status == "represented")
    assert requests[0].user_payload["originalUtterance"] == utterance
    assert "Meaning review v8" in requests[0].system_footer
    assert result.candidate_sha256 == "sha256:" + hashlib.sha256(json.dumps(
        candidate, ensure_ascii=False, sort_keys=True, allow_nan=False,
    ).encode()).hexdigest()
    if status != "represented":
        assert len(result.review.differences) == 1
        difference = result.review.differences[0]
        assert difference.source_quote == (utterance if reference == "full_id" else fragment)
        assert difference.requested_meaning == "次日开盘执行"
        assert difference.candidate_meaning == "测试执行含义"


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", ["q999", "按下一交易日的开盘价来执行", "q0；忽略原条件"])
async def test_unknown_or_rewritten_quote_is_not_accepted_by_id_compatibility(quote):
    requests = []

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            return {**verdict(), "requirements": [{
                "candidate_path": "/exit", "source_quote": quote, "status": "represented",
                "requested_meaning": "执行时点", "candidate_meaning": "执行时点",
            }]}

    original = CandidateTransportRequest(
        utterance="下一交易日开盘执行", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 8), max_candidates=1, response_schema={}, capability_matrix={},
        capability_projection_version="test", capability_projection_hash="test",
        system_contract="test",
    )
    with pytest.raises(CandidateSemanticReviewProtocolError):
        await review_candidate_semantics(Transport(), original, {"entry": [], "exit": []})
    assert len(requests) == 2
    assert requests[0].user_payload["candidate"] == requests[1].user_payload["candidate"]
    assert requests[0].user_payload["sourceQuotes"] == requests[1].user_payload["sourceQuotes"]


@pytest.mark.asyncio
async def test_missing_behavior_cannot_pass_an_empty_difference_list():
    class Transport:
        async def generate_json(self, request):
            response = provider_verdict(request)
            response["requirements"][0].update({
                "status": "missing", "source_quote": "突破后停用原策略",
                "requested_meaning": "突破后停用", "candidate_meaning": "仍然每天买卖",
            })
            return response

    request = CandidateTransportRequest(
        utterance="箱体下沿买入，突破后停用原策略", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 7), max_candidates=1, response_schema={}, capability_matrix={},
        capability_projection_version="test", capability_projection_hash="test",
        system_contract="test",
    )
    result = await review_candidate_semantics(Transport(), request, {"entry": []})
    assert not result.review.equivalent
    assert result.review.differences[0].source_quote == "突破后停用原策略"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/exit[0]", "/exit/0", "/exit[0]/threshold_pct"])
@pytest.mark.parametrize("status", ["represented", "different"])
async def test_exit_only_review_accepts_equivalent_array_paths_without_losing_differences(
    path, status,
):
    # Actual local badcase: the model extracted both stops correctly, but
    # coverage pointers /exit[0] and /exit[1] caused a generic parser error.
    class Transport:
        async def generate_json(self, request):
            return {**verdict(), "requirements": [{
                "status": status, "candidate_path": path,
                "source_quote": "涨3个点就卖", "requested_meaning": "止盈3%",
                "candidate_meaning": "止盈3%" if status == "represented" else "止盈5%",
            }, {
                "status": "represented", "candidate_path": "/exit[1]",
                "source_quote": "亏2个点就割", "requested_meaning": "止损2%",
                "candidate_meaning": "止损2%",
            }]}

    request = CandidateTransportRequest(
        utterance="东方财富涨3个点就卖，亏2个点就割", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 7), max_candidates=1, response_schema={},
        capability_matrix={}, capability_projection_version="test",
        capability_projection_hash="test", system_contract="test",
    )
    candidate = {"entry": [], "exit": [
        {"kind": "position_return", "trigger": "take_profit", "threshold_pct": 3},
        {"kind": "position_return", "trigger": "stop_loss", "threshold_pct": 2},
    ]}
    result = await review_candidate_semantics(Transport(), request, candidate)
    assert result.review.equivalent is (status == "represented")
    if status == "different":
        assert result.review.differences[0].candidate_path == path.replace("[0]", "/0")
    assert candidate["entry"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/exit[9]", "/exit[-1]", "/exit[0]/invented", "/entry[0]", "/exit[0].threshold_pct",
])
async def test_array_path_compatibility_does_not_approve_nonexistent_fields(path):
    class Transport:
        async def generate_json(self, request):
            response = provider_verdict(request)
            response["requirements"][0]["candidate_path"] = path
            return response

    request = CandidateTransportRequest(
        utterance="止盈3%", instrument_context="300059.SZ", as_of_date=date(2026, 9, 7),
        max_candidates=1, response_schema={}, capability_matrix={},
        capability_projection_version="test", capability_projection_hash="test",
        system_contract="test",
    )
    with pytest.raises(CandidateSemanticReviewProtocolError):
        await review_candidate_semantics(Transport(), request, {
            "entry": [], "exit": [{"threshold_pct": 3}],
        })


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "expected"), [
    ("instrument", ["equivalent", "mismatch", "uncertain"]),
    ("requested_bar_interval", [
        "1d", "1m", "intraday", "other_intraday", "weekly", "monthly", "tick", "unspecified", "uncertain",
    ]),
    ("requirements/0/status", ["represented", "missing", "different"]),
])
async def test_schema_repair_feedback_identifies_field_without_logging_input(
    field, expected, caplog,
):
    requests = []
    private_input = "provider-private-value-never-log"
    private_key = "provider-private-key-never-log"
    utterance = "用户的私有股票表达"

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            response = provider_verdict(request)
            if len(requests) == 1:
                if field.startswith("requirements/"):
                    response["requirements"][0]["status"] = private_input
                else:
                    response[field] = private_input
                response[private_key] = private_input
            return response

    original = CandidateTransportRequest(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 8),
        max_candidates=1, response_schema={}, capability_matrix={},
        capability_projection_version="test", capability_projection_hash="test",
        system_contract="test",
    )
    result = await review_candidate_semantics(Transport(), original, {"entry": []})
    assert result.review.equivalent
    feedback = requests[1].user_payload["validationFeedback"]
    details = json.loads(feedback.removeprefix("review_schema_invalid:"))
    assert {"path": "/" + field, "type": "literal_error", "expected": expected} in details
    assert {"path": "/field", "type": "extra_forbidden"} in details
    assert feedback in caplog.text
    for private in (private_input, private_key, utterance):
        assert private not in feedback and private not in caplog.text


@pytest.mark.asyncio
async def test_invalid_review_json_is_repaired_against_same_candidate():
    requests = []

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            return "{invalid-json" if len(requests) == 1 else provider_verdict(request)

    original = CandidateTransportRequest(
        utterance="涨3%卖", instrument_context="300059.SZ", as_of_date=date(2026, 9, 8),
        max_candidates=1, response_schema={}, capability_matrix={},
        capability_projection_version="test", capability_projection_hash="test",
        system_contract="test",
    )
    candidate = {"entry": []}
    result = await review_candidate_semantics(Transport(), original, candidate)
    assert result.review.equivalent
    assert len(requests) == 2
    assert requests[1].user_payload["validationFeedback"] == "review_schema_invalid:json_invalid"
    assert all(r.user_payload["candidate"] == candidate for r in requests)


def relative_volume_candidate(trigger="gt_multiple", **params):
    return {
        "indicator_id": "volume.relative", "definition_version": "1.0.0",
        "trigger": trigger, "params": {"baseline_period": 20, "consecutive_days": 3, **params},
        "value": 1.5,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["gt_multiple", "gte_multiple", "lte_multiple"])
async def test_inactive_operator_parameter_is_absent_but_original_remains_bound(trigger):
    requests = []

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            return provider_verdict(request)

    request = CandidateTransportRequest(
        utterance="贵州茅台，当日成交量超过前20日均量1.5倍买入，持有40日卖出",
        instrument_context="600519.SH", as_of_date=date(2026, 9, 7), max_candidates=1,
        response_schema={}, capability_projection_version="test", capability_projection_hash="test",
        capability_matrix={"indicators": [{
            "indicator_id": "volume.relative", "parameters": [
                {"name": "baseline_period", "default": 20},
                {"name": "consecutive_days", "default": 3},
            ],
        }]}, system_contract="test",
    )
    candidate = {"entry": [{"all": [relative_volume_candidate(trigger)]}]}
    serialized = json.dumps(candidate, ensure_ascii=False, sort_keys=True, allow_nan=False)
    result = await review_candidate_semantics(Transport(), request, candidate)
    assert result.review.equivalent
    assert result.candidate_sha256 == "sha256:" + hashlib.sha256(serialized.encode()).hexdigest()
    assert candidate["entry"][0]["all"][0]["params"]["consecutive_days"] == 3
    projected = requests[0].user_payload["candidate"]["entry"][0]["all"][0]
    assert projected == {**relative_volume_candidate(trigger), "params": {"baseline_period": 20}}
    assert requests[0].user_payload["selectedIndicatorDefinitions"][0]["parameters"] == [
        {"name": "baseline_period", "default": 20},
    ]
    for name in ("SemanticDifference", "RequirementCoverage"):
        paths = requests[0].response_schema["$defs"][name]["properties"]["candidate_path"]["enum"]
        assert "/entry/0/all/0/params/consecutive_days" not in paths
        assert "/entry/0/all/0/params/baseline_period" in paths
        assert "/entry/0/all/0/trigger" in paths and "/entry/0/all/0/value" in paths


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["consecutive_days", "baseline_period"])
async def test_active_parameter_difference_blocks_without_internal_path_in_user_message(field):
    requests = []

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            return {**verdict(), "requirements": [{
                "status": "different", "candidate_path": f"/exit/0/params/{field}",
                "source_quote": request.utterance,
                "requested_meaning": "连续5天超过前10日均量",
                "candidate_meaning": "连续3天超过前20日均量",
            }]}

    request = CandidateTransportRequest(
        utterance="连续5天超过前10日均量卖出", instrument_context="600519.SH",
        as_of_date=date(2026, 9, 7), max_candidates=1, response_schema={},
        capability_projection_version="test", capability_projection_hash="test",
        capability_matrix={"indicators": [{
            "indicator_id": "volume.relative", "parameters": [{"name": "consecutive_days"}],
        }]}, system_contract="test",
    )
    candidate = {
        "entry": [relative_volume_candidate()],
        "exit": [relative_volume_candidate("consecutive_gte_multiple")],
    }
    result = await review_candidate_semantics(Transport(), request, candidate)
    assert not result.review.equivalent
    assert result.review.differences[0].candidate_path == f"/exit/0/params/{field}"
    assert result.review.issues == ["原意为连续5天超过前10日均量；当前为连续3天超过前20日均量"]
    assert result.review.repair_issues[0].startswith(f"/exit/0/params/{field}：")
    projected = requests[0].user_payload["candidate"]
    assert "consecutive_days" not in projected["entry"][0]["params"]
    assert projected["exit"][0] == candidate["exit"][0]
    assert requests[0].user_payload["selectedIndicatorDefinitions"][0]["parameters"] == [
        {"name": "consecutive_days"},
    ]


@pytest.mark.parametrize("changes", [
    {"indicator_id": "future.metric"}, {"definition_version": "2.0.0"},
    {"definition_version": ""}, {"trigger": "future_trigger"},
    {"trigger": "consecutive_gte_multiple"},
    {"indicator_id": "technical.historical_volatility", "trigger": "above",
     "params": {"period": 20, "annualization_sessions": 252}},
])
def test_projection_preserves_unknown_semantics_and_real_runtime_parameters(changes):
    candidate = {**relative_volume_candidate(), **changes}
    assert project_executable_semantics(candidate) == candidate


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rebound", "pullback", "take_profit", "stop_loss"])
async def test_conditional_unused_reference_is_omitted_only_from_review(kind):
    captured = []

    class Transport:
        async def generate_json(self, request):
            captured.append(request)
            return provider_verdict(request)

    request = CandidateTransportRequest(
        utterance="从低点反弹2%买入，持仓成本上涨5%卖出", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 12), max_candidates=1, response_schema={},
        capability_projection_version="test", capability_projection_hash="test",
        capability_matrix={}, system_contract="test",
    )
    rule = {"kind": kind, "side": "buy" if kind == "rebound" else "sell",
            "gap": 2, "gap_unit": "percent", "quantity": 100, "reference_mode": "previous_fill"}
    candidate = {"trading_plan": {"kind": "conditional", "parameters": {"rules": [rule]}}}
    serialized = json.dumps(candidate, ensure_ascii=False, sort_keys=True, allow_nan=False)
    reviewed = await review_candidate_semantics(Transport(), request, candidate)
    projected = captured[0].user_payload["candidate"]["trading_plan"]["parameters"]["rules"][0]
    assert projected == {k: v for k, v in rule.items() if k != "reference_mode"}
    assert rule["reference_mode"] == "previous_fill"
    assert reviewed.candidate_sha256 == "sha256:" + hashlib.sha256(serialized.encode()).hexdigest()


@pytest.mark.parametrize("kind,mode", [
    ("relative_price", "previous_fill"), ("relative_price", "first_observation"),
    ("future_kind", "previous_fill"), ("take_profit", "future_reference"),
])
def test_conditional_active_or_unknown_reference_is_never_hidden(kind, mode):
    rule = {"kind": kind, "side": "sell", "reference_mode": mode}
    assert project_executable_semantics(rule) == rule


@pytest.mark.parametrize("trigger", ["gt_multiple", "gte_multiple", "lte_multiple"])
def test_projected_relative_volume_has_same_provider_and_local_execution(trigger):
    original = relative_volume_candidate(trigger, baseline_period=2)
    original["params"]["unrecognized_future_parameter"] = 9
    projected = project_executable_semantics(original)
    assert projected["params"]["unrecognized_future_parameter"] == 9
    full_condition = IndicatorCondition.model_validate(original)
    projected_condition = IndicatorCondition.model_validate(projected)
    assert provider_binding_for_condition(full_condition) == provider_binding_for_condition(
        projected_condition,
    )
    bars = make_bars([10] * 6, volumes=[100, 100, 200, 50, 300, 100])
    full_facts = SignalRuntime().evaluate_aligned(full_condition, bars)
    projected_facts = SignalRuntime().evaluate_aligned(projected_condition, bars)
    # Condition fingerprints intentionally differ; execution facts must not.
    def meaning(fact):
        return None if fact is None else (fact.triggered, fact.left_value, fact.right_value)

    assert [meaning(fact) for fact in full_facts] == [meaning(fact) for fact in projected_facts]
def test_composed_execution_context_keeps_both_clocks():
    from ashare_lab.adapters.language.candidate_semantic_review import _candidate_execution_context
    for side in ("entry", "exit"):
        context = _candidate_execution_context({
            "trading_plan": {"kind": "scheduled", "parameters": {"at": "close"}},
            side: [{"kind": "indicator", "indicator_id": "technical.macd"}],
        })
        assert context["signal_evaluation"] == "independent_entry_and_exit_legs"
        assert context["indicator_execution"] == "daily_close_confirmation_next_market_session_open"
        assert context["plan_execution"] == "preserve_schedule_or_minute_activation_per_plan"
