"""Fail-closed bridge from a validated technical Strategy v2 to the v1 engine.

The bridge deliberately keeps order matching, fees and portfolio accounting in
the existing v1 daily engine.  Its responsibilities are limited to:

* rejecting raw/model-produced candidates;
* adapting an already validator-issued, technical-only plan;
* deriving deterministic signal provenance from server-loaded daily bars and a
  trusted snapshot binding; and
* proving the signal trace covers every triggered fact that the v1 engine used.

No caller may supply ``SourceRef`` or a signal decision to this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import cast
from zoneinfo import ZoneInfo

from ashare_lab.application.daily_backtest import (
    DailyBacktestConfig,
    DailyBacktestInput,
    DailyBacktestResult,
    FeeQuoteProvider,
    SessionCalendar,
    run_daily_backtest,
)
from ashare_lab.domain.execution import FeeCalculator
from ashare_lab.domain.instruments import AssetType, Exchange
from ashare_lab.domain.market_data import CorporateAction, DailyBar, InstrumentSession
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.portfolio import FeeBreakdown
from ashare_lab.domain.provenance import DataEnvelope, SignalRecord, SourceKind, SourceRef
from ashare_lab.domain.runs import SnapshotBindingV2
from ashare_lab.domain.shared import (
    CurrencyMismatchError,
    DomainValidationError,
    Money,
    Price,
    Quantity,
)
from ashare_lab.domain.signals import SignalFact, SignalRuntime
from ashare_lab.domain.strategy import StrategySpec
from ashare_lab.domain.strategy.canonical import canonical_hash
from ashare_lab.domain.strategy.models import Condition
from ashare_lab.domain.strategy.models_v2 import FrozenV2Model
from ashare_lab.domain.strategy.validation_v2 import (
    ExecutableStrategyPlan,
    adapt_technical_strategy_v2_to_v1,
    is_validator_issued_plan,
)
from ashare_lab.domain.time import PointInTimeAvailability

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_FEE_PROVIDER_AUTHORITY = object()
_STOCK_FEE_POLICY = "cn.a_share.cash_equity_fees.2015_present.v1"
_ETF_FEE_POLICY = "cn.a_share.secondary_market_stock_etf_all_in_fees.v1"
_CENT = Decimal("0.01")
_BAR_TRANSFORM_PROVIDER = "ashare_lab.technical_v2"
_BAR_TRANSFORM_SCHEMA = "canonical-daily-bar-prefix.v1"


class TechnicalV2ExecutionError(RuntimeError):
    """One safe execution boundary failed with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AttestedFeeQuoteProvider:
    """Server-created asset scope around an existing versioned fee provider."""

    supported_asset_type: AssetType
    supported_exchange: Exchange
    policy_version: str
    parameters_hash: str
    _delegate: FeeQuoteProvider = field(repr=False, compare=False)
    _authority: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority is not _FEE_PROVIDER_AUTHORITY:
            raise ValueError("fee provider attestation is server-only")
        if type(self.supported_asset_type) is not AssetType:
            raise ValueError("supported_asset_type must be AssetType")
        if type(self.supported_exchange) is not Exchange:
            raise ValueError("supported_exchange must be Exchange")
        if type(self.policy_version) is not str or not self.policy_version.strip():
            raise ValueError("policy_version must be a non-empty string")
        if type(self.parameters_hash) is not str or _SHA256.fullmatch(self.parameters_hash) is None:
            raise ValueError("parameters_hash must be a sha256 content hash")

    def calculate(
        self,
        *,
        side: OrderSide,
        price: Price,
        quantity: Quantity,
        trade_date: date,
    ) -> FeeBreakdown:
        return self._delegate.calculate(
            side=side,
            price=price,
            quantity=quantity,
            trade_date=trade_date,
        )


def bind_stock_fee_provider(
    provider: FeeCalculator,
) -> AttestedFeeQuoteProvider:
    """Attest the existing cash-equity policy without altering v1 fee behavior."""

    if type(provider) is not FeeCalculator:
        raise ValueError("stock fee provider must be the versioned FeeCalculator")
    policy = provider.policy
    raw_version = policy.version
    version = getattr(raw_version, "value", raw_version)
    if version != _STOCK_FEE_POLICY:
        raise ValueError("stock fee provider must use the existing versioned cash-equity policy")
    return AttestedFeeQuoteProvider(
        supported_asset_type=AssetType.STOCK,
        supported_exchange=Exchange(policy.exchange.value),
        policy_version=_STOCK_FEE_POLICY,
        parameters_hash=canonical_hash(
            {
                "asset_type": AssetType.STOCK.value,
                "commission_rate": _decimal_identity(policy.commission_rate),
                "exchange": policy.exchange.value,
                "minimum_commission": _decimal_identity(policy.minimum_commission.amount),
                "minimum_commission_currency": policy.minimum_commission.currency,
                "policy_version": version,
            }
        ),
        _delegate=provider,
        _authority=_FEE_PROVIDER_AUTHORITY,
    )


@dataclass(frozen=True, slots=True)
class AshareEtfAllInFeeProvider:
    """All-in broker commission for secondary-market A-share stock ETFs.

    The policy deliberately does not inherit the cash-equity calculator:
    bilateral broker commission is the complete modeled fee, while stamp tax,
    transfer fee and ``other`` stay zero.  Exchange handling charges are not
    added again because this is the explicitly selected all-in broker mode.
    """

    supported_exchange: Exchange
    commission_rate: Decimal
    minimum_commission: Money
    _authority: object = field(repr=False, compare=False)
    supported_asset_type: AssetType = field(init=False, default=AssetType.ETF)
    policy_version: str = field(init=False, default=_ETF_FEE_POLICY)

    def __post_init__(self) -> None:
        if self._authority is not _FEE_PROVIDER_AUTHORITY:
            raise ValueError("ETF fee provider construction is server-only")
        if type(self.supported_exchange) is not Exchange:
            raise ValueError("supported_exchange must be Exchange")
        if type(self.commission_rate) is not Decimal:
            raise ValueError("commission_rate must be Decimal")
        if not self.commission_rate.is_finite() or self.commission_rate < 0:
            raise ValueError("commission_rate must be finite and non-negative")
        if type(self.minimum_commission) is not Money:
            raise ValueError("minimum_commission must be Money")
        if self.minimum_commission.amount < 0:
            raise ValueError("minimum_commission cannot be negative")
        if self.minimum_commission.currency != "CNY":
            raise ValueError("A-share ETF minimum_commission must use CNY")

    @property
    def parameters_hash(self) -> str:
        """Content identity of the exact all-in broker fee parameters."""

        return canonical_hash(
            {
                "asset_type": self.supported_asset_type.value,
                "commission_rate": _decimal_identity(self.commission_rate),
                "exchange": self.supported_exchange.value,
                "minimum_commission": _decimal_identity(self.minimum_commission.amount),
                "minimum_commission_currency": self.minimum_commission.currency,
                "other_rate": "0",
                "policy_version": self.policy_version,
                "stamp_tax_rate": "0",
                "transfer_fee_rate": "0",
            }
        )

    def calculate(
        self,
        *,
        side: OrderSide,
        price: Price,
        quantity: Quantity,
        trade_date: date,
    ) -> FeeBreakdown:
        if type(side) is not OrderSide:
            raise DomainValidationError("side must be OrderSide")
        if type(price) is not Price:
            raise DomainValidationError("price must be Price")
        if type(quantity) is not Quantity or quantity.value <= 0:
            raise DomainValidationError("quantity must be a positive Quantity")
        if type(trade_date) is not date:
            raise DomainValidationError("trade_date must be a date")
        if price.currency != self.minimum_commission.currency:
            raise CurrencyMismatchError("price and minimum_commission must use one currency")
        gross_amount = price.amount * Decimal(quantity.value)
        commission = Money(
            max(
                gross_amount * self.commission_rate,
                self.minimum_commission.amount,
            ).quantize(_CENT, rounding=ROUND_HALF_UP),
            price.currency,
        )
        zero = Money.zero(price.currency)
        return FeeBreakdown(
            commission=commission,
            stamp_tax=zero,
            transfer_fee=zero,
            other=zero,
        )


def create_etf_all_in_fee_provider(
    *,
    exchange: Exchange,
    commission_rate: Decimal,
    minimum_commission: Money,
) -> AshareEtfAllInFeeProvider:
    """Create the ETF-only policy inside the server composition boundary."""

    return AshareEtfAllInFeeProvider(
        supported_exchange=exchange,
        commission_rate=commission_rate,
        minimum_commission=minimum_commission,
        _authority=_FEE_PROVIDER_AUTHORITY,
    )


@dataclass(frozen=True, slots=True)
class TechnicalSignalArtifact:
    """A v1 runtime fact paired with its immutable v2 provenance record."""

    condition_id: str
    fact: SignalFact
    record: SignalRecord

    def __post_init__(self) -> None:
        if self.record.condition_id != self.condition_id:
            raise ValueError("signal artifact condition identity is inconsistent")


@dataclass(frozen=True, slots=True)
class TechnicalV2BacktestInput:
    """Existing v1 engine inputs plus validator and snapshot authority."""

    plan: ExecutableStrategyPlan
    market_snapshot: SnapshotBindingV2
    run_key: str
    bars: tuple[DailyBar, ...]
    sessions: tuple[InstrumentSession, ...]
    calendar: SessionCalendar
    fee_calculator: FeeQuoteProvider
    signal_bars: tuple[DailyBar, ...] | None = None
    corporate_actions: tuple[CorporateAction, ...] = ()
    config: DailyBacktestConfig = field(default_factory=DailyBacktestConfig)
    benchmark_equity: tuple[tuple[date, Decimal], ...] = ()
    benchmark_initial_equity: Decimal | None = None
    benchmark_entry_filled: bool | None = None


@dataclass(frozen=True, slots=True)
class TechnicalV2BacktestResult:
    v1_strategy: StrategySpec
    result: DailyBacktestResult
    signal_records: tuple[SignalRecord, ...]


def adapt_validated_technical_plan(value: object) -> StrategySpec:
    """Convert only a validator-issued technical plan into Strategy v1."""

    if not is_validator_issued_plan(value):
        raise TechnicalV2ExecutionError(
            "plan_untrusted",
            "technical v2 execution requires a validator-issued plan",
        )
    plan = value
    instrument = plan.strategy.instrument
    if instrument.asset_type not in {AssetType.STOCK, AssetType.ETF}:
        raise TechnicalV2ExecutionError(
            "capability_unavailable",
            "only confirmed A-share STOCK and ETF instruments are executable",
        )
    try:
        return adapt_technical_strategy_v2_to_v1(plan.strategy)
    except TypeError as error:
        raise TechnicalV2ExecutionError(
            "capability_unavailable",
            "financial and event conditions are not executable in P0-C",
        ) from error


def build_technical_signal_records(
    plan: object,
    market_snapshot: SnapshotBindingV2,
    bars: tuple[DailyBar, ...],
) -> tuple[TechnicalSignalArtifact, ...]:
    """Evaluate technical conditions and derive all provenance server-side."""

    strategy = adapt_validated_technical_plan(plan)
    validated = _validated_plan(plan)
    canonical_bars = _validate_snapshot_inputs(validated, market_snapshot, bars)
    roots = (
        ("$.entry", validated.strategy.entry, strategy.entry),
        ("$.exit", validated.strategy.exit, strategy.exit.children[0]),
    )
    artifacts: list[TechnicalSignalArtifact] = []
    runtime = SignalRuntime()
    for condition_ref, v2_condition, v1_condition in roots:
        condition_id = derive_condition_id(
            validated.strategy_hash,
            condition_ref,
            v2_condition,
        )
        timeline = runtime.evaluate_aligned(cast(Condition, v1_condition), canonical_bars)
        for index, fact in enumerate(timeline):
            if fact is None or not fact.triggered:
                continue
            dependencies = tuple(
                bar for bar in canonical_bars[: index + 1] if bar.available_at <= fact.available_at
            )
            if not dependencies or canonical_bars[index] not in dependencies:
                raise TechnicalV2ExecutionError(
                    "lookahead_detected",
                    "signal input is not available at signal_at",
                )
            record = _signal_record(
                plan=validated,
                snapshot=market_snapshot,
                condition_id=condition_id,
                condition_ref=condition_ref,
                fact=fact,
                bars=dependencies,
            )
            artifacts.append(
                TechnicalSignalArtifact(
                    condition_id=condition_id,
                    fact=fact,
                    record=record,
                )
            )
    return tuple(
        sorted(
            artifacts,
            key=lambda item: (
                item.record.signal_at,
                item.record.condition_ref,
                item.record.fingerprint,
            ),
        )
    )


def generate_technical_signal_records(
    plan: object,
    bars: tuple[DailyBar, ...],
    market_snapshot: SnapshotBindingV2,
) -> tuple[SignalRecord, ...]:
    """Return triggered records in deterministic time/path/fingerprint order."""

    return tuple(
        artifact.record for artifact in build_technical_signal_records(plan, market_snapshot, bars)
    )


def run_validated_technical_v2(request: TechnicalV2BacktestInput) -> TechnicalV2BacktestResult:
    """Run the existing v1 engine after provenance has been derived and checked."""

    strategy = adapt_validated_technical_plan(request.plan)
    _validate_fee_provider(request.plan, request.fee_calculator)
    trace_bars = request.signal_bars or request.bars
    artifacts = build_technical_signal_records(
        request.plan,
        request.market_snapshot,
        trace_bars,
    )
    result = run_daily_backtest(
        DailyBacktestInput(
            run_key=request.run_key,
            strategy=strategy,
            bars=request.bars,
            signal_bars=request.signal_bars,
            sessions=request.sessions,
            calendar=request.calendar,
            fee_calculator=request.fee_calculator,
            corporate_actions=request.corporate_actions,
            config=request.config,
            benchmark_equity=request.benchmark_equity,
            benchmark_initial_equity=request.benchmark_initial_equity,
            benchmark_entry_filled=request.benchmark_entry_filled,
        )
    )
    _require_engine_signal_trace(result.signals, artifacts)
    return TechnicalV2BacktestResult(
        v1_strategy=strategy,
        result=result,
        signal_records=tuple(item.record for item in artifacts),
    )


def derive_condition_id(
    strategy_hash: str,
    condition_ref: str,
    condition: FrozenV2Model,
) -> str:
    """Return a stable identity for one condition in one exact Strategy v2."""

    if _SHA256.fullmatch(strategy_hash) is None:
        raise ValueError("strategy_hash must be a sha256 content hash")
    if condition_ref not in {"$.entry", "$.exit"}:
        raise ValueError("condition_ref must identify a supported root condition")
    payload: dict[str, object] = {
        "condition": condition.model_dump(mode="json"),
        "condition_ref": condition_ref,
        "strategy_hash": strategy_hash,
    }
    return canonical_hash(payload)


def _validated_plan(value: object) -> ExecutableStrategyPlan:
    if not is_validator_issued_plan(value):
        raise TechnicalV2ExecutionError(
            "plan_untrusted",
            "technical v2 execution requires a validator-issued plan",
        )
    return value


def _validate_fee_provider(
    plan: ExecutableStrategyPlan,
    provider: FeeQuoteProvider,
) -> None:
    if type(provider) not in {AttestedFeeQuoteProvider, AshareEtfAllInFeeProvider}:
        raise TechnicalV2ExecutionError(
            "capability_unavailable",
            "v2 execution requires a server-attested fee provider",
        )
    typed_provider = cast(AttestedFeeQuoteProvider | AshareEtfAllInFeeProvider, provider)
    if typed_provider.supported_asset_type is not plan.strategy.instrument.asset_type:
        raise TechnicalV2ExecutionError(
            "capability_unavailable",
            "fee policy does not match the validated instrument asset type",
        )
    if typed_provider.supported_exchange is not plan.strategy.instrument.exchange:
        raise TechnicalV2ExecutionError(
            "capability_unavailable",
            "fee policy exchange does not match the validated instrument exchange",
        )
    if typed_provider.supported_asset_type is AssetType.STOCK and (
        type(typed_provider) is not AttestedFeeQuoteProvider
        or typed_provider.policy_version != _STOCK_FEE_POLICY
    ):
        raise TechnicalV2ExecutionError(
            "capability_unavailable",
            "stock fee policy version is not supported",
        )
    if typed_provider.supported_asset_type is AssetType.ETF and (
        type(typed_provider) is not AshareEtfAllInFeeProvider
        or typed_provider.policy_version != _ETF_FEE_POLICY
    ):
        raise TechnicalV2ExecutionError(
            "capability_unavailable",
            "ETF fee policy version is not supported",
        )


def _decimal_identity(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _validate_snapshot_inputs(
    plan: ExecutableStrategyPlan,
    snapshot: SnapshotBindingV2,
    bars: tuple[DailyBar, ...],
) -> tuple[DailyBar, ...]:
    if snapshot.kind != "market_data":
        raise TechnicalV2ExecutionError(
            "data_unavailable",
            "technical execution requires a trusted market_data snapshot binding",
        )
    if snapshot.snapshot_id not in plan.data_snapshot_ids:
        raise TechnicalV2ExecutionError(
            "data_unavailable",
            "trusted market snapshot does not match the validated plan",
        )
    matching_coverage_sources = tuple(
        source
        for coverage in plan.dataset_coverage
        if coverage.dataset_id == "daily_ohlcv"
        and coverage.instrument_symbol == plan.strategy.instrument.symbol
        for source in coverage.source_refs
        if source.snapshot_id == snapshot.snapshot_id
        and source.provider == snapshot.provider
        and source.schema_version == snapshot.schema_version
        and source.content_sha256 == snapshot.content_hash
    )
    if not matching_coverage_sources:
        raise TechnicalV2ExecutionError(
            "data_unavailable",
            "snapshot provider, schema or hash differs from the validated coverage",
        )
    backtest = plan.strategy.backtest
    if snapshot.coverage_start > backtest.start or snapshot.coverage_end < backtest.end:
        raise TechnicalV2ExecutionError(
            "data_unavailable",
            "trusted market snapshot does not cover the requested period",
        )
    if not bars:
        raise TechnicalV2ExecutionError("data_unavailable", "trusted market snapshot has no bars")
    if any(bar.instrument_id.value != plan.strategy.instrument.symbol for bar in bars):
        raise TechnicalV2ExecutionError(
            "instrument_mismatch",
            "market bars do not match the validated instrument",
        )
    if any(current.session_date >= following.session_date for current, following in pairwise(bars)):
        raise TechnicalV2ExecutionError(
            "data_unavailable",
            "trusted market bars must be strictly date ordered",
        )
    if (
        bars[0].session_date < snapshot.coverage_start
        or bars[-1].session_date > snapshot.coverage_end
    ):
        raise TechnicalV2ExecutionError(
            "data_unavailable",
            "market bars fall outside trusted snapshot coverage",
        )
    for bar in bars:
        if bar.available_at.astimezone(_SHANGHAI).date() > snapshot.coverage_end:
            raise TechnicalV2ExecutionError(
                "lookahead_detected",
                "bar first availability falls outside the validated snapshot period",
            )
    return bars


def _signal_record(
    *,
    plan: ExecutableStrategyPlan,
    snapshot: SnapshotBindingV2,
    condition_id: str,
    condition_ref: str,
    fact: SignalFact,
    bars: tuple[DailyBar, ...],
) -> SignalRecord:
    signal_at = fact.available_at.astimezone(_SHANGHAI)
    retrieved_at = snapshot.generated_at.astimezone(_SHANGHAI)
    snapshot_source = SourceRef(
        provider=snapshot.provider,
        source_id=f"snapshot-manifest:{snapshot.snapshot_id}",
        snapshot_id=snapshot.snapshot_id,
        schema_version=snapshot.schema_version,
        content_sha256=snapshot.content_hash,
        source_kind=SourceKind.PROVIDER_RECORD,
    )
    digest = hashlib.sha256()
    for bar in bars:
        row_payload = _bar_payload(bar)
        row_json = json.dumps(
            row_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest.update(len(row_json).to_bytes(8, "big"))
        digest.update(row_json.encode("utf-8"))
    prefix_hash = "sha256:" + digest.hexdigest()
    first_bar = bars[0]
    last_bar = bars[-1]
    prefix_source = SourceRef(
        provider=_BAR_TRANSFORM_PROVIDER,
        source_id=(
            f"daily_ohlcv-prefix:{last_bar.instrument_id.value}:"
            f"{first_bar.session_date.isoformat()}:{last_bar.session_date.isoformat()}"
        ),
        snapshot_id=snapshot.snapshot_id,
        schema_version=_BAR_TRANSFORM_SCHEMA,
        content_sha256=prefix_hash,
        source_kind=SourceKind.DETERMINISTIC_TRANSFORM,
    )
    envelope = DataEnvelope(
        data_id=(
            f"daily_ohlcv-prefix:{first_bar.session_date.isoformat()}:"
            f"{last_bar.session_date.isoformat()}"
        ),
        value=prefix_hash,
        unit=None,
        availability=PointInTimeAvailability(
            observed_at=datetime.combine(
                last_bar.session_date,
                time(15),
                tzinfo=_SHANGHAI,
            ),
            announced_at=None,
            first_available_at=max(bar.available_at for bar in bars).astimezone(_SHANGHAI),
            signal_at=signal_at,
            execution_at=None,
            retrieved_at=retrieved_at,
            timezone="Asia/Shanghai",
            source=snapshot.provider,
            revision_id=prefix_source.source_id,
        ),
        source_refs=(snapshot_source, prefix_source),
    )
    return SignalRecord(
        instrument_id=plan.strategy.instrument.symbol,
        condition_id=condition_id,
        condition_ref=condition_ref,
        triggered=fact.triggered,
        signal_at=signal_at,
        plan_id=plan.plan_id,
        strategy_hash=plan.strategy_hash,
        dsl_schema_version=plan.strategy.schema_version,
        code_revision=plan.code_revision,
        input_envelopes=(envelope,),
        source_refs=envelope.source_refs,
    )


def _bar_payload(bar: DailyBar) -> dict[str, object]:
    return {
        "available_at": bar.available_at.isoformat(),
        "close": str(bar.close.amount),
        "high": str(bar.high.amount),
        "instrument_id": bar.instrument_id.value,
        "low": str(bar.low.amount),
        "open": str(bar.open.amount),
        "price_basis": bar.price_basis.value,
        "session_date": bar.session_date.isoformat(),
        "turnover": str(bar.turnover),
        "volume": bar.volume.value,
    }


def _require_engine_signal_trace(
    triggered_facts: tuple[SignalFact, ...],
    artifacts: tuple[TechnicalSignalArtifact, ...],
) -> None:
    artifact_keys = {
        (
            artifact.fact.instrument_id,
            artifact.fact.session_date,
            artifact.fact.condition_ref,
            artifact.fact.triggered,
            artifact.fact.available_at,
        )
        for artifact in artifacts
    }
    for fact in triggered_facts:
        key = (
            fact.instrument_id,
            fact.session_date,
            fact.condition_ref,
            fact.triggered,
            fact.available_at,
        )
        if key not in artifact_keys:
            raise TechnicalV2ExecutionError(
                "signal_trace_mismatch",
                "v1 engine used a signal without a matching v2 provenance record",
            )


__all__ = [
    "AshareEtfAllInFeeProvider",
    "AttestedFeeQuoteProvider",
    "TechnicalSignalArtifact",
    "TechnicalV2BacktestInput",
    "TechnicalV2BacktestResult",
    "TechnicalV2ExecutionError",
    "adapt_validated_technical_plan",
    "bind_stock_fee_provider",
    "build_technical_signal_records",
    "create_etf_all_in_fee_provider",
    "derive_condition_id",
    "generate_technical_signal_records",
    "run_validated_technical_v2",
]
