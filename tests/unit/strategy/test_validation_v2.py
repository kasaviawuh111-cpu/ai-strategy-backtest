from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from ashare_lab.application.submission_gate_v2 import (
    V2SubmissionRejectedError,
    prepare_v2_backtest_submission,
)
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.instruments import (
    AssetType,
    Exchange,
    InstrumentRef,
    SecurityMasterAssetType,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)
from ashare_lab.domain.provenance import SourceKind, SourceRef
from ashare_lab.domain.strategy.models_v2 import (
    BacktestConfigV2,
    CatalogRefV2,
    ConditionGrounding,
    EventConditionV2,
    FinancialCondition,
    InterpretationCoverage,
    SourceSpan,
    StrategySpecV2,
    TechnicalConditionV2,
)
from ashare_lab.domain.strategy.validation_v2 import (
    ConditionGroundingExpectation,
    DatasetCoverageV2,
    StrategyCandidateV2,
    StrategyV2ValidationContext,
    StrategyV2ValidationError,
    ValidationStage,
    condition_semantics_hash,
    validate_strategy_candidate_v2,
)

ROOT = Path(__file__).resolve().parents[3]
CODE_REVISION = "1" * 40
SNAPSHOT_ID = "composite:" + "a" * 64
CONTENT_HASH = "sha256:" + "b" * 64
ORIGINAL_INPUT = "MACD金叉买入，死叉卖出"


def _master() -> SecurityMasterSnapshot:
    return SecurityMasterSnapshot(
        snapshot_id="security-master:" + "c" * 64,
        records=(
            SecurityMasterRecord(
                symbol="600519.SH",
                name="贵州茅台",
                exchange=Exchange.SH,
                asset_type=SecurityMasterAssetType.STOCK,
                currency="CNY",
                listing_date=date(2001, 8, 27),
                delisting_date=None,
                tradable=True,
                data_source="choice.security_master",
            ),
            SecurityMasterRecord(
                symbol="510300.SH",
                name="沪深300ETF",
                exchange=Exchange.SH,
                asset_type=SecurityMasterAssetType.ETF,
                currency="CNY",
                listing_date=date(2012, 5, 28),
                delisting_date=None,
                tradable=True,
                data_source="choice.security_master",
            ),
            SecurityMasterRecord(
                symbol="000300.SH",
                name="沪深300",
                exchange=Exchange.SH,
                asset_type=SecurityMasterAssetType.INDEX,
                currency="CNY",
                listing_date=date(2005, 4, 8),
                delisting_date=None,
                tradable=False,
                data_source="choice.security_master",
            ),
        ),
    )


def _instrument(asset_type: AssetType = AssetType.STOCK) -> InstrumentRef:
    if asset_type is AssetType.ETF:
        return InstrumentRef(
            symbol="510300.SH",
            name="沪深300ETF",
            exchange=Exchange.SH,
            asset_type=AssetType.ETF,
            currency="CNY",
            listing_date=date(2012, 5, 28),
            delisting_date=None,
            tradable=True,
            data_source="choice.security_master",
        )
    return InstrumentRef(
        symbol="600519.SH",
        name="贵州茅台",
        exchange=Exchange.SH,
        asset_type=AssetType.STOCK,
        currency="CNY",
        listing_date=date(2001, 8, 27),
        delisting_date=None,
        tradable=True,
        data_source="choice.security_master",
    )


def _entry() -> TechnicalConditionV2:
    return TechnicalConditionV2(
        indicator_id="technical.macd",
        definition_version="1.0.0",
        params={"fast": 12, "slow": 26, "signal": 9},
        trigger="golden_cross",
    )


def _exit() -> TechnicalConditionV2:
    return TechnicalConditionV2(
        indicator_id="technical.macd",
        definition_version="1.0.0",
        params={"fast": 12, "slow": 26, "signal": 9},
        trigger="death_cross",
    )


def _spec(
    *,
    instrument: InstrumentRef | None = None,
    entry: object | None = None,
    exit: object | None = None,
) -> StrategySpecV2:
    return StrategySpecV2(
        catalog=CatalogRefV2(catalog_id="cn_a.signals", release_version="2026.08.30"),
        instrument=instrument or _instrument(),
        entry=entry or _entry(),
        exit=exit or _exit(),
        interpretation_coverage=InterpretationCoverage(
            groundings=(
                ConditionGrounding(
                    dsl_path="$.entry",
                    source_start=0,
                    source_end=8,
                    source_text="MACD金叉买入",
                ),
                ConditionGrounding(
                    dsl_path="$.exit",
                    source_start=9,
                    source_end=13,
                    source_text="死叉卖出",
                ),
            ),
            unmapped_material_spans=(),
        ),
        backtest=BacktestConfigV2(
            start=date(2021, 1, 4),
            end=date(2026, 8, 28),
            initial_cash_cny=1_000_000,
        ),
    )


def _source(*, symbol: str = "600519.SH") -> SourceRef:
    return SourceRef(
        provider="choice",
        source_id=f"choice:daily:{symbol}:2021-2026",
        snapshot_id=SNAPSHOT_ID,
        schema_version="choice.daily.v1",
        content_sha256=CONTENT_HASH,
        source_kind=SourceKind.PROVIDER_RECORD,
    )


def _coverage(*, symbol: str = "600519.SH") -> DatasetCoverageV2:
    return DatasetCoverageV2(
        dataset_id="daily_ohlcv",
        instrument_symbol=symbol,
        start=date(2021, 1, 4),
        end=date(2026, 8, 28),
        timezone="Asia/Shanghai",
        availability_field="first_available_at",
        retrieved_at_role="audit_only",
        missing_value_policy="null_or_no_signal",
        source_refs=(_source(symbol=symbol),),
    )


def _expectations(spec: StrategySpecV2 | None = None) -> tuple[ConditionGroundingExpectation, ...]:
    active = spec or _spec()
    return (
        ConditionGroundingExpectation(
            dsl_path="$.entry",
            source_start=0,
            source_end=8,
            source_text="MACD金叉买入",
            condition_hash=condition_semantics_hash(active.entry),
        ),
        ConditionGroundingExpectation(
            dsl_path="$.exit",
            source_start=9,
            source_end=13,
            source_text="死叉卖出",
            condition_hash=condition_semantics_hash(active.exit),
        ),
    )


def _candidate(spec: StrategySpecV2 | None = None) -> StrategyCandidateV2:
    return StrategyCandidateV2(
        candidate_type="strategy_candidate.v2",
        original_input=ORIGINAL_INPUT,
        draft_id="draft:p0b-test",
        revision=1,
        provider="deepseek",
        strategy=spec or _spec(),
    )


def _context(
    *,
    requested_instrument: str = "600519.SH",
    coverage: DatasetCoverageV2 | None = None,
    expectations: tuple[ConditionGroundingExpectation, ...] | None = None,
) -> StrategyV2ValidationContext:
    return StrategyV2ValidationContext(
        security_master=_master(),
        original_input=ORIGINAL_INPUT,
        draft_id="draft:p0b-test",
        revision=1,
        provider="deepseek",
        requested_instrument=requested_instrument,
        expected_backtest=_spec().backtest,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        dataset_coverage=(coverage or _coverage(),),
        grounding_expectations=expectations or _expectations(),
        code_revision=CODE_REVISION,
        trading_calendar_snapshot_id="trading_calendar:" + "d" * 64,
        composite_snapshot_id="composite:" + "f" * 64,
        snapshot_bindings_hash="sha256:" + "9" * 64,
    )


def _assert_failure(
    candidate: object,
    context: StrategyV2ValidationContext,
    *,
    stage: ValidationStage,
    code: str,
) -> None:
    with pytest.raises(StrategyV2ValidationError) as captured:
        validate_strategy_candidate_v2(candidate, context)
    assert captured.value.issue.stage is stage
    assert captured.value.issue.code == code


def test_stock_and_etf_pass_identity_and_full_validation() -> None:
    stock = validate_strategy_candidate_v2(_candidate(), _context())
    assert stock.strategy.instrument.asset_type is AssetType.STOCK
    assert prepare_v2_backtest_submission(stock).plan is stock

    etf_spec = _spec(instrument=_instrument(AssetType.ETF))
    etf_context = _context(
        requested_instrument="510300.SH",
        coverage=_coverage(symbol="510300.SH"),
        expectations=_expectations(etf_spec),
    )
    etf = validate_strategy_candidate_v2(_candidate(etf_spec), etf_context)
    assert etf.strategy.instrument.asset_type is AssetType.ETF


def test_index_request_is_rejected_without_etf_substitution() -> None:
    _assert_failure(
        _candidate(),
        _context(requested_instrument="沪深300"),
        stage=ValidationStage.INSTRUMENT_IDENTITY,
        code="capability_unavailable",
    )


@pytest.mark.parametrize(
    "condition",
    [
        FinancialCondition(
            metric_id="financial.net_profit",
            definition_version="1.0.0",
            report_type="annual",
            period_basis="full_year",
            statement_scope="consolidated",
            revision_policy="as_known_at_signal",
            comparator="gte",
            value=100_000_000,
            unit="CNY",
        ),
        EventConditionV2(
            event_code="event.financial_results.annual_report",
            definition_version="1.0.0",
            trigger="became_available",
        ),
    ],
)
def test_financial_and_event_conditions_are_expressible_but_not_executable(
    condition: object,
) -> None:
    spec = _spec(entry=condition)
    _assert_failure(
        _candidate(spec),
        _context(expectations=_expectations(spec)),
        stage=ValidationStage.CAPABILITY,
        code="capability_unavailable",
    )


def test_unknown_fields_units_periods_and_instruments_fail_schema_or_identity() -> None:
    for field, value in (("executable", True), ("signal", "BUY"), ("facts", ["涨停"])):
        payload = _candidate().model_dump(mode="json")
        payload[field] = value
        _assert_failure(
            payload,
            _context(),
            stage=ValidationStage.SCHEMA,
            code="schema_invalid",
        )

    financial_payload = _spec().model_dump(mode="json")
    financial_payload["entry"] = {
        "type": "financial",
        "metric_id": "financial.net_profit",
        "definition_version": "1.0.0",
        "report_type": "monthly",
        "period_basis": "latest",
        "statement_scope": "consolidated",
        "revision_policy": "as_known_at_signal",
        "comparator": "gte",
        "value": 1,
        "unit": "RMB",
    }
    payload = _candidate().model_dump(mode="json")
    payload["strategy"] = financial_payload
    _assert_failure(
        payload,
        _context(),
        stage=ValidationStage.SCHEMA,
        code="schema_invalid",
    )

    _assert_failure(
        _candidate(),
        _context(requested_instrument="999999.SH"),
        stage=ValidationStage.INSTRUMENT_IDENTITY,
        code="instrument_unconfirmed",
    )


def test_unvalidated_model_copy_cannot_bypass_schema_gate() -> None:
    copied_without_validation = _candidate().model_copy(update={"provider": "", "revision": 0})

    _assert_failure(
        copied_without_validation,
        _context(),
        stage=ValidationStage.SCHEMA,
        code="schema_invalid",
    )


def test_unknown_technical_parameter_fails_catalog_capability() -> None:
    bad_entry = TechnicalConditionV2(
        indicator_id="technical.macd",
        definition_version="1.0.0",
        params={"fast": 12, "slow": 26, "signal": 9, "magic": 1},
        trigger="golden_cross",
    )
    spec = _spec(entry=bad_entry)
    _assert_failure(
        _candidate(spec),
        _context(expectations=_expectations(spec)),
        stage=ValidationStage.CAPABILITY,
        code="unknown_parameter",
    )


def test_pit_policy_must_use_first_available_and_never_retrieved_fallback() -> None:
    wrong = replace(_coverage(), availability_field="retrieved_at")
    _assert_failure(
        _candidate(),
        _context(coverage=wrong),
        stage=ValidationStage.POINT_IN_TIME,
        code="invalid_pit_policy",
    )


def test_data_coverage_must_cover_instrument_and_backtest_period() -> None:
    short = replace(_coverage(), start=date(2022, 1, 4))
    _assert_failure(
        _candidate(),
        _context(coverage=short),
        stage=ValidationStage.DATA_COVERAGE,
        code="data_coverage_incomplete",
    )


def test_omitted_unmapped_or_substituted_material_condition_never_becomes_ready() -> None:
    missing = _spec().model_copy(
        update={
            "interpretation_coverage": InterpretationCoverage(
                groundings=(_spec().interpretation_coverage.groundings[0],),
                unmapped_material_spans=(),
            )
        }
    )
    _assert_failure(
        _candidate(missing),
        _context(),
        stage=ValidationStage.CONDITION_COMPLETENESS,
        code="condition_grounding_incomplete",
    )

    coverage = _spec().interpretation_coverage.model_copy(
        update={
            "unmapped_material_spans": (
                SourceSpan(
                    source_start=9,
                    source_end=13,
                    source_text="死叉卖出",
                ),
            )
        }
    )
    payload = _candidate().model_dump(mode="json")
    payload["strategy"]["interpretation_coverage"] = coverage.model_dump(mode="json")
    _assert_failure(
        payload,
        _context(),
        stage=ValidationStage.CONDITION_COMPLETENESS,
        code="unmapped_material_condition",
    )

    substituted = _spec(
        entry=TechnicalConditionV2(
            indicator_id="technical.macd",
            definition_version="1.0.0",
            params={"fast": 12, "slow": 26, "signal": 9},
            trigger="death_cross",
        )
    )
    _assert_failure(
        _candidate(substituted),
        _context(),
        stage=ValidationStage.CONDITION_COMPLETENESS,
        code="condition_semantics_substituted",
    )


def test_trusted_backtest_and_request_metadata_cannot_be_substituted() -> None:
    changed_backtest = _spec().backtest.model_copy(update={"initial_cash_cny": 1})
    changed_spec = _spec().model_copy(update={"backtest": changed_backtest})
    _assert_failure(
        _candidate(changed_spec),
        _context(),
        stage=ValidationStage.CONDITION_COMPLETENESS,
        code="strategy_field_substituted",
    )

    changed_request = _candidate().model_copy(update={"draft_id": "draft:other"})
    _assert_failure(
        changed_request,
        _context(),
        stage=ValidationStage.CONDITION_COMPLETENESS,
        code="candidate_context_mismatch",
    )


def test_llm_direct_buy_sell_conclusion_is_not_a_candidate_dsl() -> None:
    _assert_failure(
        {
            "candidate_type": "trade_signal",
            "original_input": "现在能买吗",
            "provider": "deepseek",
            "action": "BUY",
            "facts": {"close": 13.82},
        },
        _context(),
        stage=ValidationStage.SCHEMA,
        code="schema_invalid",
    )


def test_candidate_preserves_original_input_byte_for_byte_for_audit() -> None:
    original = f"  {ORIGINAL_INPUT}\n"

    candidate = StrategyCandidateV2(
        candidate_type="strategy_candidate.v2",
        original_input=original,
        draft_id="draft:p0b-original",
        revision=1,
        provider="deepseek",
        strategy=_spec(),
    )

    assert candidate.original_input == original


def test_same_candidate_snapshot_and_code_produce_same_plan_identity() -> None:
    first = validate_strategy_candidate_v2(_candidate(), _context())
    second = validate_strategy_candidate_v2(_candidate(), _context())

    assert first.plan_id == second.plan_id
    assert first.strategy_hash == second.strategy_hash
    assert first.data_snapshot_ids == second.data_snapshot_ids == (SNAPSHOT_ID,)
    assert first.code_revision == second.code_revision == CODE_REVISION

    auxiliary = replace(
        _coverage(),
        dataset_id="corporate_actions",
    )
    first_context = replace(
        _context(),
        dataset_coverage=(_coverage(), auxiliary),
    )
    second_context = replace(
        _context(),
        dataset_coverage=(auxiliary, _coverage()),
    )
    assert (
        validate_strategy_candidate_v2(_candidate(), first_context).plan_id
        == validate_strategy_candidate_v2(_candidate(), second_context).plan_id
    )


def test_submission_gate_rejects_tampered_validator_plan() -> None:
    issued = validate_strategy_candidate_v2(_candidate(), _context())
    substituted = issued.strategy.model_copy(
        update={
            "entry": TechnicalConditionV2(
                indicator_id="technical.not_in_catalog",
                definition_version="1.0.0",
                params={},
                trigger="above",
            )
        }
    )
    tampered = replace(issued, strategy=substituted)

    with pytest.raises(V2SubmissionRejectedError, match="ExecutableStrategyPlan"):
        prepare_v2_backtest_submission(tampered)
