from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.domain.instruments import AssetType, Exchange, InstrumentRef
from ashare_lab.domain.provenance import DataEnvelope, SignalRecord, SourceKind, SourceRef
from ashare_lab.domain.runs.models_v2 import (
    DraftRevisionV2,
    ExecutableStrategyPlanRecordV2,
    RunManifestV2,
    SnapshotBindingV2,
    ValidationReceiptClaimsV2,
    canonical_signal_records_json,
)
from ashare_lab.domain.strategy.canonical import canonical_hash, canonical_json
from ashare_lab.domain.strategy.models_v2 import (
    BacktestConfigV2,
    CatalogRefV2,
    ConditionGrounding,
    InterpretationCoverage,
    StrategySpecV2,
    TechnicalConditionV2,
)
from ashare_lab.domain.time import PointInTimeAvailability

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
GIT_SHA = "1" * 40
NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
BACKTEST_CONFIG = {
    "commission_rate": "0.0003",
    "minimum_commission_cny": "5.00",
    "period": ["2021-01-04", "2026-08-28"],
    "slippage_bps": "5",
}
BACKTEST_CONFIG_JSON = canonical_json(BACKTEST_CONFIG)
BACKTEST_CONFIG_HASH = canonical_hash(BACKTEST_CONFIG)


def _strategy() -> StrategySpecV2:
    return StrategySpecV2(
        catalog=CatalogRefV2(catalog_id="cn_a.signals", release_version="2026.09.01"),
        instrument=InstrumentRef(
            symbol="600519.SH",
            name="贵州茅台",
            exchange=Exchange.SH,
            asset_type=AssetType.STOCK,
            listing_date=date(2001, 8, 27),
            delisting_date=None,
            tradable=True,
            data_source="choice.security_master",
        ),
        entry=TechnicalConditionV2(
            indicator_id="technical.macd",
            definition_version="1.0.0",
            params={"fast": 12, "slow": 26, "signal": 9},
            trigger="golden_cross",
        ),
        exit=TechnicalConditionV2(
            indicator_id="technical.macd",
            definition_version="1.0.0",
            params={"fast": 12, "slow": 26, "signal": 9},
            trigger="death_cross",
        ),
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
        ),
        backtest=BacktestConfigV2(
            start=date(2021, 1, 4),
            end=date(2026, 8, 28),
            initial_cash_cny=1_000_000,
        ),
    )


def _snapshot(kind: str, suffix: str) -> SnapshotBindingV2:
    return SnapshotBindingV2(
        kind=kind,
        snapshot_id=(
            f"composite:{suffix * 64}" if kind == "composite_snapshot" else f"{kind}:{suffix * 64}"
        ),
        provider="choice" if kind != "trading_calendar" else "sse_szse",
        schema_version=f"{kind}.v2",
        content_hash="sha256:" + suffix * 64,
        coverage_start=date(2021, 1, 4),
        coverage_end=date(2026, 8, 28),
        generated_at=NOW,
    )


def test_plan_record_and_manifest_are_canonical_and_tamper_evident() -> None:
    strategy = _strategy()
    strategy_json = canonical_json(strategy)
    producer_children = (
        _snapshot("producer_child", "1").model_copy(
            update={"snapshot_id": "choice:" + "1" * 64, "provider": "Choice Quant API"}
        ),
        _snapshot("producer_child", "2").model_copy(
            update={"snapshot_id": "events:" + "2" * 64, "provider": "eastmoney"}
        ),
    )
    record = ExecutableStrategyPlanRecordV2(
        plan_id=HASH_A,
        draft_id="draft:p0c",
        revision=1,
        strategy_json=strategy_json,
        strategy_hash=canonical_hash(strategy),
        catalog_hash=HASH_B,
        security_master=_snapshot("security_master", "c"),
        trading_calendar=_snapshot("trading_calendar", "d"),
        market_data=_snapshot("market_data", "e"),
        composite_snapshot=_snapshot("composite_snapshot", "f"),
        producer_children=producer_children,
        provider="deepseek",
        validator_version="strategy-v2-gate.2",
        code_revision=GIT_SHA,
        created_at=NOW,
    )
    draft = DraftRevisionV2(
        draft_id="draft:p0c",
        revision=1,
        original_input="MACD金叉买入，死叉卖出",
        provider="deepseek",
        created_at=NOW,
    )
    claims = ValidationReceiptClaimsV2.from_plan(
        record,
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    manifest = RunManifestV2.from_server_records(
        run_id="run:p0c",
        draft=draft,
        plan=record,
        receipt_id="receipt:" + "f" * 64,
        receipt_sha256="sha256:" + "f" * 64,
        engine_run_key="p0c-engine-run-key",
        backtest_config_json=BACKTEST_CONFIG_JSON,
        backtest_config_hash=BACKTEST_CONFIG_HASH,
        fee_policy_version="cn.a_share.cash_equity_fees.2015_present.v1",
        fee_policy_hash="sha256:" + "8" * 64,
        engine_result_hash="sha256:" + "9" * 64,
        created_at=NOW,
    )

    assert claims.market_data == record.market_data
    assert claims.composite_snapshot == record.composite_snapshot
    assert claims.producer_children == producer_children
    assert manifest.original_input == draft.original_input
    assert manifest.plan_id == record.plan_id
    assert manifest.composite_snapshot == record.composite_snapshot
    assert manifest.producer_children == producer_children
    assert manifest.engine_run_key == "p0c-engine-run-key"
    assert manifest.final_strategy_json == strategy_json
    assert manifest.manifest_hash == canonical_hash(manifest.canonical_payload())

    with pytest.raises(ValueError, match="strategy_hash"):
        ExecutableStrategyPlanRecordV2(
            **{
                **record.model_dump(),
                "strategy_hash": "sha256:" + "0" * 64,
            }
        )


@pytest.mark.parametrize(
    "secret",
    (
        "sk-1234567890abcdefghijklmnop",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    ),
)
def test_draft_rejects_secrets_instead_of_persisting_them(secret: str) -> None:
    with pytest.raises(ValueError, match="sensitive credential"):
        DraftRevisionV2(
            draft_id="draft:secret",
            revision=1,
            original_input=f"MACD金叉 {secret}",
            provider="deepseek",
            created_at=NOW,
        )


def test_all_persisted_times_are_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DraftRevisionV2(
            draft_id="draft:naive",
            revision=1,
            original_input="MACD金叉买入，死叉卖出",
            provider="deepseek",
            created_at=datetime(2026, 8, 31, 12),
        )


def test_snapshot_provider_preserves_the_real_provider_name() -> None:
    snapshot = _snapshot("market_data", "e").model_copy(update={"provider": "Choice Quant API"})
    validated = SnapshotBindingV2.model_validate(snapshot.model_dump())
    assert validated.provider == "Choice Quant API"

    with pytest.raises(ValueError, match="provider"):
        SnapshotBindingV2.model_validate({**snapshot.model_dump(), "provider": "Choice\nQuant API"})


def test_manifest_persists_complete_triggered_signal_evidence() -> None:
    strategy = _strategy()
    signal_at = datetime(2026, 8, 28, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    source = SourceRef(
        provider="choice",
        source_id="choice:daily:600519.SH:2026-08-28",
        snapshot_id="market_data:" + "e" * 64,
        schema_version="choice.daily.v1",
        content_sha256="sha256:" + "e" * 64,
        source_kind=SourceKind.PROVIDER_RECORD,
    )
    envelope = DataEnvelope(
        data_id="daily.close",
        value=Decimal("1468.00"),
        unit="CNY",
        availability=PointInTimeAvailability(
            observed_at=signal_at,
            announced_at=None,
            first_available_at=signal_at,
            signal_at=signal_at,
            execution_at=None,
            retrieved_at=NOW.astimezone(ZoneInfo("Asia/Shanghai")),
            timezone="Asia/Shanghai",
            source="choice",
            revision_id="choice:600519.SH:2026-08-28",
        ),
        source_refs=(source,),
    )
    signal = SignalRecord(
        instrument_id="600519.SH",
        condition_id="sha256:" + "7" * 64,
        condition_ref="$.entry",
        triggered=True,
        signal_at=signal_at,
        plan_id=HASH_A,
        strategy_hash=canonical_hash(strategy),
        dsl_schema_version="strategy.v2",
        code_revision=GIT_SHA,
        input_envelopes=(envelope,),
        source_refs=(source,),
    )
    draft = DraftRevisionV2(
        draft_id="draft:p0c-signal",
        revision=1,
        original_input="MACD金叉买入，死叉卖出",
        provider="deepseek",
        created_at=NOW,
    )
    plan = ExecutableStrategyPlanRecordV2(
        plan_id=HASH_A,
        draft_id=draft.draft_id,
        revision=draft.revision,
        strategy_json=canonical_json(strategy),
        strategy_hash=canonical_hash(strategy),
        catalog_hash=HASH_B,
        security_master=_snapshot("security_master", "c"),
        trading_calendar=_snapshot("trading_calendar", "d"),
        market_data=_snapshot("market_data", "e"),
        provider=draft.provider,
        validator_version="strategy-v2-gate.2",
        code_revision=GIT_SHA,
        created_at=NOW,
    )
    manifest = RunManifestV2.from_server_records(
        run_id="run:p0c-signal",
        draft=draft,
        plan=plan,
        receipt_id="receipt:" + "f" * 64,
        receipt_sha256="sha256:" + "f" * 64,
        engine_run_key="p0c-signal-engine-key",
        backtest_config_json=BACKTEST_CONFIG_JSON,
        backtest_config_hash=BACKTEST_CONFIG_HASH,
        fee_policy_version="cn.a_share.cash_equity_fees.2015_present.v1",
        fee_policy_hash="sha256:" + "8" * 64,
        engine_result_hash="sha256:" + "9" * 64,
        signal_records_json=canonical_signal_records_json((signal,)),
        created_at=NOW,
    )

    assert manifest.signal_records == (signal,)
    assert signal.fingerprint in manifest.signal_records_json
    assert (
        manifest.manifest_hash
        != manifest.model_copy(update={"signal_records_json": "[]"}).manifest_hash
    )


def _bound_signal(
    *,
    plan_id: str = HASH_A,
    strategy_hash: str | None = None,
    code_revision: str = GIT_SHA,
    snapshot_id: str = "market_data:" + "e" * 64,
    source_kind: SourceKind = SourceKind.PROVIDER_RECORD,
) -> SignalRecord:
    signal_at = datetime(2026, 8, 28, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    provider = (
        "ashare_lab.technical_v2" if source_kind is SourceKind.DETERMINISTIC_TRANSFORM else "choice"
    )
    source = SourceRef(
        provider=provider,
        source_id="daily:600519.SH:2026-08-28",
        snapshot_id=snapshot_id,
        schema_version="canonical.daily.v1",
        content_sha256="sha256:" + "e" * 64,
        source_kind=source_kind,
    )
    envelope = DataEnvelope(
        data_id="daily.close",
        value=Decimal("1468.00"),
        unit="CNY",
        availability=PointInTimeAvailability(
            observed_at=signal_at,
            announced_at=None,
            first_available_at=signal_at,
            signal_at=signal_at,
            execution_at=None,
            retrieved_at=NOW.astimezone(ZoneInfo("Asia/Shanghai")),
            timezone="Asia/Shanghai",
            source=provider,
            revision_id="600519.SH:2026-08-28",
        ),
        source_refs=(source,),
    )
    return SignalRecord(
        instrument_id="600519.SH",
        condition_id="sha256:" + "7" * 64,
        condition_ref="$.entry",
        triggered=True,
        signal_at=signal_at,
        plan_id=plan_id,
        strategy_hash=strategy_hash or canonical_hash(_strategy()),
        dsl_schema_version="strategy.v2",
        code_revision=code_revision,
        input_envelopes=(envelope,),
        source_refs=(source,),
    )


def _manifest_for_binding_tests(signal: SignalRecord) -> RunManifestV2:
    strategy = _strategy()
    draft = DraftRevisionV2(
        draft_id="draft:p0c-bindings",
        revision=1,
        original_input="MACD金叉买入，死叉卖出",
        provider="deepseek",
        created_at=NOW,
    )
    plan = ExecutableStrategyPlanRecordV2(
        plan_id=HASH_A,
        draft_id=draft.draft_id,
        revision=draft.revision,
        strategy_json=canonical_json(strategy),
        strategy_hash=canonical_hash(strategy),
        catalog_hash=HASH_B,
        security_master=_snapshot("security_master", "c"),
        trading_calendar=_snapshot("trading_calendar", "d"),
        market_data=_snapshot("market_data", "e"),
        provider=draft.provider,
        validator_version="strategy-v2-gate.2",
        code_revision=GIT_SHA,
        created_at=NOW,
    )
    return RunManifestV2.from_server_records(
        run_id="run:p0c-bindings",
        draft=draft,
        plan=plan,
        receipt_id="receipt:" + "f" * 64,
        receipt_sha256="sha256:" + "f" * 64,
        engine_run_key="p0c-engine-key",
        backtest_config_json=BACKTEST_CONFIG_JSON,
        backtest_config_hash=BACKTEST_CONFIG_HASH,
        fee_policy_version="cn.a_share.cash_equity_fees.2015_present.v1",
        fee_policy_hash="sha256:" + "8" * 64,
        engine_result_hash="sha256:" + "9" * 64,
        signal_records_json=canonical_signal_records_json((signal,)),
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    (
        ("engine_run_key", "another-engine-key"),
        ("backtest_config_hash", "sha256:" + "6" * 64),
        ("fee_policy_version", "cn.a_share.cash_equity_fees.v2"),
        ("fee_policy_hash", "sha256:" + "5" * 64),
    ),
)
def test_execution_assumptions_enter_manifest_hash(
    field_name: str,
    replacement: str,
) -> None:
    manifest = _manifest_for_binding_tests(_bound_signal())
    changed = manifest.model_copy(update={field_name: replacement})
    assert changed.manifest_hash != manifest.manifest_hash


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    (
        ("backtest_config_hash", "sha256:" + "0" * 64, "backtest_config_hash"),
        ("backtest_config_json", '{"z":1, "a":2}', "canonical"),
        ("backtest_config_json", '["not-an-object"]', "object"),
        (
            "backtest_config_json",
            '{"api_key":"sk-1234567890abcdefghijklmnop"}',
            "sensitive|forbidden",
        ),
        (
            "engine_run_key",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            "sensitive credential",
        ),
    ),
)
def test_forged_or_sensitive_execution_assumptions_are_rejected(
    field_name: str,
    value: str,
    message: str,
) -> None:
    manifest = _manifest_for_binding_tests(_bound_signal())
    with pytest.raises(ValueError, match=message):
        RunManifestV2.model_validate({**manifest.model_dump(), field_name: value})


@pytest.mark.parametrize(
    "signal",
    (
        _bound_signal(plan_id="sha256:" + "0" * 64),
        _bound_signal(strategy_hash="sha256:" + "0" * 64),
        _bound_signal(code_revision="0" * 40),
        _bound_signal(snapshot_id="market_data:" + "0" * 64),
    ),
)
def test_signal_cannot_cross_manifest_plan_strategy_code_or_snapshot(
    signal: SignalRecord,
) -> None:
    with pytest.raises(ValueError, match="signal record"):
        _manifest_for_binding_tests(signal)


def test_deterministic_transform_on_the_same_snapshot_is_allowed() -> None:
    signal = _bound_signal(source_kind=SourceKind.DETERMINISTIC_TRANSFORM)
    assert _manifest_for_binding_tests(signal).signal_records == (signal,)
