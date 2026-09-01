# Starlette's TestClient currently exposes partially unknown httpx method types to Pyright.
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from ashare_lab.adapters.persistence.strategy_v2_artifacts import (
    SQLAlchemyStrategyV2ArtifactStore,
)
from ashare_lab.api import create_app
from ashare_lab.application.result_views import (
    RESULT_HASH_SCHEMA_VERSION,
    calculate_result_bundle_hash,
)
from ashare_lab.application.strategy_v2_http import (
    PersistedStrategyV2HttpService,
    StrategyV2CompletedExecution,
    StrategyV2DraftCommand,
    StrategyV2DraftView,
    StrategyV2ExecutionView,
    StrategyV2HttpServiceError,
    StrategyV2RunView,
    StrategyV2ValidationView,
)
from ashare_lab.domain.instruments import AssetType, Exchange, InstrumentRef
from ashare_lab.domain.runs import (
    DraftRevisionV2,
    ExecutableStrategyPlanRecordV2,
    RunManifestV2,
    SnapshotBindingV2,
    StoredRunResultV2,
    StoredValidationReceiptV2,
    ValidationReceiptClaimsV2,
)
from ashare_lab.domain.strategy import (
    BacktestConfigV2,
    CatalogRefV2,
    ConditionGrounding,
    InterpretationCoverage,
    StrategySpecV2,
    TechnicalConditionV2,
    canonical_hash_v2,
)
from ashare_lab.domain.strategy.canonical import canonical_hash, canonical_json

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
START = date(2021, 8, 31)
END = date(2026, 8, 31)
DRAFT_ID = "draft:http-contract"
RECEIPT_ID = "receipt:" + "a" * 64
PLAN_ID = "sha256:" + "b" * 64
RUN_ID = "run:v2-http-contract"
GIT_SHA = "c" * 40
ORIGINAL_INPUT = "MACD金叉买入，死叉卖出"


def _strategy() -> StrategySpecV2:
    entry = TechnicalConditionV2(
        indicator_id="technical.macd",
        definition_version="1.0.0",
        params={"fast": 12, "slow": 26, "signal": 9},
        trigger="golden_cross",
    )
    exit_condition = entry.model_copy(update={"trigger": "death_cross"})
    return StrategySpecV2(
        catalog=CatalogRefV2(catalog_id="cn_a.signals", release_version="2026.09.01"),
        instrument=InstrumentRef(
            symbol="300059.SZ",
            name="东方财富",
            exchange=Exchange.SZ,
            asset_type=AssetType.STOCK,
            listing_date=date(2010, 3, 19),
            tradable=True,
            data_source="choice.security_master",
        ),
        entry=entry,
        exit=exit_condition,
        interpretation_coverage=InterpretationCoverage(
            groundings=(
                ConditionGrounding(
                    dsl_path="$.entry",
                    source_start=0,
                    source_end=8,
                    source_text=ORIGINAL_INPUT[:8],
                ),
                ConditionGrounding(
                    dsl_path="$.exit",
                    source_start=9,
                    source_end=len(ORIGINAL_INPUT),
                    source_text=ORIGINAL_INPUT[9:],
                ),
            )
        ),
        backtest=BacktestConfigV2(start=START, end=END, initial_cash_cny=1_000_000),
    )


def _binding(kind: str, suffix: str) -> SnapshotBindingV2:
    snapshot_prefix = "composite" if kind == "composite_snapshot" else kind
    return SnapshotBindingV2(
        kind=kind,  # type: ignore[arg-type]
        snapshot_id=f"{snapshot_prefix}:{suffix * 64}",
        provider="test-provider",
        schema_version="test.v1",
        content_hash="sha256:" + suffix * 64,
        coverage_start=START,
        coverage_end=END,
        generated_at=NOW,
    )


def _manifest() -> RunManifestV2:
    strategy = _strategy()
    backtest_config = {"allocation_ratio": "1"}
    return RunManifestV2(
        run_id=RUN_ID,
        plan_id=PLAN_ID,
        draft_id=DRAFT_ID,
        revision=1,
        original_input=ORIGINAL_INPUT,
        final_strategy_json=canonical_json(strategy),
        strategy_hash=canonical_hash_v2(strategy),
        provider="rule_based",
        validation_receipt_id=RECEIPT_ID,
        validation_receipt_sha256="sha256:" + "d" * 64,
        catalog_hash="sha256:" + "e" * 64,
        security_master=_binding("security_master", "1"),
        trading_calendar=_binding("trading_calendar", "2"),
        market_data=_binding("market_data", "3"),
        composite_snapshot=_binding("composite_snapshot", "4"),
        code_revision=GIT_SHA,
        engine_run_key="v2_http_contract",
        backtest_config_json=canonical_json(backtest_config),
        backtest_config_hash=canonical_hash(backtest_config),
        fee_policy_version="test.fees.v1",
        fee_policy_hash="sha256:" + "5" * 64,
        engine_result_hash="sha256:" + "6" * 64,
        created_at=NOW,
    )


def _result_payload() -> dict[str, Any]:
    manifest = _manifest()
    producer = manifest.composite_snapshot
    assert producer is not None
    payload: dict[str, Any] = {
        "summary": {
            "runId": RUN_ID,
            "totalReturn": 0.0,
            "benchmarkReturn": None,
            "benchmarkComparisonStatus": "benchmark_unavailable",
            "annualizedReturn": 0.0,
            "maxDrawdown": 0.0,
            "sharpeRatio": None,
            "winRate": None,
            "tradeCount": 0,
            "initialCashCny": 1_000_000.0,
            "finalEquityCny": 1_000_000.0,
            "interpretation": "No trades were produced by this contract fixture.",
            "dataRange": {
                "start": START.isoformat(),
                "end": END.isoformat(),
                "sessions": 1,
            },
            "warnings": [],
            "runEvidence": {
                "strategyHash": manifest.strategy_hash,
                "catalogHash": manifest.catalog_hash,
                "dataSnapshotId": manifest.market_data.snapshot_id,
                "dataSnapshotChecksum": manifest.market_data.content_hash,
                "dataSchemaVersion": manifest.market_data.schema_version,
                "producerSnapshotSchemaVersion": producer.schema_version,
                "producerSnapshotId": producer.snapshot_id,
                "codeRevision": manifest.code_revision,
                "engineVersion": "ashare-lab.strategy-v2-daily.v1",
                "executionAssumptions": {"allocation_ratio": "1"},
            },
        },
        "series": [
            {
                "date": START.isoformat(),
                "equity": 100.0,
                "benchmark": None,
                "drawdown": 0.0,
            },
            {
                "date": END.isoformat(),
                "equity": 100.0,
                "benchmark": None,
                "drawdown": 0.0,
            },
        ],
        "activities": [],
        "audit": {
            "engineResultHash": manifest.engine_result_hash,
            "hashSchemaVersion": RESULT_HASH_SCHEMA_VERSION,
            "openPositionShares": 0,
        },
        "robustness": None,
    }
    payload["audit"]["resultHash"] = calculate_result_bundle_hash(payload)
    return payload


def _stored_result_hash() -> str:
    return canonical_hash(_result_payload())


def _draft_record() -> DraftRevisionV2:
    return DraftRevisionV2(
        draft_id=DRAFT_ID,
        revision=1,
        original_input=ORIGINAL_INPUT,
        provider="rule_based",
        created_at=NOW,
    )


def _plan_record() -> ExecutableStrategyPlanRecordV2:
    strategy = _strategy()
    return ExecutableStrategyPlanRecordV2(
        plan_id=PLAN_ID,
        draft_id=DRAFT_ID,
        revision=1,
        strategy_json=canonical_json(strategy),
        strategy_hash=canonical_hash_v2(strategy),
        catalog_hash="sha256:" + "e" * 64,
        security_master=_binding("security_master", "1"),
        trading_calendar=_binding("trading_calendar", "2"),
        market_data=_binding("market_data", "3"),
        composite_snapshot=_binding("composite_snapshot", "4"),
        provider="rule_based",
        validator_version="strategy-v2-persistent-gate.1",
        code_revision=GIT_SHA,
        created_at=NOW,
    )


def _receipt_record() -> StoredValidationReceiptV2:
    plan = _plan_record()
    claims = ValidationReceiptClaimsV2.from_plan(
        plan,
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    return StoredValidationReceiptV2(
        receipt_id=RECEIPT_ID,
        plan_id=plan.plan_id,
        claims_json=canonical_json(claims),
        signed_token="p0c-v2.contract-fixture.signature",
        token_sha256="sha256:" + "d" * 64,
        issued_at=claims.issued_at,
        expires_at=claims.expires_at,
    )


def _stored_result_record() -> StoredRunResultV2:
    manifest = _manifest()
    return StoredRunResultV2.from_payload(
        run_id=RUN_ID,
        manifest_hash=manifest.manifest_hash,
        engine_result_hash=manifest.engine_result_hash,
        payload=_result_payload(),
        created_at=NOW,
    )


@dataclass
class _FakeStrategyV2Service:
    draft_commands: list[StrategyV2DraftCommand]
    validation_calls: list[tuple[str, int]]
    execution_calls: list[tuple[str, int]]
    run_queries: list[str]

    async def create_draft(self, command: StrategyV2DraftCommand) -> StrategyV2DraftView:
        self.draft_commands.append(command)
        strategy = _strategy()
        return StrategyV2DraftView(
            draft_id=DRAFT_ID,
            revision=1,
            status="ready",
            strategy=strategy,
            strategy_hash=canonical_hash_v2(strategy),
            provider="rule_based",
            created_at=NOW,
        )

    def get_draft(self, draft_id: str, revision: int) -> StrategyV2DraftView:
        if (draft_id, revision) != (DRAFT_ID, 1):
            raise StrategyV2HttpServiceError("strategy_draft_not_found", "missing draft")
        strategy = _strategy()
        return StrategyV2DraftView(
            draft_id=DRAFT_ID,
            revision=1,
            status="ready",
            strategy=strategy,
            strategy_hash=canonical_hash_v2(strategy),
            provider="rule_based",
            created_at=NOW,
        )

    def validate(self, draft_id: str, revision: int) -> StrategyV2ValidationView:
        self.validation_calls.append((draft_id, revision))
        return StrategyV2ValidationView(
            draft_id=draft_id,
            revision=revision,
            status="validated",
            plan_id=PLAN_ID,
            receipt_id=RECEIPT_ID,
            strategy_hash=canonical_hash_v2(_strategy()),
            expires_at=NOW + timedelta(hours=1),
        )

    def execute(self, draft_id: str, revision: int) -> StrategyV2ExecutionView:
        self.execution_calls.append((draft_id, revision))
        return StrategyV2ExecutionView(
            run_id=RUN_ID,
            state="succeeded",
            result_hash=_stored_result_hash(),
            manifest_hash=_manifest().manifest_hash,
        )

    def get_run(self, run_id: str) -> StrategyV2RunView:
        self.run_queries.append(run_id)
        if run_id != RUN_ID:
            raise StrategyV2HttpServiceError("backtest_run_not_found", "missing run")
        return StrategyV2RunView(
            run_id=run_id,
            state="succeeded",
            result_hash=_stored_result_hash(),
            manifest_hash=_manifest().manifest_hash,
            manifest=_manifest(),
            result=_result_payload(),
        )


@dataclass
class _FakeDraftLifecycle:
    validation_calls: list[tuple[str, int]]

    async def create_draft(self, command: StrategyV2DraftCommand) -> StrategyV2DraftView:
        del command
        raise AssertionError("create is not used by this persistence contract")

    def get_draft(self, draft_id: str, revision: int) -> StrategyV2DraftView:
        del draft_id, revision
        raise AssertionError("get is not used by this persistence contract")

    def validate(self, draft_id: str, revision: int) -> None:
        self.validation_calls.append((draft_id, revision))


@dataclass(frozen=True)
class _CompletedExecution:
    run_id: str
    manifest: RunManifestV2


@dataclass
class _FakeReceiptExecutor:
    receipt_ids: list[str]

    def execute(self, receipt_id: str) -> StrategyV2CompletedExecution:
        self.receipt_ids.append(receipt_id)
        return _CompletedExecution(run_id=RUN_ID, manifest=_manifest())


def _client() -> tuple[TestClient, _FakeStrategyV2Service]:
    service = _FakeStrategyV2Service([], [], [], [])
    return TestClient(create_app(strategy_v2_service=service)), service


def test_v2_draft_validation_execution_and_result_query_accept_only_server_ids() -> None:
    client, service = _client()
    with client:
        draft = client.post(
            "/api/v2/strategy-drafts",
            json={
                "utterance": ORIGINAL_INPUT,
                "instrument_context": "300059.SZ",
                "as_of_date": END.isoformat(),
            },
        )
        validation = client.post(
            "/api/v2/strategy-validations",
            json={"draft_id": DRAFT_ID, "revision": 1},
        )
        execution = client.post(
            "/api/v2/backtest-runs",
            json={"draft_id": DRAFT_ID, "revision": 1},
        )
        fetched = client.get(f"/api/v2/backtest-runs/{RUN_ID}")

    assert draft.status_code == 201
    assert draft.json()["strategy"]["schema_version"] == "strategy.v2"
    assert validation.status_code == 200
    assert validation.json()["receipt_id"] == RECEIPT_ID
    assert execution.status_code == 201
    assert execution.json()["run_id"] == RUN_ID
    assert fetched.status_code == 200
    assert fetched.json()["manifest"]["original_input"] == ORIGINAL_INPUT
    assert fetched.json()["manifest_hash"] == _manifest().manifest_hash
    assert service.draft_commands == [
        StrategyV2DraftCommand(
            utterance=ORIGINAL_INPUT,
            instrument_context="300059.SZ",
            as_of_date=END,
        )
    ]
    assert service.validation_calls == [(DRAFT_ID, 1)]
    assert service.execution_calls == [(DRAFT_ID, 1)]
    assert service.run_queries == [RUN_ID]


def test_v2_validation_and_execution_reject_all_client_authority_fields() -> None:
    forbidden: dict[str, Any] = {
        "executable": True,
        "receipt": "attacker-controlled",
        "source_refs": [],
        "snapshot_id": "composite:" + "f" * 64,
        "strategy": _strategy().model_dump(mode="json"),
    }
    client, service = _client()
    with client:
        for path in ("/api/v2/strategy-validations", "/api/v2/backtest-runs"):
            for name, value in forbidden.items():
                response = client.post(
                    path,
                    json={"draft_id": DRAFT_ID, "revision": 1, name: value},
                )
                assert response.status_code == 422
                assert response.json()["error"]["code"] == "request_validation_failed"

    assert service.validation_calls == []
    assert service.execution_calls == []


def test_v2_draft_accepts_user_text_only_and_rejects_client_dsl_or_receipt() -> None:
    client, service = _client()
    base = {
        "utterance": ORIGINAL_INPUT,
        "instrument_context": "300059.SZ",
        "as_of_date": END.isoformat(),
    }
    with client:
        for name, value in {
            "strategy": _strategy().model_dump(mode="json"),
            "receipt": RECEIPT_ID,
            "executable": True,
            "snapshot_id": "composite:" + "f" * 64,
        }.items():
            response = client.post(
                "/api/v2/strategy-drafts",
                json={**base, name: value},
            )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "request_validation_failed"

    assert service.draft_commands == []


def test_v2_routes_fail_closed_when_runtime_is_not_wired_and_v1_remains_available() -> None:
    with TestClient(create_app()) as client:
        unavailable = client.post(
            "/api/v2/backtest-runs",
            json={"draft_id": DRAFT_ID, "revision": 1},
        )
        legacy = client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": ORIGINAL_INPUT,
                "instrument_context": "300059.SZ",
                "as_of_date": END.isoformat(),
            },
        )

    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "strategy_v2_service_unavailable"
    assert legacy.status_code == 201
    assert legacy.json()["strategy"]["schema_version"] == "strategy.v1"


def test_v2_service_errors_preserve_capability_and_not_found_semantics() -> None:
    @dataclass
    class _RejectingService(_FakeStrategyV2Service):
        def validate(self, draft_id: str, revision: int) -> StrategyV2ValidationView:
            del draft_id, revision
            raise StrategyV2HttpServiceError(
                "capability_unavailable",
                "index and financial strategies are not executable",
            )

    service = _RejectingService([], [], [], [])
    with TestClient(create_app(strategy_v2_service=service)) as client:
        capability = client.post(
            "/api/v2/strategy-validations",
            json={"draft_id": DRAFT_ID, "revision": 1},
        )
        missing = client.get("/api/v2/backtest-runs/run:missing")

    assert capability.status_code == 422
    assert capability.json()["error"]["code"] == "capability_unavailable"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "backtest_run_not_found"


def test_v2_provider_unavailable_is_a_service_failure() -> None:
    @dataclass
    class _ProviderUnavailableService(_FakeStrategyV2Service):
        def validate(self, draft_id: str, revision: int) -> StrategyV2ValidationView:
            del draft_id, revision
            raise StrategyV2HttpServiceError(
                "provider_unavailable",
                "server-owned security discovery is offline",
            )

    service = _ProviderUnavailableService([], [], [], [])
    with TestClient(create_app(strategy_v2_service=service)) as client:
        response = client.post(
            "/api/v2/strategy-validations",
            json={"draft_id": DRAFT_ID, "revision": 1},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"


def test_persisted_http_service_selects_receipt_and_result_from_server_storage() -> None:
    store = SQLAlchemyStrategyV2ArtifactStore(
        create_engine("sqlite+pysqlite:///:memory:"),
        initialize_schema=True,
    )
    store.append_draft_revision(_draft_record())
    store.append_validated_plan(_plan_record(), _receipt_record())
    store.append_run_manifest(_manifest())
    store.append_run_result(_stored_result_record())
    lifecycle = _FakeDraftLifecycle([])
    executor = _FakeReceiptExecutor([])
    service = PersistedStrategyV2HttpService(
        drafts=lifecycle,
        artifacts=store,
        executor=executor,
    )

    validation = service.validate(DRAFT_ID, 1)
    execution = service.execute(DRAFT_ID, 1)
    fetched = service.get_run(RUN_ID)

    assert lifecycle.validation_calls == [(DRAFT_ID, 1)]
    assert validation.receipt_id == RECEIPT_ID
    assert executor.receipt_ids == [RECEIPT_ID]
    assert execution.result_hash == _stored_result_hash()
    assert fetched.result_hash == _stored_result_hash()
    assert fetched.manifest_hash == _manifest().manifest_hash
