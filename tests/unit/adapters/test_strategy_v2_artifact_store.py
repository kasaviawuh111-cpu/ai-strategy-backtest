from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from ashare_lab.adapters.persistence import create_backtest_run_engine
from ashare_lab.adapters.persistence.strategy_v2_artifacts import (
    ImmutableArtifactConflictError,
    MissingArtifactDependencyError,
    SQLAlchemyStrategyV2ArtifactStore,
    create_strategy_v2_artifact_schema,
)
from ashare_lab.domain.instruments import AssetType, Exchange, InstrumentRef
from ashare_lab.domain.runs.models_v2 import (
    DraftRevisionV2,
    ExecutableStrategyPlanRecordV2,
    RunManifestV2,
    SnapshotBindingV2,
    StoredRunResultV2,
    StoredValidationReceiptV2,
    ValidationReceiptClaimsV2,
)
from ashare_lab.domain.strategy.canonical import canonical_hash, canonical_json
from ashare_lab.domain.strategy.models_v2 import (
    BacktestConfigV2,
    CatalogRefV2,
    InterpretationCoverage,
    StrategySpecV2,
    TechnicalConditionV2,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
GIT_SHA = "1" * 40
BACKTEST_CONFIG = {
    "commission_rate": "0.0003",
    "minimum_commission_cny": "5.00",
    "slippage_bps": "5",
}


def _snapshot(kind: str, suffix: str) -> SnapshotBindingV2:
    return SnapshotBindingV2(
        kind=kind,
        snapshot_id=(
            f"composite:{suffix * 64}" if kind == "composite_snapshot" else f"{kind}:{suffix * 64}"
        ),
        provider="choice",
        schema_version=f"{kind}.v2",
        content_hash="sha256:" + suffix * 64,
        coverage_start=date(2021, 1, 4),
        coverage_end=date(2026, 8, 28),
        generated_at=NOW,
    )


def _composite() -> SnapshotBindingV2:
    return _snapshot("composite_snapshot", "f")


def _strategy() -> StrategySpecV2:
    technical = {
        "indicator_id": "technical.macd",
        "definition_version": "1.0.0",
        "params": {"fast": 12, "slow": 26, "signal": 9},
    }
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
        entry=TechnicalConditionV2(**technical, trigger="golden_cross"),
        exit=TechnicalConditionV2(**technical, trigger="death_cross"),
        interpretation_coverage=InterpretationCoverage(),
        backtest=BacktestConfigV2(
            start=date(2021, 1, 4),
            end=date(2026, 8, 28),
            initial_cash_cny=1_000_000,
        ),
    )


def _artifacts() -> tuple[
    DraftRevisionV2,
    ExecutableStrategyPlanRecordV2,
    StoredValidationReceiptV2,
    RunManifestV2,
]:
    draft = DraftRevisionV2(
        draft_id="draft:p0c-store",
        revision=1,
        original_input="MACD金叉买入，死叉卖出",
        provider="deepseek",
        created_at=NOW,
    )
    strategy = _strategy()
    plan = ExecutableStrategyPlanRecordV2(
        plan_id="sha256:" + "a" * 64,
        draft_id=draft.draft_id,
        revision=draft.revision,
        strategy_json=canonical_json(strategy),
        strategy_hash=canonical_hash(strategy),
        catalog_hash="sha256:" + "b" * 64,
        security_master=_snapshot("security_master", "c"),
        trading_calendar=_snapshot("trading_calendar", "d"),
        market_data=_snapshot("market_data", "e"),
        composite_snapshot=_composite(),
        provider=draft.provider,
        validator_version="strategy-v2-gate.2",
        code_revision=GIT_SHA,
        created_at=NOW,
    )
    claims = ValidationReceiptClaimsV2.from_plan(
        plan,
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    receipt = StoredValidationReceiptV2(
        receipt_id="receipt:" + "f" * 64,
        plan_id=plan.plan_id,
        claims_json=canonical_json(claims),
        signed_token="p0c-v2.payload.signature",
        token_sha256="sha256:" + "f" * 64,
        issued_at=claims.issued_at,
        expires_at=claims.expires_at,
    )
    manifest = RunManifestV2.from_server_records(
        run_id="run:p0c-store",
        draft=draft,
        plan=plan,
        receipt_id=receipt.receipt_id,
        receipt_sha256=receipt.token_sha256,
        engine_run_key="p0c-store-engine-key",
        backtest_config_json=canonical_json(BACKTEST_CONFIG),
        backtest_config_hash=canonical_hash(BACKTEST_CONFIG),
        fee_policy_version="cn.a_share.cash_equity_fees.2015_present.v1",
        fee_policy_hash="sha256:" + "8" * 64,
        engine_result_hash="sha256:" + "9" * 64,
        created_at=NOW,
    )
    return draft, plan, receipt, manifest


def _store(path: Path) -> SQLAlchemyStrategyV2ArtifactStore:
    engine = create_backtest_run_engine(f"sqlite+pysqlite:///{path}")
    create_strategy_v2_artifact_schema(engine)
    return SQLAlchemyStrategyV2ArtifactStore(engine)


def test_draft_receipt_plan_and_manifest_survive_process_restart(tmp_path: Path) -> None:
    database = tmp_path / "p0c.db"
    draft, plan, receipt, manifest = _artifacts()
    first = _store(database)
    first.append_draft_revision(draft)
    first.append_validated_plan(plan, receipt)
    first.append_run_manifest(manifest)
    result = StoredRunResultV2.from_payload(
        run_id=manifest.run_id,
        manifest_hash=manifest.manifest_hash,
        engine_result_hash=manifest.engine_result_hash,
        payload={"audit": {"engineResultHash": manifest.engine_result_hash}, "series": []},
        created_at=NOW,
    )
    first.append_run_result(result)
    first.engine.dispose()

    reopened = _store(database)
    assert reopened.get_draft_revision(draft.draft_id, draft.revision) == draft
    assert reopened.get_plan(plan.plan_id) == plan
    assert reopened.get_validation_receipt(receipt.receipt_id) == receipt
    assert reopened.get_run_manifest(manifest.run_id) == manifest
    assert reopened.get_run_result(manifest.run_id) == result


def test_append_only_records_reject_conflicting_rewrites(tmp_path: Path) -> None:
    store = _store(tmp_path / "immutable.db")
    draft, plan, receipt, manifest = _artifacts()
    store.append_draft_revision(draft)
    store.append_validated_plan(plan, receipt)
    store.append_run_manifest(manifest)

    changed_draft = draft.model_copy(update={"original_input": "偷换后的原话"})
    with pytest.raises(ImmutableArtifactConflictError):
        store.append_draft_revision(changed_draft)

    changed_manifest = manifest.model_copy(update={"provider": "forged"})
    with pytest.raises(ImmutableArtifactConflictError):
        store.append_run_manifest(changed_manifest)

    # Exact replays are idempotent, not updates.
    store.append_draft_revision(draft)
    store.append_validated_plan(plan, receipt)
    store.append_run_manifest(manifest)


def test_database_guards_cover_every_v2_artifact_table(tmp_path: Path) -> None:
    store = _store(tmp_path / "database-guards.db")
    draft, plan, receipt, manifest = _artifacts()
    store.append_draft_revision(draft)
    store.append_validated_plan(plan, receipt)
    store.append_run_manifest(manifest)
    store.append_run_result(
        StoredRunResultV2.from_payload(
            run_id=manifest.run_id,
            manifest_hash=manifest.manifest_hash,
            engine_result_hash=manifest.engine_result_hash,
            payload={"audit": {"engineResultHash": manifest.engine_result_hash}},
            created_at=NOW,
        )
    )
    tables = (
        "strategy_draft_revisions_v2",
        "strategy_executable_plans_v2",
        "strategy_validation_receipts_v2",
        "backtest_run_manifests_v2",
        "backtest_run_results_v2",
    )
    for table in tables:
        with (
            store.engine.begin() as connection,
            pytest.raises(
                DatabaseError,
                match="append-only",
            ),
        ):
            connection.execute(text(f"UPDATE {table} SET artifact_json=artifact_json || ' '"))
        with (
            store.engine.begin() as connection,
            pytest.raises(
                DatabaseError,
                match="append-only",
            ),
        ):
            connection.execute(text(f"DELETE FROM {table}"))


def test_database_payloads_do_not_contain_credentials(tmp_path: Path) -> None:
    database = tmp_path / "secrets.db"
    store = _store(database)
    draft, plan, receipt, manifest = _artifacts()
    store.append_draft_revision(draft)
    store.append_validated_plan(plan, receipt)
    store.append_run_manifest(manifest)
    store.engine.dispose()

    raw = database.read_bytes()
    assert b"Authorization" not in raw
    assert b"sk-" not in raw
    assert b"model_raw" not in raw


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    (
        ("plan_id", "sha256:" + "0" * 64),
        ("catalog_hash", "sha256:" + "0" * 64),
        ("security_master", _snapshot("security_master", "0")),
        ("trading_calendar", _snapshot("trading_calendar", "0")),
        ("market_data", _snapshot("market_data", "0")),
        ("composite_snapshot", _snapshot("composite_snapshot", "0")),
        ("code_revision", "0" * 40),
    ),
)
def test_manifest_must_match_every_server_owned_plan_binding(
    tmp_path: Path,
    field_name: str,
    forged_value: object,
) -> None:
    store = _store(tmp_path / f"forged-{field_name}.db")
    draft, plan, receipt, manifest = _artifacts()
    store.append_draft_revision(draft)
    store.append_validated_plan(plan, receipt)

    forged = manifest.model_copy(
        update={
            "run_id": f"run:forged-{field_name}",
            field_name: forged_value,
        }
    )
    with pytest.raises(MissingArtifactDependencyError, match="server-owned"):
        store.append_run_manifest(forged)


def test_model_copy_cannot_bypass_secret_validation(tmp_path: Path) -> None:
    store = _store(tmp_path / "copy-bypass.db")
    draft, _, _, _ = _artifacts()
    forged = draft.model_copy(update={"original_input": "MACD sk-1234567890abcdefghijklmnop"})
    with pytest.raises(ValueError, match="sensitive credential"):
        store.append_draft_revision(forged)


def test_run_result_must_match_server_manifest(tmp_path: Path) -> None:
    store = _store(tmp_path / "result-binding.db")
    draft, plan, receipt, manifest = _artifacts()
    store.append_draft_revision(draft)
    store.append_validated_plan(plan, receipt)
    store.append_run_manifest(manifest)
    result = StoredRunResultV2.from_payload(
        run_id=manifest.run_id,
        manifest_hash="sha256:" + "0" * 64,
        engine_result_hash=manifest.engine_result_hash,
        payload={"audit": {"engineResultHash": manifest.engine_result_hash}},
        created_at=NOW,
    )
    with pytest.raises(MissingArtifactDependencyError, match="server-owned"):
        store.append_run_result(result)


def test_atomic_completed_run_is_idempotent_and_rejects_conflicting_rewrite(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "atomic-idempotence.db")
    draft, plan, receipt, manifest = _artifacts()
    store.append_draft_revision(draft)
    store.append_validated_plan(plan, receipt)
    result = StoredRunResultV2.from_payload(
        run_id=manifest.run_id,
        manifest_hash=manifest.manifest_hash,
        engine_result_hash=manifest.engine_result_hash,
        payload={"audit": {"engineResultHash": manifest.engine_result_hash}, "series": []},
        created_at=NOW,
    )

    store.append_completed_run(manifest, result)
    store.append_completed_run(manifest, result)

    changed = StoredRunResultV2.from_payload(
        run_id=manifest.run_id,
        manifest_hash=manifest.manifest_hash,
        engine_result_hash=manifest.engine_result_hash,
        payload={"audit": {"engineResultHash": manifest.engine_result_hash}, "series": [1]},
        created_at=NOW,
    )
    with pytest.raises(ImmutableArtifactConflictError, match="run result"):
        store.append_completed_run(manifest, changed)
    assert store.get_run_manifest(manifest.run_id) == manifest
    assert store.get_run_result(manifest.run_id) == result


def test_atomic_completed_run_rolls_back_manifest_when_result_insert_fails(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "atomic-rollback.db")
    draft, plan, receipt, first_manifest = _artifacts()
    store.append_draft_revision(draft)
    store.append_validated_plan(plan, receipt)
    shared_payload = {
        "audit": {"engineResultHash": first_manifest.engine_result_hash},
        "series": [],
    }
    first_result = StoredRunResultV2.from_payload(
        run_id=first_manifest.run_id,
        manifest_hash=first_manifest.manifest_hash,
        engine_result_hash=first_manifest.engine_result_hash,
        payload=shared_payload,
        created_at=NOW,
    )
    store.append_completed_run(first_manifest, first_result)

    second_manifest = first_manifest.model_copy(update={"run_id": "run:p0d-atomic-rollback"})
    colliding_result = StoredRunResultV2.from_payload(
        run_id=second_manifest.run_id,
        manifest_hash=second_manifest.manifest_hash,
        engine_result_hash=second_manifest.engine_result_hash,
        payload=shared_payload,
        created_at=NOW,
    )
    assert colliding_result.result_hash == first_result.result_hash

    with pytest.raises(ImmutableArtifactConflictError, match="completed run"):
        store.append_completed_run(second_manifest, colliding_result)
    assert store.get_run_manifest(second_manifest.run_id) is None
    assert store.get_run_result(second_manifest.run_id) is None
