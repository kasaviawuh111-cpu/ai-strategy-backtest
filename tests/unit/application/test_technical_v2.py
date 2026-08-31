from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.application.daily_backtest import DailyBacktestConfig, DailyBacktestInput
from ashare_lab.application.technical_v2 import (
    TechnicalV2BacktestInput,
    TechnicalV2ExecutionError,
    adapt_validated_technical_plan,
    bind_stock_fee_provider,
    build_technical_signal_records,
    create_etf_all_in_fee_provider,
    generate_technical_signal_records,
    run_validated_technical_v2,
)
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.execution import AshareExchange, FeeCalculator, FeePolicy, TradingCalendar
from ashare_lab.domain.instruments import (
    AssetType,
    Exchange,
    InstrumentRef,
    SecurityMasterAssetType,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)
from ashare_lab.domain.market_data import (
    Board,
    DailyBar,
    InstrumentSession,
    TradingStatus,
)
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.runs import SnapshotBindingV2
from ashare_lab.domain.shared import (
    CurrencyMismatchError,
    DomainValidationError,
    InstrumentId,
    Money,
    Price,
    Quantity,
)
from ashare_lab.domain.strategy.models_v2 import (
    BacktestConfigV2,
    CatalogRefV2,
    ConditionGrounding,
    InterpretationCoverage,
    LeafConditionV2,
    StrategySpecV2,
    TechnicalConditionV2,
)
from ashare_lab.domain.strategy.validation_v2 import (
    ConditionGroundingExpectation,
    DatasetCoverageV2,
    StrategyCandidateV2,
    StrategyV2ValidationContext,
    condition_semantics_hash,
    validate_strategy_candidate_v2,
)

ROOT = Path(__file__).resolve().parents[3]
TZ = ZoneInfo("Asia/Shanghai")
START = date(2025, 1, 2)
END = START + timedelta(days=6)
CODE_REVISION = "1" * 40
SNAPSHOT_ID = "market:" + "a" * 64
SNAPSHOT_HASH = "sha256:" + "b" * 64
ORIGINAL_INPUT = "收盘上穿2日均线买入，下穿2日均线卖出"


def _instrument(
    asset_type: AssetType = AssetType.STOCK,
    exchange: Exchange = Exchange.SH,
) -> InstrumentRef:
    is_etf = asset_type is AssetType.ETF
    symbol = "510300.SH" if is_etf else "600519.SH"
    name = "沪深300ETF" if is_etf else "贵州茅台"
    listing_date = date(2012, 5, 28) if is_etf else date(2001, 8, 27)
    if exchange is Exchange.SZ:
        symbol = "159919.SZ" if is_etf else "000001.SZ"
        name = "沪深300ETF" if is_etf else "平安银行"
        listing_date = date(2012, 5, 28) if is_etf else date(1991, 4, 3)
    return InstrumentRef(
        symbol=symbol,
        name=name,
        exchange=exchange,
        asset_type=asset_type,
        currency="CNY",
        listing_date=listing_date,
        delisting_date=None,
        tradable=True,
        data_source="choice.security_master",
    )


def _master() -> SecurityMasterSnapshot:
    def record(instrument: InstrumentRef) -> SecurityMasterRecord:
        payload = instrument.model_dump(mode="python")
        payload["asset_type"] = SecurityMasterAssetType(instrument.asset_type.value)
        return SecurityMasterRecord.model_validate(payload)

    return SecurityMasterSnapshot(
        snapshot_id="security-master:" + "c" * 64,
        records=tuple(
            record(instrument)
            for instrument in (
                _instrument(),
                _instrument(AssetType.ETF),
                _instrument(exchange=Exchange.SZ),
                _instrument(AssetType.ETF, Exchange.SZ),
            )
        ),
    )


def _condition(trigger: str) -> TechnicalConditionV2:
    return TechnicalConditionV2(
        indicator_id="technical.ma",
        definition_version="1.0.0",
        params={"period": 2, "price_field": "close"},
        trigger=trigger,
    )


def _spec(
    asset_type: AssetType = AssetType.STOCK,
    exchange: Exchange = Exchange.SH,
) -> StrategySpecV2:
    return StrategySpecV2(
        catalog=CatalogRefV2(catalog_id="cn_a.signals", release_version="2026.08.30"),
        instrument=_instrument(asset_type, exchange),
        entry=_condition("price_crosses_above"),
        exit=_condition("price_crosses_below"),
        interpretation_coverage=InterpretationCoverage(
            groundings=(
                ConditionGrounding(
                    dsl_path="$.entry",
                    source_start=0,
                    source_end=10,
                    source_text=ORIGINAL_INPUT[0:10],
                ),
                ConditionGrounding(
                    dsl_path="$.exit",
                    source_start=11,
                    source_end=len(ORIGINAL_INPUT),
                    source_text=ORIGINAL_INPUT[11:],
                ),
            )
        ),
        backtest=BacktestConfigV2(
            start=START,
            end=END,
            initial_cash_cny=100_000,
        ),
    )


def _plan(
    asset_type: AssetType = AssetType.STOCK,
    exchange: Exchange = Exchange.SH,
):
    spec = _spec(asset_type, exchange)
    symbol = spec.instrument.symbol
    from ashare_lab.domain.provenance import SourceRef

    source = SourceRef(
        provider="choice",
        source_id=f"choice:daily:{symbol}",
        snapshot_id=SNAPSHOT_ID,
        schema_version="choice.daily.v1",
        content_sha256=SNAPSHOT_HASH,
    )
    coverage = DatasetCoverageV2(
        dataset_id="daily_ohlcv",
        instrument_symbol=symbol,
        start=START,
        end=END,
        timezone="Asia/Shanghai",
        availability_field="first_available_at",
        retrieved_at_role="audit_only",
        missing_value_policy="null_or_no_signal",
        source_refs=(source,),
    )
    expectations = tuple(
        ConditionGroundingExpectation(
            dsl_path=grounding.dsl_path,
            source_start=grounding.source_start,
            source_end=grounding.source_end,
            source_text=grounding.source_text,
            condition_hash=condition_semantics_hash(cast(LeafConditionV2, condition)),
        )
        for grounding, condition in zip(
            spec.interpretation_coverage.groundings,
            (spec.entry, spec.exit),
            strict=True,
        )
    )
    candidate = StrategyCandidateV2(
        original_input=ORIGINAL_INPUT,
        draft_id="draft:p0c-technical",
        revision=1,
        provider="deepseek",
        strategy=spec,
    )
    context = StrategyV2ValidationContext(
        security_master=_master(),
        original_input=ORIGINAL_INPUT,
        draft_id="draft:p0c-technical",
        revision=1,
        provider="deepseek",
        requested_instrument=symbol,
        expected_backtest=spec.backtest,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        dataset_coverage=(coverage,),
        grounding_expectations=expectations,
        code_revision=CODE_REVISION,
    )
    return validate_strategy_candidate_v2(candidate, context)


def _binding() -> SnapshotBindingV2:
    return SnapshotBindingV2(
        kind="market_data",
        snapshot_id=SNAPSHOT_ID,
        provider="choice",
        schema_version="choice.daily.v1",
        content_hash=SNAPSHOT_HASH,
        coverage_start=START,
        coverage_end=END,
        generated_at=datetime(2025, 1, 9, 12, tzinfo=TZ),
    )


def _bars(
    asset_type: AssetType = AssetType.STOCK,
    exchange: Exchange = Exchange.SH,
) -> tuple[DailyBar, ...]:
    instrument = InstrumentId(_instrument(asset_type, exchange).symbol)
    closes = ("10", "9", "11", "12", "8", "7", "7")
    return tuple(
        DailyBar(
            instrument_id=instrument,
            session_date=START + timedelta(days=index),
            open=Price(Decimal(close)),
            high=Price(Decimal(close)),
            low=Price(Decimal(close)),
            close=Price(Decimal(close)),
            volume=Quantity(1_000_000),
            turnover=Decimal(close) * Decimal("1000000"),
            available_at=datetime.combine(
                START + timedelta(days=index),
                datetime.min.time().replace(hour=15),
                tzinfo=TZ,
            ),
        )
        for index, close in enumerate(closes)
    )


def _sessions(source: tuple[DailyBar, ...]) -> tuple[InstrumentSession, ...]:
    return tuple(
        InstrumentSession(
            instrument_id=bar.instrument_id,
            session_date=bar.session_date,
            board=Board.MAIN,
            status=TradingStatus.TRADING,
            previous_close=Price(Decimal("10")),
            upper_limit=Price(Decimal("50")),
            lower_limit=Price(Decimal("1")),
            minimum_buy_quantity=100,
            buy_quantity_increment=100,
        )
        for bar in source
    )


def _fees(exchange: AshareExchange = AshareExchange.SHANGHAI) -> FeeCalculator:
    return FeeCalculator(
        FeePolicy(
            exchange=exchange,
            commission_rate=Decimal("0.0003"),
            minimum_commission=Money(Decimal("5")),
        )
    )


def _etf_fees():
    return create_etf_all_in_fee_provider(
        exchange=Exchange.SH,
        commission_rate=Decimal("0.0003"),
        minimum_commission=Money(Decimal("5")),
    )


def _engine_input(
    asset_type: AssetType = AssetType.STOCK,
    exchange: Exchange = Exchange.SH,
) -> TechnicalV2BacktestInput:
    source = _bars(asset_type, exchange)
    return TechnicalV2BacktestInput(
        plan=_plan(asset_type, exchange),
        market_snapshot=_binding(),
        run_key="p0c-v2",
        bars=source,
        sessions=_sessions(source),
        calendar=TradingCalendar(
            version="test-calendar",
            sessions=tuple(START + timedelta(days=index) for index in range(10)),
        ),
        fee_calculator=bind_stock_fee_provider(_fees()),
        config=DailyBacktestConfig(
            participation_rate=Decimal("1"),
            slippage_bps=Decimal("0"),
            allocation_ratio=Decimal("0.9"),
        ),
    )


def test_raw_candidate_or_model_trade_conclusion_cannot_reach_adapter() -> None:
    with pytest.raises(TechnicalV2ExecutionError, match="validator-issued") as captured:
        adapt_validated_technical_plan({"action": "BUY", "facts": {"close": 10}})
    assert captured.value.code == "plan_untrusted"


def test_signal_factory_derives_condition_and_sources_from_server_inputs() -> None:
    request = _engine_input()
    records = build_technical_signal_records(
        request.plan,
        request.market_snapshot,
        request.bars,
    )

    assert records
    assert {artifact.record.condition_ref for artifact in records} == {"$.entry", "$.exit"}
    assert all(artifact.record.condition_id == artifact.condition_id for artifact in records)
    assert all(artifact.record.source_refs for artifact in records)
    assert all(len(artifact.record.input_envelopes) == 1 for artifact in records)
    assert all(artifact.record.data_snapshot_ids == (SNAPSHOT_ID,) for artifact in records)
    assert all(artifact.record.code_revision == CODE_REVISION for artifact in records)
    assert len({artifact.condition_id for artifact in records}) == 2
    assert generate_technical_signal_records(
        request.plan,
        request.bars,
        request.market_snapshot,
    ) == tuple(artifact.record for artifact in records)


def test_signal_factory_is_deterministic_and_rejects_snapshot_or_future_mismatch() -> None:
    request = _engine_input()
    first = build_technical_signal_records(
        request.plan,
        request.market_snapshot,
        request.bars,
    )
    second = build_technical_signal_records(
        request.plan,
        request.market_snapshot,
        request.bars,
    )
    assert tuple(item.record.fingerprint for item in first) == tuple(
        item.record.fingerprint for item in second
    )

    changed = request.market_snapshot.model_copy(update={"snapshot_id": "market:" + "f" * 64})
    with pytest.raises(TechnicalV2ExecutionError) as captured:
        build_technical_signal_records(request.plan, changed, request.bars)
    assert captured.value.code == "data_unavailable"

    changed_hash = request.market_snapshot.model_copy(update={"content_hash": "sha256:" + "f" * 64})
    with pytest.raises(TechnicalV2ExecutionError) as captured:
        build_technical_signal_records(request.plan, changed_hash, request.bars)
    assert captured.value.code == "data_unavailable"

    wrong_kind = request.market_snapshot.model_copy(update={"kind": "trading_calendar"})
    with pytest.raises(TechnicalV2ExecutionError) as captured:
        build_technical_signal_records(request.plan, wrong_kind, request.bars)
    assert captured.value.code == "data_unavailable"

    future = list(request.bars)
    future[2] = replace(
        future[2],
        available_at=datetime(2026, 1, 1, 15, tzinfo=TZ),
    )
    with pytest.raises(TechnicalV2ExecutionError) as captured:
        build_technical_signal_records(request.plan, request.market_snapshot, tuple(future))
    assert captured.value.code == "lookahead_detected"


def test_stock_v2_is_a_thin_adapter_with_identical_v1_trades_and_result_hash() -> None:
    request = _engine_input()
    adapted = adapt_validated_technical_plan(request.plan)
    v1 = __import__(
        "ashare_lab.application.daily_backtest",
        fromlist=["run_daily_backtest"],
    ).run_daily_backtest(
        DailyBacktestInput(
            run_key=request.run_key,
            strategy=adapted,
            bars=request.bars,
            sessions=request.sessions,
            calendar=request.calendar,
            fee_calculator=request.fee_calculator,
            config=request.config,
        )
    )
    v2 = run_validated_technical_v2(request)

    assert v2.result.fills == v1.fills
    assert v2.result.orders == v1.orders
    assert v2.result.content_hash == v1.content_hash
    assert v2.v1_strategy == adapted
    assert all(record.source_refs for record in v2.signal_records)


def test_stock_fee_provider_cannot_execute_etf_even_with_exact_fixture_data() -> None:
    request = _engine_input(AssetType.ETF)
    assert build_technical_signal_records(
        request.plan,
        request.market_snapshot,
        request.bars,
    )
    with pytest.raises(TechnicalV2ExecutionError) as captured:
        run_validated_technical_v2(request)
    assert captured.value.code == "capability_unavailable"

    wrong_binding = request.market_snapshot.model_copy(
        update={"coverage_end": END - timedelta(days=1)}
    )
    with pytest.raises(TechnicalV2ExecutionError) as captured:
        build_technical_signal_records(request.plan, wrong_binding, request.bars)
    assert captured.value.code == "data_unavailable"


def test_etf_all_in_policy_is_versioned_validated_and_content_hashed() -> None:
    first = _etf_fees()
    same = _etf_fees()
    changed = create_etf_all_in_fee_provider(
        exchange=Exchange.SH,
        commission_rate=Decimal("0.0002"),
        minimum_commission=Money(Decimal("5")),
    )

    assert first.supported_asset_type is AssetType.ETF
    assert first.policy_version == "cn.a_share.secondary_market_stock_etf_all_in_fees.v1"
    assert first.parameters_hash.startswith("sha256:")
    assert first.parameters_hash == same.parameters_hash
    assert first.parameters_hash != changed.parameters_hash
    assert first.supported_exchange is Exchange.SH
    with pytest.raises(ValueError, match="commission_rate"):
        create_etf_all_in_fee_provider(
            exchange=Exchange.SH,
            commission_rate=Decimal("-0.0001"),
            minimum_commission=Money(Decimal("5")),
        )
    with pytest.raises(ValueError, match="minimum_commission"):
        create_etf_all_in_fee_provider(
            exchange=Exchange.SH,
            commission_rate=Decimal("0.0003"),
            minimum_commission=Money(Decimal("-1")),
        )


def test_etf_all_in_policy_charges_only_bilateral_broker_commission() -> None:
    provider = _etf_fees()
    buy = provider.calculate(
        side=OrderSide.BUY,
        price=Price(Decimal("123.456")),
        quantity=Quantity(1_000),
        trade_date=date(2025, 1, 2),
    )
    sell = provider.calculate(
        side=OrderSide.SELL,
        price=Price(Decimal("123.456")),
        quantity=Quantity(1_000),
        trade_date=date(2025, 1, 3),
    )

    assert buy == sell
    assert buy.commission == Money(Decimal("37.04"))
    assert buy.stamp_tax == Money.zero("CNY")
    assert buy.transfer_fee == Money.zero("CNY")
    assert buy.other == Money.zero("CNY")
    with pytest.raises(CurrencyMismatchError):
        provider.calculate(
            side=OrderSide.BUY,
            price=Price(Decimal("10"), "USD"),
            quantity=Quantity(100),
            trade_date=date(2025, 1, 2),
        )
    with pytest.raises(DomainValidationError, match="side"):
        provider.calculate(
            side="BUY",  # type: ignore[arg-type]
            price=Price(Decimal("10")),
            quantity=Quantity(100),
            trade_date=date(2025, 1, 2),
        )


def test_etf_fixture_v2_executes_with_etf_policy_and_matches_direct_v1() -> None:
    request = replace(
        _engine_input(AssetType.ETF),
        fee_calculator=_etf_fees(),
    )
    adapted = adapt_validated_technical_plan(request.plan)
    direct = __import__(
        "ashare_lab.application.daily_backtest",
        fromlist=["run_daily_backtest"],
    ).run_daily_backtest(
        DailyBacktestInput(
            run_key=request.run_key,
            strategy=adapted,
            bars=request.bars,
            sessions=request.sessions,
            calendar=request.calendar,
            fee_calculator=request.fee_calculator,
            config=request.config,
        )
    )
    completed = run_validated_technical_v2(request)

    assert request.plan.strategy.instrument == _instrument(AssetType.ETF)
    assert {fill.side for fill in completed.result.fills} == {
        OrderSide.BUY,
        OrderSide.SELL,
    }
    assert all(fill.fees.commission.amount > 0 for fill in completed.result.fills)
    assert all(fill.fees.stamp_tax.amount == 0 for fill in completed.result.fills)
    assert all(fill.fees.transfer_fee.amount == 0 for fill in completed.result.fills)
    assert all(fill.fees.other.amount == 0 for fill in completed.result.fills)
    assert completed.result.orders == direct.orders
    assert completed.result.fills == direct.fills
    assert completed.result.content_hash == direct.content_hash


def test_unattested_or_wrong_asset_fee_provider_fails_closed() -> None:
    request = _engine_input()
    with pytest.raises(TechnicalV2ExecutionError) as captured:
        run_validated_technical_v2(replace(request, fee_calculator=_fees()))
    assert captured.value.code == "capability_unavailable"

    with pytest.raises(TechnicalV2ExecutionError) as captured:
        run_validated_technical_v2(replace(request, fee_calculator=_etf_fees()))
    assert captured.value.code == "capability_unavailable"


def test_stock_fee_attestation_derives_policy_hash_and_binds_exchange() -> None:
    raw_sh = _fees(AshareExchange.SHANGHAI)
    same_sh = _fees(AshareExchange.SHANGHAI)
    raw_sz = _fees(AshareExchange.SHENZHEN)
    attested_sh = bind_stock_fee_provider(raw_sh)
    attested_same = bind_stock_fee_provider(same_sh)
    attested_sz = bind_stock_fee_provider(raw_sz)

    assert attested_sh.supported_exchange is Exchange.SH
    assert attested_sz.supported_exchange is Exchange.SZ
    assert attested_sh.parameters_hash == attested_same.parameters_hash
    assert attested_sh.parameters_hash != attested_sz.parameters_hash
    assert attested_sh.calculate(
        side=OrderSide.SELL,
        price=Price(Decimal("123.456")),
        quantity=Quantity(1_000),
        trade_date=date(2025, 1, 3),
    ) == raw_sh.calculate(
        side=OrderSide.SELL,
        price=Price(Decimal("123.456")),
        quantity=Quantity(1_000),
        trade_date=date(2025, 1, 3),
    )

    sz_request = _engine_input(exchange=Exchange.SZ)
    with pytest.raises(TechnicalV2ExecutionError, match="exchange") as captured:
        run_validated_technical_v2(sz_request)
    assert captured.value.code == "capability_unavailable"

    completed = run_validated_technical_v2(
        replace(
            sz_request,
            fee_calculator=attested_sz,
        )
    )
    assert completed.result.fills


def test_engine_rejects_bar_instrument_substitution() -> None:
    request = _engine_input()
    changed = tuple(replace(bar, instrument_id=InstrumentId("000001.SZ")) for bar in request.bars)
    with pytest.raises(TechnicalV2ExecutionError) as captured:
        run_validated_technical_v2(replace(request, bars=changed))
    assert captured.value.code == "instrument_mismatch"
