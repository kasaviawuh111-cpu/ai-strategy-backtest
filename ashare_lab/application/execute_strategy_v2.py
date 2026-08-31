"""Receipt-only Strategy v2 execution boundary.

The public operation accepts one opaque ``receipt_id``.  Every other input is
either recovered from append-only server storage, loaded from server-pinned
snapshots, or injected when the service is assembled.  Client/model supplied
plans, executable flags, source references, snapshot paths and signals have no
place in this API.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ashare_lab.application.daily_backtest import (
    DailyBacktestConfig,
    DailyBacktestResult,
    FeeQuoteProvider,
    SessionCalendar,
)
from ashare_lab.application.technical_v2 import (
    TechnicalV2BacktestInput,
    TechnicalV2ExecutionError,
    run_validated_technical_v2,
)
from ashare_lab.application.validation_receipts_v2 import (
    PersistentPlanRecoveryError,
    ValidationReceiptServiceV2,
)
from ashare_lab.domain.catalog import CatalogSnapshot
from ashare_lab.domain.runs import (
    DraftRevisionV2,
    RunManifestV2,
    SnapshotBindingV2,
    canonical_signal_records_json,
)
from ashare_lab.domain.shared import InstrumentId, require_aware
from ashare_lab.domain.strategy.canonical import canonical_hash, canonical_json
from ashare_lab.domain.strategy.models_v2 import (
    LeafConditionV2,
    StrategySpecV2,
    TechnicalConditionV2,
    iter_condition_leaf_paths,
)
from ashare_lab.domain.strategy.validation_v2 import (
    ConditionGroundingExpectation,
    StrategyV2ValidationContext,
    condition_semantics_hash,
)
from ashare_lab.ports.market_data import DateRange
from ashare_lab.ports.trusted_snapshots import (
    TrustedSecurityMasterSnapshot,
    TrustedSecurityMasterSnapshotLoader,
    TrustedSnapshotError,
    TrustedTechnicalSnapshot,
    TrustedTechnicalSnapshotLoader,
    TrustedV2SnapshotContracts,
    build_trusted_v2_snapshot_contracts,
)

_RECEIPT_ID = re.compile(r"receipt:[0-9a-f]{64}")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_GIT_SHA = re.compile(r"[0-9a-f]{40}")


class ExecuteStrategyV2Error(RuntimeError):
    """One receipt-only submission failed with a stable public error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ExecuteStrategyV2Result:
    run_id: str
    result: DailyBacktestResult
    manifest: RunManifestV2


class ExecuteStrategyV2Service:
    """Restore, revalidate, execute and persist one server-owned Strategy v2."""

    def __init__(
        self,
        *,
        receipt_service: ValidationReceiptServiceV2,
        security_master_loader: TrustedSecurityMasterSnapshotLoader,
        technical_snapshot_loader: TrustedTechnicalSnapshotLoader,
        security_master_snapshot_id: str,
        producer_snapshot_id: str,
        catalog: CatalogSnapshot,
        code_revision: str,
        calendar_factory: Callable[[TrustedTechnicalSnapshot], SessionCalendar],
        fee_provider: FeeQuoteProvider,
        backtest_config: DailyBacktestConfig,
        run_id_factory: Callable[[], str],
        clock: Callable[[], datetime],
    ) -> None:
        self._receipts = receipt_service
        self._master_loader = security_master_loader
        self._technical_loader = technical_snapshot_loader
        self._security_master_snapshot_id = security_master_snapshot_id
        self._producer_snapshot_id = producer_snapshot_id
        self._catalog = catalog
        self._code_revision = code_revision
        self._calendar_factory = calendar_factory
        self._fee_provider = fee_provider
        self._backtest_config = backtest_config
        self._run_id_factory = run_id_factory
        self._clock = clock

    def execute(self, receipt_id: str) -> ExecuteStrategyV2Result:
        """Execute exactly one stored receipt; no other request fields are accepted."""

        if type(receipt_id) is not str or _RECEIPT_ID.fullmatch(receipt_id) is None:
            raise ExecuteStrategyV2Error(
                "receipt_untrusted",
                "validation receipt id is missing or invalid",
            )
        store = self._receipts.store
        receipt = store.get_validation_receipt(receipt_id)
        if receipt is None:
            raise ExecuteStrategyV2Error(
                "receipt_untrusted",
                "validation receipt was not found in server storage",
            )
        record = store.get_plan(receipt.plan_id)
        if record is None:
            raise ExecuteStrategyV2Error(
                "receipt_untrusted",
                "validation receipt plan was not found in server storage",
            )
        draft = store.get_draft_revision(record.draft_id, record.revision)
        if draft is None:
            raise ExecuteStrategyV2Error(
                "receipt_untrusted",
                "validation receipt draft was not found in server storage",
            )
        _require_pure_technical(record.strategy)

        period = DateRange(record.strategy.backtest.start, record.strategy.backtest.end)
        instrument_id = InstrumentId(record.strategy.instrument.symbol)
        try:
            master = self._master_loader.load(self._security_master_snapshot_id)
            technical = self._technical_loader.load(
                producer_snapshot_id=self._producer_snapshot_id,
                instrument_id=instrument_id,
                period=period,
                security_master=master,
            )
            contracts = build_trusted_v2_snapshot_contracts(
                security_master=master,
                technical_snapshot=technical,
                instrument_id=instrument_id,
                period=period,
            )
        except TrustedSnapshotError as error:
            raise ExecuteStrategyV2Error(
                "data_unavailable",
                f"trusted snapshot could not be loaded: {error}",
            ) from error

        context = _current_validation_context(
            draft=draft,
            strategy=record.strategy,
            master=master,
            contracts=contracts,
            catalog=self._catalog,
            code_revision=self._code_revision,
        )
        try:
            plan = self._receipts.restore_executable_plan(
                receipt_id,
                current_context=context,
                current_snapshot_bindings=(
                    contracts.security_master,
                    contracts.trading_calendar,
                    contracts.market_data,
                ),
            )
        except PersistentPlanRecoveryError as error:
            raise ExecuteStrategyV2Error(
                "receipt_untrusted",
                f"validation receipt or trusted snapshot binding is invalid: {error}",
            ) from error

        run_id = self._run_id_factory()
        created_at = self._clock()
        require_aware(created_at, "created_at")
        engine_run_key = derive_technical_engine_run_key(
            strategy_hash=plan.strategy_hash,
            snapshot_bindings=(
                contracts.security_master,
                contracts.trading_calendar,
                contracts.market_data,
            ),
            code_revision=plan.code_revision,
            backtest_config=self._backtest_config,
            fee_provider=self._fee_provider,
        )
        backtest_config_payload = _backtest_config_payload(self._backtest_config)
        _, fee_policy_version, fee_policy_hash = _fee_provider_identity(self._fee_provider)
        try:
            completed = run_validated_technical_v2(
                TechnicalV2BacktestInput(
                    plan=plan,
                    market_snapshot=contracts.market_data,
                    run_key=engine_run_key,
                    bars=technical.execution_bars,
                    signal_bars=technical.signal_bars,
                    sessions=technical.sessions,
                    calendar=self._calendar_factory(technical),
                    fee_calculator=self._fee_provider,
                    corporate_actions=technical.corporate_actions,
                    config=self._backtest_config,
                )
            )
        except TechnicalV2ExecutionError as error:
            raise ExecuteStrategyV2Error(error.code, str(error)) from error

        manifest = RunManifestV2.from_server_records(
            run_id=run_id,
            draft=draft,
            plan=record,
            receipt_id=receipt.receipt_id,
            receipt_sha256=receipt.token_sha256,
            engine_run_key=engine_run_key,
            backtest_config_json=canonical_json(backtest_config_payload),
            backtest_config_hash=canonical_hash(backtest_config_payload),
            fee_policy_version=fee_policy_version,
            fee_policy_hash=fee_policy_hash,
            engine_result_hash=completed.result.content_hash,
            signal_records_json=canonical_signal_records_json(completed.signal_records),
            created_at=created_at,
        )
        store.append_run_manifest(manifest)
        return ExecuteStrategyV2Result(
            run_id=run_id,
            result=completed.result,
            manifest=manifest,
        )


def derive_technical_engine_run_key(
    *,
    strategy_hash: str,
    snapshot_bindings: tuple[SnapshotBindingV2, ...],
    code_revision: str,
    backtest_config: DailyBacktestConfig,
    fee_provider: FeeQuoteProvider,
) -> str:
    """Derive the v1 identifier namespace solely from immutable run evidence."""

    if type(strategy_hash) is not str or _SHA256.fullmatch(strategy_hash) is None:
        raise ValueError("strategy_hash must be a sha256 content hash")
    if type(code_revision) is not str or _GIT_SHA.fullmatch(code_revision) is None:
        raise ValueError("code_revision must be a complete 40-character Git SHA")
    if (
        type(snapshot_bindings) is not tuple
        or len(snapshot_bindings) != 3
        or any(type(item) is not SnapshotBindingV2 for item in snapshot_bindings)
        or {item.kind for item in snapshot_bindings}
        != {"security_master", "trading_calendar", "market_data"}
    ):
        raise ValueError("exactly three typed snapshot bindings are required")
    asset_type, policy_version, parameters_hash = _fee_provider_identity(fee_provider)

    payload = {
        "backtest_config": _backtest_config_payload(backtest_config),
        "code_revision": code_revision,
        "fee": {
            "asset_type": asset_type,
            "parameters_hash": parameters_hash,
            "policy_version": policy_version,
        },
        "strategy_hash": strategy_hash,
        "snapshots": [
            {
                "content_hash": item.content_hash,
                "kind": item.kind,
                "snapshot_id": item.snapshot_id,
            }
            for item in sorted(snapshot_bindings, key=lambda item: item.kind)
        ],
    }
    digest = canonical_hash(payload).removeprefix("sha256:")
    return "v2_" + digest[:37]


def _backtest_config_payload(config: DailyBacktestConfig) -> dict[str, object]:
    if type(config) is not DailyBacktestConfig:
        raise ValueError("backtest_config must be DailyBacktestConfig")
    return {
        "allocation_ratio": _decimal_text(config.allocation_ratio),
        "capacity_mode": config.capacity_mode.value,
        "edge_entry_validity_sessions": config.edge_entry_validity_sessions,
        "event_entry_validity_sessions": config.event_entry_validity_sessions,
        "limit_handling": config.limit_handling.value,
        "max_exit_attempts": config.max_exit_attempts,
        "participation_rate": _decimal_text(config.participation_rate),
        "retry_unfilled_exits": config.retry_unfilled_exits,
        "slippage_bps": _decimal_text(config.slippage_bps),
        "state_entry_validity_sessions": config.state_entry_validity_sessions,
    }


def _fee_provider_identity(provider: FeeQuoteProvider) -> tuple[str, str, str]:
    raw_policy_version = getattr(provider, "policy_version", None)
    policy_version = getattr(raw_policy_version, "value", raw_policy_version)
    raw_asset_type = getattr(provider, "supported_asset_type", None)
    asset_type = getattr(raw_asset_type, "value", raw_asset_type)
    parameters_hash = getattr(provider, "parameters_hash", None)
    if type(policy_version) is not str or not policy_version.strip():
        raise ValueError("fee provider must expose a versioned policy identity")
    if type(asset_type) is not str or asset_type not in {"STOCK", "ETF"}:
        raise ValueError("fee provider must expose its supported asset type")
    if type(parameters_hash) is not str or _SHA256.fullmatch(parameters_hash) is None:
        raise ValueError("fee provider must expose a sha256 parameters_hash")
    return asset_type, policy_version, parameters_hash


def _decimal_text(value: object) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("backtest config decimals must be finite Decimal values")
    return format(value.normalize(), "f")


def _require_pure_technical(strategy: StrategySpecV2) -> None:
    leaves = tuple(iter_condition_leaf_paths(strategy))
    if not leaves or any(not isinstance(item, TechnicalConditionV2) for _, item in leaves):
        raise ExecuteStrategyV2Error(
            "capability_unavailable",
            "only pure technical Strategy v2 plans are executable in P0-C",
        )


def _current_validation_context(
    *,
    draft: DraftRevisionV2,
    strategy: StrategySpecV2,
    master: TrustedSecurityMasterSnapshot,
    contracts: TrustedV2SnapshotContracts,
    catalog: CatalogSnapshot,
    code_revision: str,
) -> StrategyV2ValidationContext:
    leaf_by_path: dict[str, LeafConditionV2] = dict(iter_condition_leaf_paths(strategy))
    grounding_paths = tuple(
        grounding.dsl_path for grounding in strategy.interpretation_coverage.groundings
    )
    if len(set(grounding_paths)) != len(grounding_paths) or set(grounding_paths) != set(
        leaf_by_path
    ):
        raise ExecuteStrategyV2Error(
            "receipt_untrusted",
            "stored strategy groundings do not match its executable conditions",
        )
    grounding_expectations = tuple(
        ConditionGroundingExpectation(
            dsl_path=grounding.dsl_path,
            source_start=grounding.source_start,
            source_end=grounding.source_end,
            source_text=grounding.source_text,
            condition_hash=condition_semantics_hash(leaf_by_path[grounding.dsl_path]),
        )
        for grounding in strategy.interpretation_coverage.groundings
    )
    return StrategyV2ValidationContext(
        security_master=master.snapshot,
        original_input=draft.original_input,
        draft_id=draft.draft_id,
        revision=draft.revision,
        provider=draft.provider,
        requested_instrument=strategy.instrument.symbol,
        expected_backtest=strategy.backtest,
        catalog=catalog,
        dataset_coverage=(contracts.dataset_coverage,),
        grounding_expectations=grounding_expectations,
        code_revision=code_revision,
    )


__all__ = [
    "ExecuteStrategyV2Error",
    "ExecuteStrategyV2Result",
    "ExecuteStrategyV2Service",
    "derive_technical_engine_run_key",
]
