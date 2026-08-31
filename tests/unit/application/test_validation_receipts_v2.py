from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from ashare_lab.adapters.persistence import create_backtest_run_engine
from ashare_lab.adapters.persistence.strategy_v2_artifacts import (
    SQLAlchemyStrategyV2ArtifactStore,
    create_strategy_v2_artifact_schema,
)
from ashare_lab.application.submission_gate_v2 import (
    V2SubmissionRejectedError,
    prepare_v2_backtest_submission,
)
from ashare_lab.application.validation_receipts_v2 import (
    PersistentPlanRecoveryError,
    ValidationReceiptServiceV2,
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
from ashare_lab.domain.runs.models_v2 import DraftRevisionV2, SnapshotBindingV2
from ashare_lab.domain.strategy.canonical import canonical_json
from ashare_lab.domain.strategy.models_v2 import (
    BacktestConfigV2,
    CatalogRefV2,
    ConditionGrounding,
    InterpretationCoverage,
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
NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
KEY = b"p0c-test-server-signing-key-is-at-least-32-bytes"
GIT_SHA = "1" * 40
SNAPSHOT_ID = "market_data:" + "e" * 64


def _master() -> SecurityMasterSnapshot:
    return SecurityMasterSnapshot(
        snapshot_id="security_master:" + "c" * 64,
        records=(
            SecurityMasterRecord(
                symbol="600519.SH",
                name="贵州茅台",
                exchange=Exchange.SH,
                asset_type=SecurityMasterAssetType.STOCK,
                listing_date=date(2001, 8, 27),
                tradable=True,
                data_source="choice.security_master",
            ),
        ),
    )


def _strategy() -> StrategySpecV2:
    entry = TechnicalConditionV2(
        indicator_id="technical.macd",
        definition_version="1.0.0",
        params={"fast": 12, "slow": 26, "signal": 9},
        trigger="golden_cross",
    )
    exit = entry.model_copy(update={"trigger": "death_cross"})
    return StrategySpecV2(
        catalog=CatalogRefV2(catalog_id="cn_a.signals", release_version="2026.08.30"),
        instrument=InstrumentRef(
            symbol="600519.SH",
            name="贵州茅台",
            exchange=Exchange.SH,
            asset_type=AssetType.STOCK,
            listing_date=date(2001, 8, 27),
            tradable=True,
            data_source="choice.security_master",
        ),
        entry=entry,
        exit=exit,
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


def _source() -> SourceRef:
    return SourceRef(
        provider="choice",
        source_id="choice:daily:600519.SH:2021-2026",
        snapshot_id=SNAPSHOT_ID,
        schema_version="choice.daily.v1",
        content_sha256="sha256:" + "e" * 64,
        source_kind=SourceKind.PROVIDER_RECORD,
    )


def _context(strategy: StrategySpecV2 | None = None) -> StrategyV2ValidationContext:
    active = strategy or _strategy()
    return StrategyV2ValidationContext(
        security_master=_master(),
        original_input="MACD金叉买入，死叉卖出",
        draft_id="draft:p0c-receipt",
        revision=1,
        provider="deepseek",
        requested_instrument="600519.SH",
        expected_backtest=active.backtest,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        dataset_coverage=(
            DatasetCoverageV2(
                dataset_id="daily_ohlcv",
                instrument_symbol="600519.SH",
                start=date(2021, 1, 4),
                end=date(2026, 8, 28),
                timezone="Asia/Shanghai",
                availability_field="first_available_at",
                retrieved_at_role="audit_only",
                missing_value_policy="null_or_no_signal",
                source_refs=(_source(),),
            ),
        ),
        grounding_expectations=(
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
        ),
        code_revision=GIT_SHA,
    )


def _bindings() -> tuple[SnapshotBindingV2, SnapshotBindingV2, SnapshotBindingV2]:
    def binding(kind: str, suffix: str, provider: str = "choice") -> SnapshotBindingV2:
        return SnapshotBindingV2(
            kind=kind,
            snapshot_id=(
                _master().snapshot_id
                if kind == "security_master"
                else (SNAPSHOT_ID if kind == "market_data" else f"{kind}:{suffix * 64}")
            ),
            provider=provider,
            schema_version=f"{kind}.v2",
            content_hash="sha256:" + suffix * 64,
            coverage_start=date(2021, 1, 4),
            coverage_end=date(2026, 8, 28),
            generated_at=NOW,
        )

    return (
        binding("security_master", "c"),
        binding("trading_calendar", "d", "sse_szse"),
        binding("market_data", "e"),
    )


def _store(path: Path) -> SQLAlchemyStrategyV2ArtifactStore:
    engine = create_backtest_run_engine(f"sqlite+pysqlite:///{path}")
    create_strategy_v2_artifact_schema(engine)
    return SQLAlchemyStrategyV2ArtifactStore(engine)


def _issue(
    database: Path,
) -> tuple[ValidationReceiptServiceV2, str, StrategyV2ValidationContext]:
    context = _context()
    candidate = StrategyCandidateV2(
        original_input=context.original_input,
        draft_id=context.draft_id,
        revision=context.revision,
        provider=context.provider,
        strategy=_strategy(),
    )
    plan = validate_strategy_candidate_v2(candidate, context)
    service = ValidationReceiptServiceV2(
        store=_store(database),
        signing_key=KEY,
        clock=lambda: NOW,
        receipt_ttl=timedelta(hours=1),
    )
    service.save_draft(
        DraftRevisionV2(
            draft_id=context.draft_id,
            revision=context.revision,
            original_input=context.original_input,
            provider=context.provider,
            created_at=NOW,
        )
    )
    receipt = service.persist_validated_plan(plan, snapshot_bindings=_bindings())
    return service, receipt.receipt_id, context


def test_receipt_restores_an_actual_validator_plan_after_process_restart(tmp_path: Path) -> None:
    database = tmp_path / "restart.db"
    first, receipt_id, context = _issue(database)
    first.store.engine.dispose()

    restarted = ValidationReceiptServiceV2(
        store=_store(database),
        signing_key=KEY,
        clock=lambda: NOW + timedelta(minutes=5),
        receipt_ttl=timedelta(hours=1),
    )
    restored = restarted.restore_executable_plan(
        receipt_id,
        current_context=context,
        current_snapshot_bindings=_bindings(),
    )

    assert prepare_v2_backtest_submission(restored).plan.plan_id == restored.plan_id


def test_raw_persisted_payload_cannot_bypass_the_submission_gate(tmp_path: Path) -> None:
    service, receipt_id, _ = _issue(tmp_path / "bypass.db")
    record = service.store.get_validation_receipt(receipt_id)
    assert record is not None
    with pytest.raises(V2SubmissionRejectedError):
        prepare_v2_backtest_submission(record)
    with pytest.raises(V2SubmissionRejectedError):
        prepare_v2_backtest_submission({"executable": True, "receipt": record.signed_token})


def test_tampered_expired_or_wrong_key_receipts_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "tamper.db"
    service, receipt_id, context = _issue(database)
    receipt = service.store.get_validation_receipt(receipt_id)
    assert receipt is not None

    parts = receipt.signed_token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    payload["strategy_hash"] = "sha256:" + "0" * 64
    forged_payload = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    forged = receipt.model_copy(update={"signed_token": f"{parts[0]}.{forged_payload}.{parts[2]}"})
    with service.store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE strategy_validation_receipts_v2 "
                "SET artifact_json = :payload WHERE receipt_id = :receipt_id"
            ),
            {"payload": canonical_json(forged), "receipt_id": receipt_id},
        )
    with pytest.raises(PersistentPlanRecoveryError, match=r"token hash|signature"):
        service.restore_executable_plan(
            receipt_id,
            current_context=context,
            current_snapshot_bindings=_bindings(),
        )

    # Re-issue into a clean database to isolate expiry and key rotation.
    clean_db = tmp_path / "expiry.db"
    _, clean_id, clean_context = _issue(clean_db)
    expired = ValidationReceiptServiceV2(
        store=_store(clean_db),
        signing_key=KEY,
        clock=lambda: NOW + timedelta(hours=2),
        receipt_ttl=timedelta(hours=1),
    )
    with pytest.raises(PersistentPlanRecoveryError, match="expired"):
        expired.restore_executable_plan(
            clean_id,
            current_context=clean_context,
            current_snapshot_bindings=_bindings(),
        )

    wrong_key = ValidationReceiptServiceV2(
        store=_store(clean_db),
        signing_key=b"another-server-key-at-least-thirty-two-bytes",
        clock=lambda: NOW,
        receipt_ttl=timedelta(hours=1),
    )
    with pytest.raises(PersistentPlanRecoveryError, match="signature"):
        wrong_key.restore_executable_plan(
            clean_id,
            current_context=clean_context,
            current_snapshot_bindings=_bindings(),
        )


def test_snapshot_drift_rejects_recovery(tmp_path: Path) -> None:
    service, receipt_id, context = _issue(tmp_path / "drift.db")
    security, calendar, market = _bindings()
    changed_market = market.model_copy(update={"content_hash": "sha256:" + "9" * 64})
    with pytest.raises(PersistentPlanRecoveryError, match="snapshot binding"):
        service.restore_executable_plan(
            receipt_id,
            current_context=context,
            current_snapshot_bindings=(security, calendar, changed_market),
        )


def test_original_input_is_reloaded_from_the_server_draft(tmp_path: Path) -> None:
    service, receipt_id, context = _issue(tmp_path / "original.db")
    restored = service.restore_executable_plan(
        receipt_id,
        current_context=context,
        current_snapshot_bindings=_bindings(),
    )
    assert restored.original_input == "MACD金叉买入，死叉卖出"

    forged_context = replace(context, original_input="客户伪造原话")
    with pytest.raises(PersistentPlanRecoveryError, match="draft revision"):
        service.restore_executable_plan(
            receipt_id,
            current_context=forged_context,
            current_snapshot_bindings=_bindings(),
        )
